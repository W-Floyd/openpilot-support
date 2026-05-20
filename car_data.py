#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))

# Forks to load, in priority order (first fork's data wins for shared cars).
FORKS = [
    ("openpilot", os.path.join(HERE, "openpilot", "opendbc_repo")),
    ("sunnypilot", os.path.join(HERE, "sunnypilot", "opendbc_repo")),
    ("OPGM", os.path.join(HERE, "OPGM", "opendbc_repo")),
    ("BMW-E8x-E9x", os.path.join(HERE, "BMW-E8x-E9x", "opendbc_repo")),
    ("StarPilot", os.path.join(HERE, "StarPilot", "opendbc_repo")),
    ("BluePilot", os.path.join(HERE, "BluePilot", "opendbc_repo")),
]

OPENPILOT_CACHE_FILE = os.path.join(HERE, ".openpilot_cache.json")
CAR_ALIASES_FILE = os.path.join(HERE, "car_aliases.json")
FAMILY_MAPPINGS_FILE = os.path.join(HERE, "family_mappings.json")
AUTOTRADER_MODELS_CACHE_FILE = os.path.join(HERE, ".autotrader_models_cache.json")


def _load_car_aliases() -> dict[str, tuple[str, str]]:
    try:
        with open(CAR_ALIASES_FILE) as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            km, kmod = k.split("|", 1)
            vm, vmod = v.split("|", 1)
            result[(km, kmod)] = (vm, vmod)
        return result
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


CAR_ALIASES: dict[tuple[str, str], tuple[str, str]] = _load_car_aliases()


def _load_family_mappings() -> dict[tuple[str, str], list[str]]:
    try:
        with open(FAMILY_MAPPINGS_FILE) as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            make, model = k.split("|", 1)
            result[(make, model)] = v
        return result
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


FAMILY_MAPPINGS: dict[tuple[str, str], list[str]] = _load_family_mappings()


def resolve_alias(make: str, model: str) -> tuple[str, str]:
    """Return the canonical (make, model) for a car, following car_aliases.json."""
    return CAR_ALIASES.get((make, model), (make, model))


