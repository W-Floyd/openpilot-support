#!/usr/bin/env python3
import argparse
import concurrent.futures
import html.parser
import http.server
import json
import math
import os
import queue
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


def cargurus_car_key(car: dict) -> str | None:
    years = sorted(set(car["years"]))
    if not years:
        return None
    make = to_ascii(car["make"])
    model = to_ascii(car["model"])
    return f"{make}|{model}|{years[0]}-{years[-1]}"


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


def cargurus_query(car: dict) -> str | None:
    years = sorted(set(car["years"]))
    if not years:
        return None
    make = to_ascii(car["make"])
    model = to_ascii(car["model"])
    return f"Make: {make}, Model: {model} {years[0]}-{years[-1]}"


def fetch_cargurus_response(query: str) -> dict | None:
    url = (
        f"https://www.cargurus.com/api/vehicle-discovery-service/v2/search/hybrid"
        f"?query={urllib.parse.quote(query)}&locale=en_US&format=SRP&origin=SRP&devicePlatform=DESKTOP"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
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


def fetch_cargurus_cache(cars: list[dict], retry_nulls: bool = False) -> dict:
    """Fetch CarGurus data for all cars, updating the cache file. Returns raw response cache."""
    cache = load_cargurus_cache()
    valid_queries = {q for car in cars if (q := cargurus_query(car)) is not None}
    stale = [k for k in cache if k not in valid_queries]
    if stale:
        for k in stale:
            del cache[k]
        save_cargurus_cache(cache)
    pending = [
        q
        for car in cars
        if (q := cargurus_query(car)) is not None
        and (q not in cache or (retry_nulls and cache[q] is None))
    ]
    total = len(pending)

    def fetch_one(query: str, idx: int) -> tuple[str, object]:
        print(f"  [{idx}/{total}] Fetching CarGurus: {query}", file=sys.stderr)
        return query, fetch_cargurus_response(query)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_one, q, i + 1): q for i, q in enumerate(pending)}
        for future in concurrent.futures.as_completed(futures):
            query, response = future.result()
            if isinstance(response, dict) and response.get("success") == "FAILURE":
                response = None
            cache[query] = response
            save_cargurus_cache(cache)
    return cache


def build_cargurus_js_cache(cars: list[dict], raw_cache: dict) -> dict:
    """Convert raw API response cache to JS-ready {carKey: {url}} or {carKey: {error}} map."""
    result = {}
    for car in cars:
        key = cargurus_car_key(car)
        query = cargurus_query(car)
        if key is None or query is None:
            continue
        response = raw_cache.get(query)
        if (
            response
            and response.get("success") != "FAILURE"
            and response.get("filterCriteria", {}).get("makeModelTrimPaths")
        ):
            result[key] = {"paths": response["filterCriteria"]["makeModelTrimPaths"]}
        elif query in raw_cache:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching '{url}': {e}", file=sys.stderr)
        return None

    parser = JsonLdExtractor()
    parser.feed(body)
    for block in parser.blocks:
        entity = block.get("mainEntity", {})
        review = entity.get("review", {})
        rating = review.get("reviewRating", {})
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

MODEL_MAPPINGS: dict[tuple[str, str], list[str]] = {
    ("Lexus", "CT Hybrid"): ["CT 200h"],
    ("Lexus", "ES Hybrid"): ["ES 300h"],
    ("Lexus", "ES"): ["ES 250", "ES 300", "ES 330", "ES 350", "ES 350f"],
    ("Lexus", "IS"): ["IS 200", "IS 250", "IS 250t", "IS 300", "IS 350", "IS 500"],
    ("Lexus", "LC Hybrid"): ["LC 500h"],
    ("Lexus", "LC"): ["LC 500"],
    ("Lexus", "LS Hybrid"): ["LS 500h", "LS 600h"],
    ("Lexus", "LS"): ["LS 400", "LS 430", "LS 460", "LS 500"],
    ("Lexus", "LX"): ["LX 470", "LX 570", "LX 600"],
    ("Lexus", "NX Hybrid"): ["NX 200h", "NX 350h", "NX 450h"],
    ("Lexus", "NX"): ["NX", "NX 200", "NX 200t", "NX200T", "NX 250", "NX 350"],
    ("Lexus", "RC Hybrid"): ["RC 300h"],
    ("Lexus", "RC"): ["RC 200t", "RC 300", "RC 350", "RC F"],
    ("Lexus", "RX Hybrid"): ["RX 400h", "RX 450h", "RX 450hL", "RX 500h"],
    ("Lexus", "RX"): ["RX 300", "RX 330", "RX 350", "RX 350L"],
    ("Lexus", "UX Hybrid"): ["UX 200h", "UX 250h"],
}

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
    for (make, _), variants in MODEL_MAPPINGS.items():
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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
            MODEL_MAPPINGS.get((car["make"], car["model"])) or [car["model"]]
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
            MODEL_MAPPINGS.get((car["make"], car["model"])) or [car["model"]]
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
        raw_models = MODEL_MAPPINGS.get((make, model)) or [model]
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
    fork_info: list[dict] | None = None,
    minify: bool = True,
    html_out: str | None = None,
) -> str:
    here = os.path.dirname(__file__)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
    template = env.get_template("template.html")
    model_mappings_json = json.dumps(
        {f"{make}|{model}": mapped for (make, model), mapped in MODEL_MAPPINGS.items()},
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
        "--retry-nulls-all",
        action="store_true",
        help="Re-fetch all cached entries whose stored value is null (implies --retry-nulls-cg/ari/cc).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch template.html for changes and regenerate HTML automatically.",
    )
    args = parser.parse_args()
    if args.retry_nulls_all:
        args.retry_nulls_cg = args.retry_nulls_ari = args.retry_nulls_cc = True

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

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        fut_cg = pool.submit(_fetch_cg)
        fut_ari = pool.submit(_fetch_ari)
        fut_cc = pool.submit(_fetch_cc)
        raw_cache = fut_cg.result()
        ari_cache = fut_ari.result()
        cc_cache = fut_cc.result()

    cargurus_js_cache = build_cargurus_js_cache(cars, raw_cache)

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
