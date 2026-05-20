#!/usr/bin/env python3
import argparse
import concurrent.futures
import html.parser
import http.server
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

import jinja2
import minify_html

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
CARGURUS_CACHE_FILE = os.path.join(HERE, ".cargurus_cache.json")
CARGURUS_IDS_FILE = os.path.join(HERE, ".cargurus_ids.json")
CAR_ALIASES_FILE = os.path.join(HERE, "car_aliases.json")
FAMILY_MAPPINGS_FILE = os.path.join(HERE, "family_mappings.json")
PROXIFLY_CACHE_FILE = os.path.join(HERE, ".proxifly_cache.json")


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


from dataclasses import dataclass


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
        [sys.executable, __file__, "--dump-fork", fork_path],
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


_CG_TRIVIAL_PACKAGES = {"All", "Any", ""}


def cargurus_car_key(car: dict) -> str | None:
    years = sorted(set(car["years"]))
    if not years:
        return None
    make = to_ascii(car["make"])
    model = to_ascii(car["model"])
    pkg = car.get("package", "")
    suffix = f"|{pkg}" if pkg not in _CG_TRIVIAL_PACKAGES else ""
    return f"{make}|{model}|{years[0]}-{years[-1]}{suffix}"


def to_ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def generate_favicon_svg() -> str:
    """Generate a simple SVG favicon."""
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<defs>
  <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
    <stop offset="0%" style="stop-color:#3B82F6;stop-opacity:1" />
    <stop offset="100%" style="stop-color:#10B981;stop-opacity:1" />
  </linearGradient>
</defs>
<rect width="100" height="100" rx="20" fill="url(#grad)"/>
<text x="50" y="65" font-family="Arial, sans-serif" font-size="50" font-weight="bold" text-anchor="middle" fill="white">OP</text>
</svg>"""


def generate_favicon_url(html_filename: str) -> str:
    """Generate a URL for the favicon relative to the HTML file."""
    base = html_filename.rsplit(".", 1)[0]
    return f"{base}-favicon.svg"


def _cg_model(model: str) -> str:
    """Strip trailing parenthetical from model name for CarGurus lookups."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", model).strip()


def cargurus_query_base(car: dict) -> str | None:
    if not car["years"]:
        return None
    make, model = resolve_alias(car["make"], _cg_model(car["model"]))
    return f"{to_ascii(make)} {to_ascii(model)}"


def cargurus_query(car: dict) -> str | None:
    base = cargurus_query_base(car)
    if base is None:
        return None
    pkg = car.get("package", "")
    if pkg not in _CG_TRIVIAL_PACKAGES:
        return f"{base} {pkg}"
    return base


PROXIFLY_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.json"
PROXIFLY_VERIFIED_CACHE_FILE = os.path.join(HERE, ".proxifly_verified_cache.json")
PROXIFLY_BLACKLIST_FILE = os.path.join(HERE, ".proxifly_blacklist_cache.json")
IP_CHECK_URL = "https://ip.notmy.space/json"

_ACTIVE_PROXIES: list[str] = []  # for urllib random-pick; read-only after init
_READY_PROXIES: queue.Queue = queue.Queue()  # pre-validated proxies ready to use
_proxy_pool_done = threading.Event()  # set when the check pool finishes
_proxy_direct_ip: str | None = None
_proxy_ip_check_url: str = IP_CHECK_URL
_proxy_tested_count: int = 0  # protected by a threading.Lock
_proxy_total_count: int = 0
_proxy_count_lock = threading.Lock()


class _ProxyBlocked(Exception):
    def __init__(self, blacklist: bool = False):
        self.blacklist = blacklist


def load_proxy_list(max_age_seconds: int = 3600) -> list[str]:
    """Fetch the proxifly proxy list (cached locally for max_age_seconds).

    Returns a list of 'http://ip:port' strings for http/https-capable proxies.
    """
    raw: list[dict] = []
    if os.path.exists(PROXIFLY_CACHE_FILE):
        if time.time() - os.path.getmtime(PROXIFLY_CACHE_FILE) < max_age_seconds:
            try:
                with open(PROXIFLY_CACHE_FILE) as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
    if not raw:
        print("  Fetching proxy list from proxifly...", file=sys.stderr)
        req = urllib.request.Request(
            PROXIFLY_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
        with open(PROXIFLY_CACHE_FILE, "w") as f:
            json.dump(raw, f)
    proxies = [p["proxy"] for p in raw if p.get("protocol") in ("http", "https")]
    print(f"  Loaded {len(proxies)} http/https proxies.", file=sys.stderr)
    return proxies


def _fetch_ip(
    proxy: str | None = None, ip_check_url: str = IP_CHECK_URL, timeout: int = 5
) -> str | None:
    try:
        req = urllib.request.Request(
            ip_check_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read())["ip"]
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["ip"]
    except Exception:
        return None


def _load_verified_proxy_cache() -> list[str] | None:
    if not os.path.exists(PROXIFLY_VERIFIED_CACHE_FILE):
        return None
    if not os.path.exists(PROXIFLY_CACHE_FILE):
        return None
    if os.path.getmtime(PROXIFLY_VERIFIED_CACHE_FILE) < os.path.getmtime(
        PROXIFLY_CACHE_FILE
    ):
        return None
    try:
        with open(PROXIFLY_VERIFIED_CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return None


def _save_verified_proxy_cache(proxies: list[str]) -> None:
    with open(PROXIFLY_VERIFIED_CACHE_FILE, "w") as f:
        json.dump(proxies, f)


def _start_proxy_pool(candidates: list[str], workers: int = 20) -> None:
    """Test all candidates in parallel; put working proxies into _READY_PROXIES.

    Runs in a daemon thread. Sets _proxy_pool_done and saves cache when done.
    """
    total = _proxy_total_count
    verified: list[str] = []
    verified_lock = threading.Lock()

    def _check(proxy: str) -> None:
        global _proxy_tested_count
        seen = _fetch_ip(proxy, _proxy_ip_check_url)
        with _proxy_count_lock:
            _proxy_tested_count += 1
            n = _proxy_tested_count
        if seen and seen != _proxy_direct_ip:
            print(f"  [{n}/{total}] {proxy} → {seen} ✓", file=sys.stderr)
            _READY_PROXIES.put(proxy)
            with verified_lock:
                verified.append(proxy)
                _save_verified_proxy_cache(verified)
        else:
            print(f"  [{n}/{total}] {proxy} ✗", file=sys.stderr)

    def _run() -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_check, candidates))
        _proxy_pool_done.set()

    threading.Thread(target=_run, daemon=True).start()