def to_ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def parse_years(years_str: str) -> list[int]:
    if not years_str:
        return []
    result = []
    for part in years_str.split(","):
        part = part.strip()
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str)
            end = (start // 100) * 100 + int(end_str)
            if end < start:
                end += 100
            result.extend(range(start, end + 1))
        else:
            result.append(int(part))
    return result


def _extract_years_from_model(car_docs) -> list[int]:
    """Extract year range from model name when years field is not available."""
    if parse_years(car_docs.years):
        return parse_years(car_docs.years)

    pattern = r"\d{4}-\d{2}|\d{4}"
    matches = re.findall(pattern, car_docs.model)
    if not matches:
        return []

    years: list[int] = []
    for match in matches:
        years.extend(parse_years(match))
    return sorted(set(years))


_NON_ACC_PATTERNS = ["NO ACC", "Non-ACC", "Non ACC", "No-ACC"]
_NON_ACC_REGEX = "|".join(f"( - )?{p}" for p in _NON_ACC_PATTERNS)
_NON_SCC_PATTERNS = ["Non-SCC"]
_NON_SCC_REGEX = "|".join(f"( - )?{p}" for p in _NON_SCC_PATTERNS)
_HARNESS_SUFFIX_RE = re.compile(r"\s+(\S+ Harness)\s*$", re.IGNORECASE)
_ACC_W_SUFFIX_RE = re.compile(r"\s+ACC w (\S+)\s*$", re.IGNORECASE)


def _clean_model_name(car_docs) -> str:
    """Strip year range from model name."""
    model = car_docs.model

    if not (car_docs.years):
        pattern = r"(\s*20\d{2}(-20\d{2}|-\d{2})?)"
        model = re.sub(pattern, "", model).strip()

    model = _HARNESS_SUFFIX_RE.sub("", model).strip()

    if "ACC" in (car_docs.package or ""):
        model = re.sub(
            rf"\s*({_NON_ACC_REGEX})", "", model, flags=re.IGNORECASE
        ).strip()

    if "SCC" in (car_docs.package or ""):
        model = re.sub(
            rf"\s*({_NON_SCC_REGEX})", "", model, flags=re.IGNORECASE
        ).strip()

    model = _ACC_W_SUFFIX_RE.sub("", model).strip()

    return model


def _modify_package_from_model(car_docs) -> str:
    package = car_docs.package or ""
    if "ACC" in package and re.search(_NON_ACC_REGEX, car_docs.model, re.IGNORECASE):
        return "No Adaptive Cruise Control (Non-ACC)"
    if "SCC" in package and re.search(_NON_SCC_REGEX, car_docs.model, re.IGNORECASE):
        return "No Smart Cruise Control (Non-SCC)"
    m = _HARNESS_SUFFIX_RE.search(car_docs.model)
    if m:
        suffix = m.group(1)
        return f"{package} + {suffix}" if package else suffix
    m = _ACC_W_SUFFIX_RE.search(car_docs.model)
    if m:
        suffix = m.group(1)
        return f"{package} + {suffix}" if package else suffix
    return package


@dataclass
class MockCarDocs:
    model: str
    years: str = ""


def test_clean_model_name() -> None:
    """Test the _clean_model_name function."""
    test_cases = [
        # (car_docs, expected_result)
        (MockCarDocs("Accord", "2018"), "Accord"),
        (MockCarDocs("Civic 2020-22", ""), "Civic"),
        (MockCarDocs("CR-V", "2015"), "CR-V"),
        (MockCarDocs("no years here", ""), "no years here"),
        (MockCarDocs("Silverado 1500", "2022"), "Silverado 1500"),
        (MockCarDocs("Silverado 1500 2022", ""), "Silverado 1500"),
        (
            MockCarDocs("Suburban Premier 2016-2020 - No-ACC", ""),
            "Suburban Premier - No-ACC",
        ),
    ]

    for car_docs, expected in test_cases:
        result = _clean_model_name(car_docs)
        assert result == expected, (
            f"Failed for {car_docs.model!r}: got {result!r}, expected {expected!r}"
        )

    print("All tests passed!")


def test_extract_years_from_model() -> None:
    """Test the _extract_years_from_model function."""
    test_cases = [
        # (input_model, expected_years)
        (
            MockCarDocs("Suburban Premier", "2016-20"),
            [2016, 2017, 2018, 2019, 2020],
        ),
        (MockCarDocs("Silverado 2020-21", ""), [2020, 2021]),
    ]

    for model, expected in test_cases:
        result = _extract_years_from_model(model)
        assert result == expected, (
            f"Failed for {model!r}: got {result}, expected {expected}"
        )

    print("All tests passed!")


def car_docs_to_dict(car_docs) -> dict:
    # Import from whichever opendbc fork is currently loaded in sys.modules.
    from opendbc.car.docs_definitions import CarHarness, Column, ExtraCarsColumn, Star

    row = car_docs.row

    def star_to_bool(val) -> bool | None:
        if isinstance(val, Star):
            return val == Star.FULL
        return None

    harness = None
    if car_docs.car_parts.parts:
        harness_docs = [
            part
            for part in car_docs.car_parts.all_parts()
            if isinstance(part, CarHarness)
        ]
        for part in harness_docs:
            harness = str(part.value.name).replace(" connector", "")

    return {
        "make": car_docs.make,
        "model": _clean_model_name(car_docs),
        "years": _extract_years_from_model(car_docs),
        "name": car_docs.name,
        "package": _modify_package_from_model(car_docs),
        "support_type": car_docs.support_type.value,
        "support_link": car_docs.support_link,
        "merged": car_docs.merged,
        "min_steer_speed": car_docs.min_steer_speed
        if car_docs.min_steer_speed is not None
        and not math.isinf(car_docs.min_steer_speed)
        else None,
        "min_enable_speed": car_docs.min_enable_speed
        if car_docs.min_enable_speed is not None
        and not math.isinf(car_docs.min_enable_speed)
        else None,
        "auto_resume": car_docs.auto_resume,
        "good_steering_torque": star_to_bool(row[Column.STEERING_TORQUE]),
        "openpilot_longitudinal": row[Column.LONGITUDINAL]
        if not isinstance(row[Column.LONGITUDINAL], Star)
        else star_to_bool(row[Column.LONGITUDINAL]),
        "video": car_docs.video,
        "setup_video": car_docs.setup_video,
        "detail_sentence": car_docs.detail_sentence,
        "harness": harness,
        # Formatted columns matching CARS_template.md ExtraCarsColumn
        "extra_cars_columns": {
            col.name.lower(): car_docs.get_extra_cars_column(col)
            for col in ExtraCarsColumn
        },
    }


def _setup_old_layout_stubs() -> None:
    """Inject stub modules for the old openpilot layout's compiled/hardware deps.

    Old-style forks (selfdrive/car layout) import hardware drivers and Cython
    extensions at module load time.  These are only used at runtime (actual CAN
    bus comms, USB panda connections) — not during docs generation — so safe to
    stub out.
    """
    import types

    def _stub(name: str, **attrs):
        if name in sys.modules:
            return sys.modules[name]
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Noop:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, *a, **kw):
            return self

    # usb1: Python libusb wrapper — only needed for USB comms with panda hardware
    _stub(
        "usb1",
        ENDPOINT_IN=0x80,
        ENDPOINT_OUT=0x00,
        TYPE_VENDOR=0x40,
        RECIPIENT_DEVICE=0x00,
        USBContext=_Noop,
        USBErrorIO=Exception,
        USBErrorOverflow=Exception,
    )

    # Compiled Cython CAN bus parser/packer — only needed for live CAN parsing
    _stub("opendbc.can.parser_pyx", CANParser=_Noop, CANDefine=_Noop)
    _stub("opendbc.can.packer_pyx", CANPacker=_Noop)

    # cereal.messaging IPC — only used at runtime, not during docs generation
    _stub("cereal.messaging")

    # Events system — only used in CarInterface.update(), not get_all_car_docs()
    _stub("openpilot.selfdrive.controls.lib.events", Events=_Noop)

    # requests: pulled in by panda/python/flash_release.py at import time in some forks
    _stub("requests")

    # panda: hardware driver, only needed for live USB comms with panda device
    # Panda.FLAG_* and uds.SERVICE_TYPE.* are integer class attributes — need a
    # metaclass so that ClassName.ANYTHING returns 0 without explicit definitions.
    class _IntNoop(int):
        def __new__(cls, *a, **kw):
            return super().__new__(cls, 0)

        def __getattr__(self, name):
            return _IntNoop()

        def __call__(self, *a, **kw):
            return _IntNoop()

    class _IntMeta(type):
        def __getattr__(cls, name):
            return 0

    class _PandaStub(metaclass=_IntMeta):
        def __init__(self, *a, **kw):
            pass

        def __call__(self, *a, **kw):
            return self

    _stub("panda", Panda=_PandaStub, PandaWifiStreaming=_Noop, PandaDFU=_Noop)
    _stub(
        "panda.python",
        Panda=_PandaStub,
        PandaWifiStreaming=_Noop,
        PandaDFU=_Noop,
        flash_release=_Noop,
        BASEDIR="",
        ensure_st_up_to_date=_Noop,
        build_st=_Noop,
        PandaSerial=_Noop,
        ESPROM=_Noop,
        CesantaFlasher=_Noop,
    )

    class _IntAttrs:
        """Stub for panda uds enum types: any attribute access returns plain int 0."""

        def __getattr__(self, name):
            return 0

    uds_mod = _stub("panda.python.uds")
    uds_mod.__getattr__ = lambda name: _IntAttrs()


def car_docs_to_dict_old(car_docs) -> dict:
    """Convert old-style (selfdrive/car) CarDocs to our standard dict format."""
    from openpilot.selfdrive.car.docs_definitions import Column, Star

    row = car_docs.row

    def star_to_bool(val) -> bool | None:
        if isinstance(val, Star):
            return val == Star.FULL
        return None

    return {
        "make": car_docs.make,
        "model": _clean_model_name(car_docs),
        "years": _extract_years_from_model(car_docs),
        "name": car_docs.name,
        "package": car_docs.package,
        "support_type": "Community",
        "support_link": "#community",
        "merged": False,
        "min_steer_speed": car_docs.min_steer_speed
        if car_docs.min_steer_speed is not None
        and not math.isinf(car_docs.min_steer_speed)
        else None,
        "min_enable_speed": car_docs.min_enable_speed
        if car_docs.min_enable_speed is not None
        and not math.isinf(car_docs.min_enable_speed)
        else None,
        "auto_resume": car_docs.auto_resume,
        "good_steering_torque": star_to_bool(row[Column.STEERING_TORQUE]),
        "openpilot_longitudinal": row[Column.LONGITUDINAL]
        if not isinstance(row[Column.LONGITUDINAL], Star)
        else star_to_bool(row[Column.LONGITUDINAL]),
        "video": getattr(car_docs, "video_link", None),
        "setup_video": None,
        "detail_sentence": car_docs.detail_sentence,
        "extra_cars_columns": {},
    }