def _load_proxy_blacklist() -> set[str]:
    try:
        with open(PROXIFLY_BLACKLIST_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def _save_proxy_blacklist(blacklist: set[str]) -> None:
    with open(PROXIFLY_BLACKLIST_FILE, "w") as f:
        json.dump(sorted(blacklist), f)


def _find_next_proxy(blacklist: set[str] | None = None) -> str | None:
    """Return the next verified proxy from the ready queue, skipping any blacklisted entries."""
    while not _proxy_pool_done.is_set() or not _READY_PROXIES.empty():
        try:
            proxy = _READY_PROXIES.get(timeout=0.5)
            if blacklist and proxy in blacklist:
                continue
            return proxy
        except queue.Empty:
            continue
    return None


def _pick_proxy() -> str | None:
    return random.choice(_ACTIVE_PROXIES) if _ACTIVE_PROXIES else None


def _urlopen_proxied(req, timeout: int = 10):
    proxy = _pick_proxy()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _cg_selector_fetch(path: str) -> dict:
    url = f"https://www.cargurus.com{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _urlopen_proxied(req, timeout=10) as resp:
        return json.loads(resp.read())


def enumerate_cargurus_ids() -> dict:
    """Fetch complete CarGurus make→model taxonomy via the Car Selector API.

    Returns a nested dict:
        {make_name: {"id": "m4", "models": {model_name: "d2137", ...}}, ...}
    Also writes .cargurus_ids.json.
    """
    makes = _cg_selector_fetch(
        "/Cars/api/1.0/carselector/listMakes.action?searchType=USED"
    )["makes"]
    print(f"  Fetched {len(makes)} makes.", file=sys.stderr)

    result: dict = {}
    total = len(makes)
    for idx, make in enumerate(makes, 1):
        try:
            models_resp = _cg_selector_fetch(
                f"/Cars/api/1.0/carselector/listModels.action"
                f"?searchType=USED&makeId={make['id']}"
            )
            models = {m["name"]: m["id"] for m in models_resp.get("models", [])}
        except Exception as e:
            print(f"  [{idx}/{total}] {make['name']}: error — {e}", file=sys.stderr)
            models = {}
        result[make["name"]] = {"id": make["id"], "models": models}
        print(
            f"  [{idx}/{total}] {make['name']}: {len(models)} models",
            file=sys.stderr,
        )
        time.sleep(0.5)

    with open(CARGURUS_IDS_FILE, "w") as f:
        json.dump(dict(sorted(result.items())), f, indent=2)
    print(f"  Saved to {CARGURUS_IDS_FILE}", file=sys.stderr)
    return result


def load_cargurus_ids() -> dict:
    if os.path.exists(CARGURUS_IDS_FILE):
        try:
            with open(CARGURUS_IDS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def warn_unmatched_cargurus(cars: list[dict], cg_ids: dict) -> None:
    """Print a warning for each car whose make or model isn't in the CarGurus taxonomy."""
    if not cg_ids:
        return
    # Normalize CG model names (some have trailing whitespace in the taxonomy)
    cg_ids_norm = {
        make: {m.strip(): id_ for m, id_ in entry["models"].items()}
        for make, entry in cg_ids.items()
    }
    unmatched_make: list[str] = []
    unmatched_model: list[str] = []
    for car in cars:
        make, model = resolve_alias(car["make"], _cg_model(car["model"]))
        make_entry = cg_ids_norm.get(make)
        if make_entry is None:
            unmatched_make.append(f"{car['make']} {car['model']}")
        elif model not in make_entry:
            unmatched_model.append(f"{make} {model}")
    if unmatched_make:
        print(
            f"  CarGurus: {len(unmatched_make)} car(s) with unrecognised make:",
            file=sys.stderr,
        )
        for name in sorted(set(unmatched_make)):
            print(f"    {name}", file=sys.stderr)
    if unmatched_model:
        print(
            f"  CarGurus: {len(unmatched_model)} car(s) with unrecognised model:",
            file=sys.stderr,
        )
        for name in sorted(set(unmatched_model)):
            print(f"    {name}", file=sys.stderr)


def fetch_cargurus_response(query: str) -> dict | None:
    url = (
        f"https://www.cargurus.com/api/vehicle-discovery-service/v2/search/suggestions"
        f"?query={urllib.parse.quote(query)}&includeRecentSearch=false&includeSavedSearch=false"
        f"&countryCode=UNITED_STATES&newOrUsed=USED&origin=HOMEPAGE&devicePlatform=DESKTOP"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen_proxied(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Error fetching '{query}': {e}", file=sys.stderr)
        return None


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


def load_cargurus_cache() -> dict:
    if os.path.exists(CARGURUS_CACHE_FILE):
        try:
            with open(CARGURUS_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_cargurus_cache(cache: dict) -> None:
    with open(CARGURUS_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


def _extract_cg_paths(response: dict | None) -> dict | None:
    """Distill a suggestions API response down to the two path lists we actually need."""
    results = (response or {}).get("data", {}).get("results")
    if not results:
        return None
    trim_paths = _pick_cargurus_paths(results, want_trim=True)
    base_paths = _pick_cargurus_paths(results, want_trim=False)
    if trim_paths is None and base_paths is None:
        return None
    return {"trim_paths": trim_paths, "base_paths": base_paths}


def fetch_cargurus_cache(cars: list[dict], retry_nulls: bool = False) -> dict:
    """Fetch CarGurus data for all cars, updating the cache file. Returns compact path cache."""
    cache = load_cargurus_cache()
    valid_queries = set()
    for car in cars:
        if q := cargurus_query(car):
            valid_queries.add(q)
        if b := cargurus_query_base(car):
            valid_queries.add(b)
    stale = [k for k in cache if k not in valid_queries]
    if stale:
        for k in stale:
            del cache[k]
        save_cargurus_cache(cache)
    pending_set = set()
    for car in cars:
        for q in (cargurus_query(car), cargurus_query_base(car)):
            if q and (q not in cache or (retry_nulls and cache[q] is None)):
                pending_set.add(q)
    pending = list(pending_set)
    total = len(pending)

    def fetch_one(query: str, idx: int) -> tuple[str, object]:
        print(f"  [{idx}/{total}] Fetching CarGurus: {query}", file=sys.stderr)
        return query, _extract_cg_paths(fetch_cargurus_response(query))

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_one, q, i + 1): q for i, q in enumerate(pending)}
        for future in concurrent.futures.as_completed(futures):
            query, entry = future.result()
            cache[query] = entry
            save_cargurus_cache(cache)
    return cache


def _pick_cargurus_paths(results: list[dict], want_trim: bool) -> list[str] | None:
    """Pick the best makeModelTrimPaths from a suggestions response results list.

    Prefers MakeModelTrim when want_trim is True (car has a non-trivial package),
    falls back to MakeModel. Always ignores year-specific result types so the caller
    can apply its own year range.
    """
    preferred = "MakeModelTrim" if want_trim else "MakeModel"
    fallback = "MakeModel" if want_trim else "MakeModelTrim"
    for type_ in (preferred, fallback):
        for r in results:
            if r.get("type") == type_:
                paths = r.get("filterCriteria", {}).get("makeModelTrimPaths")
                if paths:
                    return paths
    # Last resort: any result with paths
    for r in results:
        paths = r.get("filterCriteria", {}).get("makeModelTrimPaths")
        if paths:
            return paths
    return None


def build_cargurus_js_cache(cars: list[dict], raw_cache: dict) -> dict:
    """Convert compact path cache to JS-ready {carKey: {paths}} or {carKey: {error}} map."""
    result = {}
    for car in cars:
        key = cargurus_car_key(car)
        if key is None:
            continue
        pkg = car.get("package", "")
        want_trim = pkg not in _CG_TRIVIAL_PACKAGES
        queries = list(
            dict.fromkeys(
                q for q in (cargurus_query(car), cargurus_query_base(car)) if q
            )
        )
        matched = False
        for i, query in enumerate(queries):
            entry = raw_cache.get(query)
            if entry:
                use_trim = want_trim and i == 0
                paths = entry.get("trim_paths" if use_trim else "base_paths")
                if paths:
                    result[key] = {"paths": paths}
                    matched = True
                    break
        if not matched and queries[0] in raw_cache:
            result[key] = {"error": True}
    return result


ARI_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".ari_cache.json")


class JsonLdExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_ld = False
        self._blocks: list[str] = []
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script" and ("type", "application/ld+json") in attrs:
            self._in_ld = True
            self._buf = ""

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._blocks.append(self._buf)
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf += data

    @property
    def blocks(self) -> list[dict]:
        result = []
        for b in self._blocks:
            try:
                result.append(json.loads(b))
            except json.JSONDecodeError:
                pass
        return result


def ari_slug(text: str) -> str:
    return to_ascii(text).lower().replace(" ", "-")


def ari_url(make: str, model: str, year: int) -> str:
    return f"https://autoreliabilityindex.com/{ari_slug(make)}/{ari_slug(model)}/{year}"


def fetch_ari_response(make: str, model: str, year: int) -> dict | None:
    url = ari_url(make, model, year)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen_proxied(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching '{url}': {e}", file=sys.stderr)
        return None

    parser = JsonLdExtractor()
    parser.feed(body)
    for block in parser.blocks:
        entity = block.get("mainEntity", {})
        entities = entity if isinstance(entity, list) else [entity]
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            review = ent.get("review", {})
            if isinstance(review, list):
                review = review[0] if review else {}
            rating = review.get("reviewRating", {}) if isinstance(review, dict) else {}
            score = rating.get("ratingValue")
            if score is not None:
                return {"score": score, "url": url}
    return None


def load_ari_cache() -> dict:
    if os.path.exists(ARI_CACHE_FILE):
        try:
            with open(ARI_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_ari_cache(cache: dict) -> None:
    with open(ARI_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


def ari_cache_key(make: str, model: str, year: int) -> str:
    return f"{to_ascii(make)}|{to_ascii(model)}|{year}"


def fetch_ari_cache(cars: list[dict], retry_nulls: bool = False) -> dict:
    """Fetch ARI data for all car/year combinations, updating the cache file."""
    cache = load_ari_cache()
    valid_keys = {
        ari_cache_key(car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
    }
    stale = [k for k in cache if k not in valid_keys]
    if stale:
        for k in stale:
            del cache[k]
        save_ari_cache(cache)
    pending = [
        (car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
        if (k := ari_cache_key(car["make"], car["model"], year)) not in cache
        or (retry_nulls and cache[k] is None)
    ]
    total = len(pending)

    def fetch_one(entry: tuple[str, str, int], idx: int) -> tuple[str, object]:
        make, model, year = entry
        print(f"  [{idx}/{total}] Fetching ARI: {make} {model} {year}", file=sys.stderr)
        return ari_cache_key(make, model, year), fetch_ari_response(make, model, year)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_one, entry, i + 1): entry
            for i, entry in enumerate(pending)
        }
        for future in concurrent.futures.as_completed(futures):
            key, result = future.result()
            cache[key] = result
            save_ari_cache(cache)
    return cache


CC_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".carcomplaints_cache.json")


CC_SEALS = {
    "best.png": "Seal of Awesome",
    "good.png": "Seal of Pretty Good",
    "bad.png": "Beware of the Clunker",
    "worst.png": "Avoid Like The Plague",
}

EDMUNDS_CACHE_FILE = os.path.join(HERE, ".edmunds_cache.json")


def edmunds_slug(text: str) -> str:
    s = to_ascii(text).lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s)


def edmunds_url(make: str, model: str, year: int) -> str:
    return f"https://www.edmunds.com/{edmunds_slug(make)}/{edmunds_slug(_cg_model(model))}/{year}/review/"


def edmunds_cache_key(make: str, model: str, year: int) -> str:
    return f"{to_ascii(make)}|{to_ascii(model)}|{year}"


def _load_firefox_edmunds_cookies() -> list[dict]:
    import glob
    import shutil
    import sqlite3
    import tempfile

    profiles = glob.glob(
        os.path.expanduser(
            "~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite"
        )
    )
    if not profiles:
        return []
    # Use the most recently modified profile
    db = max(profiles, key=os.path.getmtime)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        shutil.copy2(db, tmp.name)
        tmp_path = tmp.name
    try:
        con = sqlite3.connect(tmp_path)
        rows = con.execute(
            "SELECT name, value, host, path, isSecure, isHttpOnly, expiry FROM moz_cookies WHERE host LIKE '%edmunds.com'"
        ).fetchall()
        con.close()
    finally:
        os.unlink(tmp_path)
    cookies = []
    for name, value, host, path, secure, http_only, expiry in rows:
        domain = host if host.startswith(".") else host
        # Firefox stores expiry in seconds; values >1e11 are milliseconds
        expires = expiry / 1000 if expiry > 1e11 else float(expiry)
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": bool(secure),
                "httpOnly": bool(http_only),
                "expires": expires,
            }
        )
    return cookies


def _parse_edmunds_price(text: str) -> dict | None:
    prices = [int(m.replace(",", "")) for m in re.findall(r"[\d,]+", text)]
    if not prices:
        return None
    return {"min": min(prices), "max": max(prices)}



async def _edmunds_single_price(
    page, *label_texts: str, timeout: int = 3000
) -> int | None:
    """Extract the first dollar amount following any of the given label texts."""
    for label in label_texts:
        try:
            loc = page.locator(f"text={label}").first
            container = loc.locator("xpath=..")
            text = await container.text_content(timeout=timeout)
            nums = [int(m.replace(",", "")) for m in re.findall(r"[\d,]+", text or "")]
            if nums:
                return nums[0]
        except Exception:
            pass
    return None


async def _fetch_edmunds_price_with_page(
    make: str, model: str, year: int, page
) -> dict | None:
    url = edmunds_url(make, model, year)
    try:
        response = await page.goto(url, timeout=12000, wait_until="domcontentloaded")
        actual_title = await page.title()
        print(f"    car page: HTTP {response.status if response else '?'}, title={actual_title!r}, url={page.url}", file=sys.stderr)
        if response and response.status == 404:
            return None
        if response and response.status >= 400:
            title = await page.title()
            headers = dict(response.headers)
            screenshot_path = os.path.join(HERE, f"_edmunds_debug_{response.status}.png")
            try:
                await page.screenshot(path=screenshot_path)
            except Exception:
                screenshot_path = "(screenshot failed)"
            print(
                f"  HTTP {response.status} fetching Edmunds {make} {model} {year}\n"
                f"    title: {title!r}\n"
                f"    url: {page.url}\n"
                f"    screenshot: {screenshot_path}\n"
                f"    headers: { {k: v for k, v in headers.items() if k.lower() in ('server','cf-ray','x-amz-cf-id','x-cache','via','location','content-type','x-powered-by')} }",
                file=sys.stderr,
            )
            raise _ProxyBlocked(blacklist=True)
        if "page not found" in actual_title.lower():
            return None
        # Scroll to trigger lazy-loading of the below-fold price section
        await page.mouse.wheel(0, 600)
        await page.mouse.wheel(0, 600)
        loc = page.locator("text=Price Range:").first
        await loc.wait_for(timeout=15000)
        price_str = await loc.text_content(timeout=5000)
        entry = _parse_edmunds_price(price_str or "")
        if not entry:
            return None
        entry["url"] = page.url
        entry["suggested"] = await _edmunds_single_price(
            page, "Edmunds Suggested Price", "Edmunds suggests you pay"
        )
        entry["avg_used"] = await _edmunds_single_price(page, "Average price")
        entry["lastUpdated"] = time.time()
        return entry
    except _ProxyBlocked:
        raise
    except Exception as e:
        err = str(e)
        if "Target page, context or browser has been closed" in err:
            raise SystemExit(
                "error: Edmunds browser was closed unexpectedly — exiting."
            )
        if "Timeout" in err and ("goto" in err or "navigation" in err):
            raise _ProxyBlocked(blacklist=True)
        print(f"  Error fetching Edmunds {make} {model} {year}: {e}", file=sys.stderr)
        return None


def load_edmunds_cache() -> dict:
    if os.path.exists(EDMUNDS_CACHE_FILE):
        try:
            with open(EDMUNDS_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_edmunds_cache(cache: dict) -> None:
    with open(EDMUNDS_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


async def _fetch_edmunds_cache_async(
    pending: list,
    cache: dict,
    headless: bool,
    sleep: float,
    jitter: float,
    use_firefox_cookies: bool = False,
) -> dict:
    import asyncio

    from camoufox.async_api import AsyncNewBrowser
    from playwright.async_api import async_playwright

    MAX_RETRIES = 3
    total = len(pending)
    idx = 0
    iter_times: list[float] = []
    retries: dict[int, int] = {}
    blacklist: set[str] = _load_proxy_blacklist()

    async def _make_page(browser):
        ctx_kwargs: dict = {
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        ctx = await browser.new_context(**ctx_kwargs)
        if use_firefox_cookies:
            cookies = _load_firefox_edmunds_cookies()
            if cookies:
                await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        return ctx, page

    async with async_playwright() as p:
        proxy = await asyncio.to_thread(_find_next_proxy, blacklist) if _ACTIVE_PROXIES else None
        browser: object = None
        ctx: object = None
        page: object = None

        async def _launch_browser() -> None:
            nonlocal browser, ctx, page
            if browser is not None:
                await browser.close()
                browser = ctx = page = None
            proxy_arg = {"server": proxy} if proxy else None
            browser = await AsyncNewBrowser(
                p,
                headless=headless,
                humanize=True,
                block_webrtc=True,
                os=["windows", "macos"],
                proxy=proxy_arg,
                geoip=proxy is not None,
            )

        async def _rotate(exc: _ProxyBlocked | None = None) -> None:
            nonlocal proxy, browser, ctx, page
            if exc and exc.blacklist and proxy:
                blacklist.add(proxy)
                _save_proxy_blacklist(blacklist)
                print(f"  Blacklisted proxy {proxy}", file=sys.stderr)
            if browser is not None:
                await browser.close()
                browser = ctx = page = None
            proxy = (
                await asyncio.to_thread(_find_next_proxy, blacklist) if _ACTIVE_PROXIES else None
            )

        while idx < total:
            if browser is None:
                try:
                    await _launch_browser()
                except Exception as e:
                    print(f"  Browser launch failed ({e}) — rotating", file=sys.stderr)
                    await _rotate(_ProxyBlocked(blacklist=True))
                    continue
                ctx, page = await _make_page(browser)

                print(
                    f"  Visiting Edmunds homepage{f' via {proxy}' if proxy else ''}...",
                    file=sys.stderr,
                )
                try:
                    hp_resp = await page.goto(
                        "https://www.edmunds.com/", timeout=12000, wait_until="load"
                    )
                    hp_status = hp_resp.status if hp_resp else "?"
                    hp_title = await page.title()
                    print(
                        f"  Homepage: HTTP {hp_status}, title={hp_title!r}, url={page.url}",
                        file=sys.stderr,
                    )
                    homepage_ok = hp_resp is None or hp_resp.status < 400
                    if not homepage_ok:
                        screenshot_path = os.path.join(HERE, "_edmunds_debug_homepage.png")
                        try:
                            await page.screenshot(path=screenshot_path)
                        except Exception:
                            screenshot_path = "(failed)"
                        print(f"  Homepage blocked — screenshot: {screenshot_path}", file=sys.stderr)
                except Exception as e:
                    print(f"  Homepage failed ({e}) — rotating", file=sys.stderr)
                    homepage_ok = False
                if not homepage_ok:
                    await _rotate()
                    continue

            make, model, year = pending[idx]
            i = idx + 1
            t0 = asyncio.get_event_loop().time()
            delay = (sleep + random.uniform(0, jitter)) if i < total else 0.0
            eta_str = ""
            if iter_times:
                avg = sum(iter_times) / len(iter_times)
                remaining_secs = avg * (total - i + 1) + delay
                m, s = divmod(int(remaining_secs), 60)
                eta_str = f"  ETA ~{m}m{s:02d}s" if m else f"  ETA ~{s}s"
            print(
                f"  [{i}/{total}] Fetching Edmunds: {make} {model} {year}{eta_str}",
                file=sys.stderr,
            )
            try:
                key = edmunds_cache_key(make, model, year)
                cache[key] = await _fetch_edmunds_price_with_page(
                    make, model, year, page
                )
                save_edmunds_cache(cache)
                if delay > 0:
                    await asyncio.sleep(delay)
                iter_times.append(asyncio.get_event_loop().time() - t0)
                idx += 1
            except _ProxyBlocked as exc:
                await _rotate(exc)
            except Exception as e:
                err_str = str(e)
                if "Target page, context or browser has been closed" in err_str:
                    print("  Browser closed unexpectedly — exiting", file=sys.stderr)
                    if browser:
                        await browser.close()
                    return cache
                retries[idx] = retries.get(idx, 0) + 1
                if retries[idx] >= MAX_RETRIES:
                    cache[edmunds_cache_key(make, model, year)] = None
                    print(
                        f"  Giving up on Edmunds {make} {model} {year} ({retries[idx]} failures)",
                        file=sys.stderr,
                    )
                    idx += 1
                else:
                    print(
                        f"  Fetch error ({e}) ({retries[idx]}/{MAX_RETRIES}) — retrying...",
                        file=sys.stderr,
                    )
                    await _rotate()

        if browser:
            await browser.close()
    return cache


def fetch_edmunds_cache(
    cars: list[dict],
    retry_nulls: bool = False,
    headless: bool = True,
    sleep: float = 15.0,
    jitter: float = 5.0,
    use_firefox_cookies: bool = False,
) -> dict:
    """Fetch Edmunds price data per car/year using stealth Chromium."""
    import asyncio

    cache = load_edmunds_cache()
    valid_keys = {
        edmunds_cache_key(car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
    }
    stale = [k for k in cache if k not in valid_keys]
    if stale:
        for k in stale:
            del cache[k]
        save_edmunds_cache(cache)
    _six_months_ago = time.time() - 182 * 86400
    pending = [
        (car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
        if (k := edmunds_cache_key(car["make"], car["model"], year)) not in cache
        or (retry_nulls and cache[k] is None)
        or (
            isinstance(cache.get(k), dict)
            and cache[k].get("lastUpdated", 0) < _six_months_ago
        )
    ]
    if not pending:
        return cache
    return asyncio.run(
        _fetch_edmunds_cache_async(
            pending, cache, headless, sleep, jitter, use_firefox_cookies
        )
    )


AUTOTRADER_MODELS_CACHE_FILE = os.path.join(HERE, ".autotrader_models_cache.json")


def _build_autotrader_mappings() -> dict[tuple[str, str], str]:
    try:
        with open(AUTOTRADER_MODELS_CACHE_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    # Build lookup: make_name -> {normalized_model_name -> model_code}
    # Normalized: lowercase, strip trailing "+", collapse whitespace
    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s.rstrip("+")).lower()

    at_lookup: dict[str, dict[str, str]] = {}
    for make_entry in data.get("payload", {}).get("makeCode", []):
        models: dict[str, str] = {}
        for model in make_entry.get("models", []):
            models[norm(model["name"])] = model["code"]
        at_lookup[make_entry["name"]] = models

    mappings: dict[tuple[str, str], str] = {}
    for (make, _), variants in FAMILY_MAPPINGS.items():
        make_lookup = at_lookup.get(make, {})
        for variant in variants:
            code = make_lookup.get(norm(variant))
            if code:
                mappings[(make, variant)] = code
    return mappings


AUTOTRADER_MAPPINGS = _build_autotrader_mappings()


class CcParser(html.parser.HTMLParser):
    """Extracts subnav counts, seal, and JSON-LD from a carcomplaints.com page."""

    def __init__(self):
        super().__init__()
        self._in_ld = False
        self._ld_buf = ""
        self.ld_blocks: list[str] = []
        self._current_li_id: str | None = None
        self._in_cnt = False
        self.counts: dict[str, str] = {}
        self.seal: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "script" and attrs_dict.get("type") == "application/ld+json":
            self._in_ld = True
            self._ld_buf = ""
        elif tag == "li" and attrs_dict.get("id"):
            self._current_li_id = attrs_dict["id"]
        elif tag == "span" and attrs_dict.get("class") == "cnt" and self._current_li_id:
            self._in_cnt = True
        elif tag == "img" and self.seal is None:
            src = attrs_dict.get("src", "")
            filename = src.rsplit("/", 1)[-1]
            if filename in CC_SEALS:
                self.seal = CC_SEALS[filename]

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld_blocks.append(self._ld_buf)
            self._in_ld = False
        elif tag == "span" and self._in_cnt:
            self._in_cnt = False
        elif tag == "li":
            self._current_li_id = None

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf += data
        elif self._in_cnt and self._current_li_id:
            self.counts[self._current_li_id] = data.strip()


def _parse_cc_count(val: str) -> int | None:
    """Convert '8K', '262', etc. to int."""
    val = val.strip().upper()
    if not val:
        return None
    try:
        if val.endswith("K"):
            return int(float(val[:-1]) * 1000)
        return int(val)
    except ValueError:
        return None


def cc_slug(text: str) -> str:
    return to_ascii(text).replace(" ", "_")


def cc_url(make: str, raw_model: str, year: int) -> str:
    return f"https://www.carcomplaints.com/{cc_slug(make)}/{cc_slug(raw_model)}/{year}/"


def cc_cache_key(make: str, raw_model: str, year: int) -> str:
    return f"{to_ascii(make)}|{to_ascii(raw_model)}|{year}"


def fetch_cc_response(make: str, raw_model: str, year: int) -> dict | None:
    url = cc_url(make, raw_model, year)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen_proxied(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching '{url}': {e}", file=sys.stderr)
        return None

    p = CcParser()
    p.feed(body)

    complaints = _parse_cc_count(p.counts.get("prbNav", ""))
    recalls = _parse_cc_count(p.counts.get("rclNav", ""))
    tsbs = _parse_cc_count(p.counts.get("tsbNav", ""))
    investigations = _parse_cc_count(p.counts.get("invNav", ""))

    # Top problems from JSON-LD ItemList
    top_problems: list[str] = []
    for raw in p.ld_blocks:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") == "ItemList":
                for el in item.get("itemListElement", []):
                    headline = el.get("headline", "")
                    if headline:
                        top_problems.append(headline)

    if complaints is None and not top_problems:
        return None

    return {
        "url": url,
        "complaints": complaints,
        "recalls": recalls,
        "tsbs": tsbs,
        "investigations": investigations,
        "top_problems": top_problems,
        "seal": p.seal,
    }


def load_cc_cache() -> dict:
    if os.path.exists(CC_CACHE_FILE):
        try:
            with open(CC_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_cc_cache(cache: dict) -> None:
    with open(CC_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


def fetch_cc_cache(cars: list[dict], retry_nulls: bool = False) -> dict:
    """Fetch CarComplaints data for all car/year combinations, updating the cache file."""
    cache = load_cc_cache()
    valid_keys = {
        cc_cache_key(car["make"], raw_model, year)
        for car in cars
        for year in sorted(set(car["years"]))
        for raw_model in (
            FAMILY_MAPPINGS.get((car["make"], car["model"])) or [car["model"]]
        )
    }
    stale = [k for k in cache if k not in valid_keys]
    if stale:
        for k in stale:
            del cache[k]
        save_cc_cache(cache)
    pending = [
        (car["make"], raw_model, year)
        for car in cars
        for year in sorted(set(car["years"]))
        for raw_model in (
            FAMILY_MAPPINGS.get((car["make"], car["model"])) or [car["model"]]
        )
        if (k := cc_cache_key(car["make"], raw_model, year)) not in cache
        or (retry_nulls and cache[k] is None)
    ]
    total = len(pending)

    def fetch_one(entry: tuple[str, str, int], idx: int) -> tuple[str, object]:
        make, raw_model, year = entry
        print(
            f"  [{idx}/{total}] Fetching CarComplaints: {make} {raw_model} {year}",
            file=sys.stderr,
        )
        return cc_cache_key(make, raw_model, year), fetch_cc_response(
            make, raw_model, year
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(fetch_one, entry, i + 1): entry
            for i, entry in enumerate(pending)
        }
        for future in concurrent.futures.as_completed(futures):
            key, result = future.result()
            cache[key] = result
            save_cc_cache(cache)
    return cache


ALPINE_JS_URL = "https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"
PURE_CSS_URL = "https://cdn.jsdelivr.net/npm/purecss@3.0.0/build/base-min.css"
ALPINE_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".alpine_cache.js")
PURE_CSS_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".pure_css_cache.css")


def fetch_asset(url: str, cache_file: str) -> str:
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return f.read()
    print(f"Fetching {url} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as r:
        content = r.read().decode()
    with open(cache_file, "w") as f:
        f.write(content)
    return content


def _js_str(v) -> str:
    """Stringify a value the same way JS String() does."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def build_filter_index(cars: list[dict], cc_cache: dict) -> dict:
    """
    Build an inverted index mapping each filterable field value to the sorted
    list of car indices that have that value. Precomputed in Python so JS init()
    only needs a fast index→car-ref conversion instead of iterating all cars.
    """
    direct_fields = [
        "make",
        "model",
        "forks",
        "support_type",
        "openpilot_longitudinal",
        "merged",
        "auto_resume",
        "good_steering_torque",
        "years",
        "package",
        "harness",
        "min_steer_speed",
        "min_enable_speed",
    ]
    index: dict[str, dict[str, list[int]]] = {f: {} for f in direct_fields}
    index["cc_seal"] = {}

    for i, car in enumerate(cars):
        for field in direct_fields:
            val = car.get(field)
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if v is not None:
                    index[field].setdefault(_js_str(v), []).append(i)

        # Compute cc_seal membership using the same lookup as the JS
        make, model = car["make"], car["model"]
        raw_models = FAMILY_MAPPINGS.get((make, model)) or [model]
        has_none = False
        seal_values: set[str] = set()
        for year in sorted(set(car["years"])):
            entry = next(
                (
                    cc_cache.get(cc_cache_key(make, rm, year))
                    for rm in raw_models
                    if cc_cache.get(cc_cache_key(make, rm, year))
                ),
                None,
            )
            if entry and entry.get("seal"):
                seal_values.add(entry["seal"])
            else:
                has_none = True
        for seal in seal_values:
            index["cc_seal"].setdefault(seal, []).append(i)
        if has_none:
            index["cc_seal"].setdefault("", []).append(i)

    return index


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


def generate_html(
    cars: list[dict],
    cargurus_js_cache: dict | None = None,
    ari_cache: dict | None = None,
    cc_cache: dict | None = None,
    edmunds_cache: dict | None = None,
    fork_info: list[dict] | None = None,
    minify: bool = True,
    html_out: str | None = None,
) -> str:
    here = os.path.dirname(__file__)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
    template = env.get_template("template.html")
    model_mappings_json = json.dumps(
        {
            f"{make}|{model}": mapped
            for (make, model), mapped in FAMILY_MAPPINGS.items()
        },
        separators=(",", ":"),
    )
    autotrader_mappings_json = json.dumps(
        {
            f"{make}|{model}": mapped
            for (make, model), mapped in AUTOTRADER_MAPPINGS.items()
        },
        separators=(",", ":"),
    )
    rendered = template.render(
        cars_json=json.dumps(cars, separators=(",", ":")),
        cargurus_cache_json=json.dumps(
            {k: v for k, v in (cargurus_js_cache or {}).items() if v is not None},
            separators=(",", ":"),
        ),
        ari_cache_json=json.dumps(
            {k: v for k, v in (ari_cache or {}).items() if v is not None},
            separators=(",", ":"),
        ),
        cc_cache_json=json.dumps(
            {k: v for k, v in (cc_cache or {}).items() if v is not None},
            separators=(",", ":"),
        ),
        edmunds_cache_json=json.dumps(
            {k: v for k, v in (edmunds_cache or {}).items() if v is not None},
            separators=(",", ":"),
        ),
        model_mappings_json=model_mappings_json,
        autotrader_mappings_json=autotrader_mappings_json,
        filter_index_json=json.dumps(
            build_filter_index(cars, cc_cache or {}),
            separators=(",", ":"),
        ),
        fork_info_json=json.dumps(fork_info or [], separators=(",", ":")),
        alpine_js=fetch_asset(ALPINE_JS_URL, ALPINE_CACHE_FILE),
        pure_css=fetch_asset(PURE_CSS_URL, PURE_CSS_CACHE_FILE),
        # Use relative path from server root (same folder as HTML)
        favicon=f"{os.path.splitext(os.path.basename(html_out))[0]}-favicon.svg"
        if html_out
        else None,
    )
    if not minify:
        return rendered
    return minify_html.minify(rendered, minify_js=True, minify_css=True)


_reload_clients: list[queue.Queue] = []
_reload_lock = threading.Lock()

_RELOAD_SCRIPT = b'\n<script>(function(){var s=new EventSource("/reload");s.onmessage=function(){location.reload()}})()</script>\n'


def _notify_reload() -> None:
    with _reload_lock:
        clients = list(_reload_clients)
    for q in clients:
        try:
            q.put_nowait("reload")
        except Exception:
            pass


class _ReloadHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/reload":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with _reload_lock:
            _reload_clients.append(q)
        try:
            while True:
                try:
                    q.get(timeout=25)
                    self.wfile.write(b"data: reload\n\n")
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _reload_lock:
                try:
                    _reload_clients.remove(q)
                except ValueError:
                    pass

    def log_message(self, format, *args):
        if args and "/reload" in str(args[0]):
            return
        super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(
        description="Generate openpilot car support files."
    )
    parser.add_argument(
        "--html-out", default=None, help="Path for generated HTML file."
    )
    parser.add_argument(
        "--json-out", default=None, help="Path for generated JSON file."
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve the HTML file on a local HTTP server after building.",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for --serve (default: 8000)."
    )
    parser.add_argument(
        "--no-fetch-cg",
        action="store_true",
        help="Skip fetching CarGurus data for all cars.",
    )
    parser.add_argument(
        "--no-fetch-ari",
        action="store_true",
        help="Skip fetching Auto Reliability Index data for all cars.",
    )
    parser.add_argument(
        "--no-fetch-cc",
        action="store_true",
        help="Skip fetching CarComplaints data for all cars.",
    )
    parser.add_argument(
        "--no-fetch-edmunds",
        action="store_true",
        help="Skip fetching Edmunds price data.",
    )
    parser.add_argument(
        "--edmunds-no-headless",
        action="store_true",
        help="Launch Chromium with a visible window when fetching Edmunds data (useful for debugging).",
    )
    parser.add_argument(
        "--edmunds-firefox-cookies",
        action="store_true",
        help="Load Edmunds cookies from Firefox profile instead of visiting the homepage first.",
    )
    parser.add_argument(
        "--edmunds-sleep",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to sleep between Edmunds fetches (default: 15).",
    )
    parser.add_argument(
        "--edmunds-jitter",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Max random jitter added to each Edmunds sleep interval (default: 5).",
    )
    parser.add_argument(
        "--no-minify",
        action="store_true",
        help="Skip HTML/JS/CSS minification (useful for debugging).",
    )
    # Hidden: used by load_fork_cars() to isolate capnp schema loading per fork.
    parser.add_argument("--dump-fork", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-cache-openpilot",
        action="store_true",
        help="Force re-fetching openpilot data for all forks (disable caching).",
    )
    parser.add_argument(
        "--retry-nulls-cg",
        action="store_true",
        help="Re-fetch CarGurus cached entries whose stored value is null.",
    )
    parser.add_argument(
        "--retry-nulls-ari",
        action="store_true",
        help="Re-fetch ARI cached entries whose stored value is null.",
    )
    parser.add_argument(
        "--retry-nulls-cc",
        action="store_true",
        help="Re-fetch CarComplaints cached entries whose stored value is null.",
    )
    parser.add_argument(
        "--retry-nulls-edmunds",
        action="store_true",
        help="Re-fetch Edmunds cached entries whose stored value is null.",
    )
    parser.add_argument(
        "--retry-nulls-all",
        action="store_true",
        help="Re-fetch all cached entries whose stored value is null (implies --retry-nulls-cg/ari/cc).",
    )
    parser.add_argument(
        "--proxy",
        action="store_true",
        help="Route external requests through randomly-selected proxies from the proxifly free proxy list.",
    )
    parser.add_argument(
        "--ip-check-url",
        default=IP_CHECK_URL,
        metavar="URL",
        help=f'JSON endpoint returning {{"ip": ...}} used to verify proxy routing (default: {IP_CHECK_URL}).',
    )
    parser.add_argument(
        "--enumerate-cg",
        action="store_true",
        help="Fetch the full CarGurus make/model taxonomy and save to .cargurus_ids.json, then exit.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch template.html for changes and regenerate HTML automatically.",
    )
    args = parser.parse_args()
    if args.retry_nulls_all:
        args.retry_nulls_cg = args.retry_nulls_ari = args.retry_nulls_cc = (
            args.retry_nulls_edmunds
        ) = True

    if args.proxy:
        global _proxy_direct_ip, _proxy_ip_check_url
        _proxy_ip_check_url = args.ip_check_url
        _proxy_direct_ip = _fetch_ip(ip_check_url=args.ip_check_url)
        if _proxy_direct_ip is None:
            print(f"  Warning: could not reach {args.ip_check_url}.", file=sys.stderr)
        else:
            print(f"  Direct IP: {_proxy_direct_ip}", file=sys.stderr)
        all_candidates = load_proxy_list()
        cached = _load_verified_proxy_cache()
        if cached is not None:
            print(f"  Using {len(cached)} cached verified proxies.", file=sys.stderr)
            _ACTIVE_PROXIES.extend(cached)
            for p in cached:
                _READY_PROXIES.put(p)
            _proxy_pool_done.set()
        else:
            random.shuffle(all_candidates)
            _ACTIVE_PROXIES.extend(all_candidates)
            global _proxy_total_count
            _proxy_total_count = len(all_candidates)
            _start_proxy_pool(all_candidates)

    if args.enumerate_cg:
        print("Enumerating CarGurus make/model taxonomy...", file=sys.stderr)
        enumerate_cargurus_ids()
        return

    if args.dump_fork:
        print(json.dumps(_load_cars_directly(args.dump_fork)))
        return

    print("Loading car docs from forks...", file=sys.stderr)
    fork_car_lists = []
    fork_info = []
    for fork_name, fork_path in FORKS:
        fork_cars = load_fork_cars(
            fork_name, fork_path, use_cache=not args.no_cache_openpilot
        )
        if fork_cars is None:
            print(
                f"  Skipping {fork_name}: no supported opendbc layout found.",
                file=sys.stderr,
            )
            continue
        layout = (
            "new" if os.path.isdir(os.path.join(fork_path, "opendbc", "car")) else "old"
        )
        print(
            f"  Loading {fork_name} ({layout} layout)...",
            file=sys.stderr,
        )
        print(f"  Found {len(fork_cars)} cars in {fork_name}.", file=sys.stderr)
        fork_car_lists.append((fork_name, fork_cars))

    openpilot_cache = load_openpilot_cache()
    dirty = False
    fork_info = []
    for name, path in FORKS:
        if not any(n == name for n, _ in fork_car_lists):
            continue
        key = f"_git_{name}"
        if key not in openpilot_cache:
            openpilot_cache[key] = get_fork_git_info(name, path)
            dirty = True
        fork_info.append(openpilot_cache[key])
    if dirty:
        save_openpilot_cache(openpilot_cache)

    cars = merge_fork_cars(fork_car_lists)
    print(f"Total unique cars: {len(cars)}.", file=sys.stderr)

    def _fetch_cg():
        if not args.no_fetch_cg:
            print("Fetching CarGurus data...", file=sys.stderr)
            return fetch_cargurus_cache(cars, retry_nulls=args.retry_nulls_cg)
        return load_cargurus_cache()

    def _fetch_ari():
        if not args.no_fetch_ari:
            print("Fetching Auto Reliability Index data...", file=sys.stderr)
            return fetch_ari_cache(cars, retry_nulls=args.retry_nulls_ari)
        return load_ari_cache()

    def _fetch_cc():
        if not args.no_fetch_cc:
            print("Fetching CarComplaints data...", file=sys.stderr)
            return fetch_cc_cache(cars, retry_nulls=args.retry_nulls_cc)
        return load_cc_cache()

    def _fetch_edmunds():
        if not args.no_fetch_edmunds:
            print("Fetching Edmunds price data...", file=sys.stderr)
            return fetch_edmunds_cache(
                cars,
                retry_nulls=args.retry_nulls_edmunds,
                headless=not args.edmunds_no_headless,
                sleep=args.edmunds_sleep,
                jitter=args.edmunds_jitter,
                use_firefox_cookies=args.edmunds_firefox_cookies,
            )
        return load_edmunds_cache()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_cg = pool.submit(_fetch_cg)
        fut_ari = pool.submit(_fetch_ari)
        fut_cc = pool.submit(_fetch_cc)
        fut_edm = pool.submit(_fetch_edmunds)
        raw_cache = fut_cg.result()
        ari_cache = fut_ari.result()
        cc_cache = fut_cc.result()
        edmunds_cache = fut_edm.result()

    cargurus_js_cache = build_cargurus_js_cache(cars, raw_cache)
    warn_unmatched_cargurus(cars, load_cargurus_ids())

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(cars, f, indent=2)
        print(f"Written to {args.json_out}", file=sys.stderr)

    def _write_html():
        os.makedirs(os.path.dirname(os.path.abspath(args.html_out)), exist_ok=True)
        with open(args.html_out, "w") as f:
            f.write(
                generate_html(
                    cars,
                    cargurus_js_cache,
                    ari_cache,
                    cc_cache,
                    edmunds_cache,
                    fork_info=fork_info,
                    minify=not args.no_minify,
                    html_out=args.html_out,
                )
            )
        print(f"Written to {args.html_out}", file=sys.stderr)

    if args.html_out:
        _write_html()

        # Save favicon as a separate file next to the HTML
        favicon_svg = generate_favicon_svg()
        base_path = os.path.splitext(args.html_out)[0]
        favicon_path = f"{base_path}-favicon.svg"
        favicon_dir = os.path.dirname(favicon_path) or "."
        os.makedirs(favicon_dir, exist_ok=True)
        with open(favicon_path, "w") as f:
            f.write(favicon_svg)
        print(f"Written favicon to {favicon_path}", file=sys.stderr)

    if args.serve:
        if not args.html_out:
            print("Error: --serve requires --html-out", file=sys.stderr)
            sys.exit(1)

        handler = _ReloadHandler if args.watch else http.server.SimpleHTTPRequestHandler
        serve_dir = os.path.dirname(os.path.abspath(args.html_out))
        os.chdir(serve_dir)
        httpd = http.server.ThreadingHTTPServer(("", args.port), handler)
        print(f"Serving at http://localhost:{args.port}/", file=sys.stderr)
        if args.watch:
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
        else:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.", file=sys.stderr)
            return

    if args.watch:
        if not args.html_out:
            print("Error: --watch requires --html-out", file=sys.stderr)
            sys.exit(1)

        html_path = os.path.abspath(args.html_out)
        live_reload = args.serve

        def rebuild():
            _write_html()
            if live_reload:
                with open(html_path, "ab") as f:
                    f.write(_RELOAD_SCRIPT)
                _notify_reload()

        if live_reload:
            with open(html_path, "ab") as f:
                f.write(_RELOAD_SCRIPT)

        template_path = os.path.join(HERE, "template.html")
        last_mtime = os.path.getmtime(template_path)
        print(f"Watching {template_path} for changes...", file=sys.stderr)
        try:
            while True:
                time.sleep(0.5)
                mtime = os.path.getmtime(template_path)
                if mtime != last_mtime:
                    last_mtime = mtime
                    print("template.html changed, regenerating...", file=sys.stderr)
                    try:
                        rebuild()
                    except Exception as e:
                        print(f"Error: {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