def _load_cars_directly_old(fork_root: str) -> list[dict]:
    """Load car docs from an old-style openpilot fork (selfdrive/car layout)."""
    _setup_old_layout_stubs()
    sys.path.insert(0, fork_root)
    from openpilot.selfdrive.car.docs import get_all_car_docs

    # Some fork code prints debug info to stdout during get_params(); redirect to
    # stderr so it doesn't corrupt the JSON written to stdout by --dump-fork.
    old_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        cars = [car_docs_to_dict_old(cd) for cd in get_all_car_docs()]
    finally:
        sys.stdout = old_stdout
    return cars


def _load_cars_directly(fork_path: str) -> list[dict]:
    """Load and convert car docs from a fork directly.

    Detects the layout (new opendbc_repo vs old selfdrive/car) and dispatches
    accordingly.  Must run in an isolated process — capnp's schema registry is
    global and will reject duplicate schema IDs across forks.
    """
    if os.path.isdir(os.path.join(fork_path, "opendbc", "car")):
        # New layout: opendbc_repo/opendbc/car/docs.py
        sys.path.insert(0, fork_path)
        # Some forks (e.g. StarPilot) import cereal.custom and openpilot.* at module
        # level in car_helpers and per-brand files, but docs loading doesn't need them
        # at runtime.  We inject minimal stubs so those top-level imports succeed.
        #
        # Two-part strategy:
        # 1. Targeted cereal stub: expose cereal.custom as MagicMock but do NOT add a
        #    cereal.car attribute.  structs.py wraps `from cereal import car` in
        #    try/except ImportError and falls back to capnp.load("car.capnp"), giving the
        #    real CarParams.  Providing a fake cereal.car would break that for all forks.
        # 2. Meta path finder for openpilot.* only: StarPilot imports arbitrary submodules
        #    (e.g. openpilot.starpilot.common.testing_grounds) that aren't on sys.path in
        #    this subprocess.  Return MagicMock modules for any openpilot.* import.
        import importlib.abc
        import importlib.machinery
        import types
        from unittest.mock import MagicMock

        _injected: list[str] = []

        def _inject(name: str, obj) -> None:
            if name not in sys.modules:
                sys.modules[name] = obj
                _injected.append(name)

        # cereal stub — custom present, car absent (structs.py fallback handles car)
        _cereal = types.ModuleType("cereal")
        _cereal.__path__ = []  # mark as package so submodule imports resolve
        _cereal_custom = MagicMock()
        _cereal.custom = _cereal_custom  # type: ignore[attr-defined]
        _inject("cereal", _cereal)
        _inject("cereal.custom", _cereal_custom)

        # openpilot meta path finder — mocks any openpilot.* submodule on demand
        class _OpenpilotMockLoader(importlib.abc.Loader):
            def create_module(self, spec):
                mod = MagicMock()
                mod.__name__ = spec.name
                mod.__loader__ = self
                mod.__package__ = spec.name.rpartition(".")[0]
                mod.__spec__ = spec
                mod.__path__ = []
                return mod

            def exec_module(self, _module):
                pass

        # Legacy openpilot top-level packages that some forks (e.g. BluePilot) still
        # import directly (e.g. `from common.pid import ...`, `from selfdrive.modeld...`).
        _LEGACY_OPENPILOT_ROOTS = ("common", "selfdrive", "third_party", "tools")

        class _OpenpilotMockFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, _path, _target=None):
                if fullname == "openpilot" or fullname.startswith("openpilot."):
                    return importlib.machinery.ModuleSpec(
                        fullname, _OpenpilotMockLoader()
                    )
                # Mock cereal submodules on demand, but leave cereal.car absent so
                # structs.py's try/except ImportError fallback to capnp still triggers.
                if fullname.startswith("cereal.") and fullname != "cereal.car":
                    return importlib.machinery.ModuleSpec(
                        fullname, _OpenpilotMockLoader()
                    )
                # Mock legacy openpilot top-level packages (common, selfdrive, …) that
                # some forks still use without an `openpilot.` prefix.
                root = fullname.split(".")[0]
                if root in _LEGACY_OPENPILOT_ROOTS:
                    return importlib.machinery.ModuleSpec(
                        fullname, _OpenpilotMockLoader()
                    )
                return None

        _finder = _OpenpilotMockFinder()
        sys.meta_path.insert(0, _finder)
        try:
            # StarPilot added a required `starpilot_toggles` arg to get_params but
            # didn't update get_params_for_docs in docs.py, and some platforms are
            # missing from get_torque_params().  Only patch when the fork's get_params
            # actually requires starpilot_toggles.
            import inspect

            import opendbc.car.docs as _car_docs_mod
            from opendbc.car.docs import get_all_car_docs

            _sample_iface = next(iter(_car_docs_mod.interfaces.values()), None)
            if (
                _sample_iface is not None
                and "starpilot_toggles"
                in inspect.signature(_sample_iface.get_params).parameters
            ):

                def _patched_get_params_for_docs(platform):
                    from types import SimpleNamespace

                    from opendbc.car import gen_empty_fingerprint
                    from opendbc.car.structs import CarParams

                    cp_platform = (
                        platform
                        if platform in _car_docs_mod.interfaces
                        else _car_docs_mod.MOCK.MOCK
                    )
                    try:
                        return _car_docs_mod.interfaces[cp_platform].get_params(
                            cp_platform,
                            fingerprint=gen_empty_fingerprint(),
                            car_fw=[CarParams.CarFw(ecu=CarParams.Ecu.unknown)],
                            alpha_long=True,
                            is_release=False,
                            docs=True,
                            starpilot_toggles=SimpleNamespace(),
                        )
                    except Exception:
                        return _car_docs_mod.interfaces[
                            _car_docs_mod.MOCK.MOCK
                        ].get_params(
                            _car_docs_mod.MOCK.MOCK,
                            fingerprint=gen_empty_fingerprint(),
                            car_fw=[CarParams.CarFw(ecu=CarParams.Ecu.unknown)],
                            alpha_long=True,
                            is_release=False,
                            docs=True,
                            starpilot_toggles=SimpleNamespace(),
                        )

                _car_docs_mod.get_params_for_docs = _patched_get_params_for_docs

            # BluePilot references Device.threex_angled_mount / Device.threex in
            # init_make, but its Device enum only has Device.four.  Pre-import the
            # module and wrap apply_bp_device_mount to swallow AttributeErrors so
            # Ford cars are still included (just with default parts).
            try:
                import opendbc.sunnypilot.car.ford.values_ext as _ford_values_ext

                _orig_apply_bp = _ford_values_ext.apply_bp_device_mount

                def _safe_apply_bp_device_mount(car_docs, CP):
                    try:
                        _orig_apply_bp(car_docs, CP)
                    except AttributeError:
                        pass

                _ford_values_ext.apply_bp_device_mount = _safe_apply_bp_device_mount
            except (ImportError, AttributeError):
                pass

            result = [car_docs_to_dict(cd) for cd in get_all_car_docs()]
        finally:
            sys.meta_path.remove(_finder)
            for _mod in _injected:
                sys.modules.pop(_mod, None)
        return result
    else:
        # Old layout: fork_root/openpilot/selfdrive/car/docs.py
        return _load_cars_directly_old(os.path.dirname(fork_path))


def load_fork_cars(
    fork_name: str, fork_path: str, use_cache: bool = True
) -> list[dict] | None:
    """Load car docs from a fork's opendbc_repo via subprocess for isolation.

    If use_cache is True, cached results from previous runs are reused when
    available. The cache file stores fork-specific car data keyed by fork name.

    Returns None if the fork has no supported layout.
    """
    cache = load_openpilot_cache()
    if use_cache and fork_name in cache:
        print(
            f"  Using cached {fork_name} data...",
            file=sys.stderr,
        )
        return cache[fork_name]

    fork_root = os.path.dirname(fork_path)
    new_layout = os.path.isdir(os.path.join(fork_path, "opendbc", "car"))
    old_layout = os.path.isfile(
        os.path.join(fork_root, "openpilot", "selfdrive", "car", "docs.py")
    )
    if not new_layout and not old_layout:
        return None

    result = subprocess.run(
        [sys.executable, os.path.join(HERE, "parse.py"), "--dump-fork", fork_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr, end="")
        raise RuntimeError(f"Failed to load cars from {fork_path}")
    cars = json.loads(result.stdout)

    cache[fork_name] = cars
    cache[f"_git_{fork_name}"] = get_fork_git_info(fork_name, fork_path)
    save_openpilot_cache(cache)

    return cars


def _same_features(a: dict, b: dict) -> bool:
    """Return True if two car entries have the same functional support features."""
    return (
        a["package"] == b["package"]
        and (
            a["support_type"] == b["support_type"]
            or (
                (a["support_type"] == "Upstream" and b["support_type"] == "Community")
                or (
                    a["support_type"] == "Community" and b["support_type"] == "Upstream"
                )
            )
        )
        and (
            a["openpilot_longitudinal"] == b["openpilot_longitudinal"]
            or (
                a["openpilot_longitudinal"] == "openpilot"
                and b["openpilot_longitudinal"] == "openpilot available"
            )
        )
        and a["good_steering_torque"] == b["good_steering_torque"]
        and a["auto_resume"] == b["auto_resume"]
    )


def merge_fork_cars(fork_car_lists: list[tuple[str, list[dict]]]) -> list[dict]:
    merged: dict[str, dict] = {}
    for fork_name, cars in fork_car_lists:
        for car in cars:
            key = car["name"]
            if key in merged:
                merged[key]["forks"].append(fork_name)
            else:
                merged[key] = {**car, "forks": [fork_name]}

    # Group names by (make, model); only groups with >1 entry can have subsets.
    by_make_model: dict[tuple[str, str], list[str]] = {}
    for name, car in merged.items():
        by_make_model.setdefault((car["make"], car["model"]), []).append(name)

    to_remove: set[str] = set()
    for names in by_make_model.values():
        if len(names) < 2:
            continue
        # Process largest year ranges first so a subset is always absorbed by
        # the widest matching entry.
        names_by_size = sorted(
            names, key=lambda n: len(merged[n]["years"]), reverse=True
        )
        for i, larger_name in enumerate(names_by_size):
            if larger_name in to_remove:
                continue
            larger = merged[larger_name]
            larger_years = set(larger["years"])
            for smaller_name in names_by_size[i + 1 :]:
                if smaller_name in to_remove:
                    continue
                smaller = merged[smaller_name]
                smaller_years = set(smaller["years"])
                if smaller_years <= larger_years and _same_features(larger, smaller):
                    for fork in smaller["forks"]:
                        if fork not in larger["forks"]:
                            larger["forks"].append(fork)
                    to_remove.add(smaller_name)

    for name in to_remove:
        del merged[name]

    return sorted(
        merged.values(),
        key=lambda c: (c["make"], c["model"], min(c["years"]) if c["years"] else 0),
    )


def get_fork_git_info(fork_name: str, fork_path: str) -> dict:
    """Return {name, url, hash, hash_url} for a fork, or just {name} on failure."""
    fork_root = os.path.dirname(fork_path)
    try:
        remote = subprocess.check_output(
            ["git", "-C", fork_root, "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        sha = subprocess.check_output(
            ["git", "-C", fork_root, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # Normalise SSH → HTTPS and strip .git
        url = re.sub(r"^git@github\.com:", "https://github.com/", remote)
        url = re.sub(r"\.git$", "", url)
        return {
            "name": fork_name,
            "url": url,
            "hash": sha,
            "hash_url": f"{url}/commit/{sha}",
        }
    except Exception:
        return {"name": fork_name}


def load_openpilot_cache() -> dict:
    if os.path.exists(OPENPILOT_CACHE_FILE):
        try:
            with open(OPENPILOT_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_openpilot_cache(cache: dict) -> None:
    with open(OPENPILOT_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)
