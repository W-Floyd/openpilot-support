#!/usr/bin/env python3
import argparse
import concurrent.futures
import html.parser
import http.server
import json
import math
import os
import subprocess
import sys
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
    ("opgm", os.path.join(HERE, "opgm", "opendbc_repo")),
    ("BMW-E8x-E9x", os.path.join(HERE, "BMW-E8x-E9x", "opendbc_repo")),
    ("StarPilot", os.path.join(HERE, "StarPilot", "opendbc_repo")),
]

OPENPILOT_CACHE_FILE = os.path.join(HERE, ".openpilot_cache.json")
CARGURUS_CACHE_FILE = os.path.join(HERE, ".cargurus_cache.json")


def _extract_years_from_model(model: str) -> list[int]:
    """Extract year range from model name when years field is not available."""
    import re

    pattern = r"\d{4}-\d{2}|\d{4}"
    matches = re.findall(pattern, model)
    if not matches:
        return []

    years: list[int] = []
    for match in matches:
        years.extend(parse_years(match))
    return sorted(set(years))


def _remove_years_from_model(car_docs) -> str:
    """Strip year range from model name."""
    import re

    if car_docs.years:
        return car_docs.model
    else:
        pattern = r"(\s*20\d{2}(-20\d{2}|-\d{2})?)"
        return re.sub(pattern, "", car_docs.model).strip()


from dataclasses import dataclass


@dataclass
class MockCarDocs:
    model: str
    years: str = ""


def test_remove_years_from_model() -> None:
    """Test the _remove_years_from_model function."""
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
        result = _remove_years_from_model(car_docs)
        assert result == expected, (
            f"Failed for {car_docs.model!r}: got {result!r}, expected {expected!r}"
        )

    print("All tests passed!")


def test_extract_years_from_model() -> None:
    """Test the _extract_years_from_model function."""
    test_cases = [
        # (input_model, expected_years)
        ("Suburban Premier 2016-20", [2016, 2017, 2018, 2019, 2020]),
        ("Silverado 2020-21", [2020, 2021]),
        ("Civic LX 2019", [2019]),
        ("no years here", []),
        ("2016-2020 Edition", [2016, 2017, 2018, 2019, 2020]),
        ("Accord 2022-24", [2022, 2023, 2024]),
        ("CR-V 2015", [2015]),
        ("Explorer 2020-23, 2024", [2020, 2021, 2022, 2023, 2024]),
        ("RAV4 2022-23", [2022, 2023]),
        ("Malibu Hybrid 2017 - No-ACC", [2017]),
        ("Malibu 2017-19 ASCM Harness", [2017, 2018, 2019]),
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
    from opendbc.car.docs_definitions import Column, ExtraCarsColumn, Star

    row = car_docs.row

    def star_to_bool(val) -> bool | None:
        if isinstance(val, Star):
            return val == Star.FULL
        return None

    return {
        "make": car_docs.make,
        "model": _remove_years_from_model(car_docs),
        "years": _extract_years_from_model(car_docs.model)
        if not parse_years(car_docs.years)
        else parse_years(car_docs.years),
        "name": car_docs.name,
        "package": car_docs.package,
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
        "model": _remove_years_from_model(car_docs),
        "years": _extract_years_from_model(car_docs.model)
        if not parse_years(car_docs.years)
        else parse_years(car_docs.years),
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

        class _OpenpilotMockFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, _path, _target=None):
                if fullname == "openpilot" or fullname.startswith("openpilot."):
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
    cache = None
    if use_cache:
        cache = load_openpilot_cache()
        if fork_name in cache:
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

    if use_cache and cache is not None:
        cache[fork_name] = cars
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
                and b["openpilot_longitudinal"] == "openpilot supported"
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

    return list(merged.values())


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


def build_cargurus_url(fc: dict) -> str:
    paths = ",".join(fc["makeModelTrimPaths"])
    return (
        f"https://www.cargurus.com/search?sourceContext=carSelectorAPI"
        f"&sortDirection=ASC&sortType=BEST_MATCH"
        f"&startYear={fc['startYear']}&endYear={fc['endYear']}"
        f"&srpVariation=DEFAULT_SEARCH"
        f"&makeModelTrimPaths={urllib.parse.quote(paths, safe='')}"
        f"&priceDropsOnly=false&hideNationwideShipping=true"
    )


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


def fetch_cargurus_cache(cars: list[dict]) -> dict:
    """Fetch CarGurus data for all cars, updating the cache file. Returns raw response cache."""
    cache = load_cargurus_cache()
    pending = [
        q for car in cars if (q := cargurus_query(car)) is not None and q not in cache
    ]
    total = len(pending)

    def fetch_one(query: str, idx: int) -> tuple[str, object]:
        print(f"  [{idx}/{total}] Fetching CarGurus: {query}", file=sys.stderr)
        return query, fetch_cargurus_response(query)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_one, q, i + 1): q for i, q in enumerate(pending)}
        for future in concurrent.futures.as_completed(futures):
            query, response = future.result()
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
            result[key] = {"url": build_cargurus_url(response["filterCriteria"])}
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


def fetch_ari_cache(cars: list[dict]) -> dict:
    """Fetch ARI data for all car/year combinations, updating the cache file."""
    cache = load_ari_cache()
    pending = [
        (car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
        if ari_cache_key(car["make"], car["model"], year) not in cache
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


def fetch_cc_cache(cars: list[dict]) -> dict:
    """Fetch CarComplaints data for all car/year combinations, updating the cache file."""
    cache = load_cc_cache()
    pending = [
        (car["make"], raw_model, year)
        for car in cars
        for year in sorted(set(car["years"]))
        for raw_model in (
            MODEL_MAPPINGS.get((car["make"], car["model"])) or [car["model"]]
        )
        if cc_cache_key(car["make"], raw_model, year) not in cache
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
TAILWIND_JS_URL = "https://cdn.tailwindcss.com"
ALPINE_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".alpine_cache.js")
TAILWIND_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".tailwind_cache.js")


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


def generate_html(
    cars: list[dict],
    cargurus_js_cache: dict | None = None,
    ari_cache: dict | None = None,
    cc_cache: dict | None = None,
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
        alpine_js=fetch_asset(ALPINE_JS_URL, ALPINE_CACHE_FILE),
        tailwind_js=fetch_asset(TAILWIND_JS_URL, TAILWIND_CACHE_FILE),
        # Use relative path from server root (same folder as HTML)
        favicon=f"{os.path.splitext(os.path.basename(html_out))[0]}-favicon.svg"
        if html_out
        else None,
    )
    if not minify:
        return rendered
    return minify_html.minify(rendered, minify_js=True, minify_css=True)


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
    args = parser.parse_args()

    if args.dump_fork:
        print(json.dumps(_load_cars_directly(args.dump_fork)))
        return

    print("Loading car docs from forks...", file=sys.stderr)
    fork_car_lists = []
    for fork_name, fork_path in FORKS:
        if not args.no_cache_openpilot:
            cache = load_openpilot_cache()
            if fork_name in cache:
                fork_cars = cache[fork_name]
                print(f"  Using cached {fork_name} data...", file=sys.stderr)
                print(f"  Found {len(fork_cars)} cars in {fork_name}.", file=sys.stderr)
                fork_car_lists.append((fork_name, fork_cars))
                continue

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

    cars = merge_fork_cars(fork_car_lists)
    print(f"Total unique cars: {len(cars)}.", file=sys.stderr)

    if not args.no_fetch_cg:
        print("Fetching CarGurus data...", file=sys.stderr)
        raw_cache = fetch_cargurus_cache(cars)
    else:
        raw_cache = load_cargurus_cache()

    cargurus_js_cache = build_cargurus_js_cache(cars, raw_cache)

    if not args.no_fetch_ari:
        print("Fetching Auto Reliability Index data...", file=sys.stderr)
        ari_cache = fetch_ari_cache(cars)
    else:
        ari_cache = load_ari_cache()

    if not args.no_fetch_cc:
        print("Fetching CarComplaints data...", file=sys.stderr)
        cc_cache = fetch_cc_cache(cars)
    else:
        cc_cache = load_cc_cache()

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(cars, f, indent=2)
        print(f"Written to {args.json_out}", file=sys.stderr)

    if args.html_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.html_out)), exist_ok=True)
        with open(args.html_out, "w") as f:
            f.write(
                generate_html(
                    cars,
                    cargurus_js_cache,
                    ari_cache,
                    cc_cache,
                    minify=not args.no_minify,
                    html_out=args.html_out,
                )
            )
        print(f"Written to {args.html_out}", file=sys.stderr)

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

        serve_dir = os.path.dirname(os.path.abspath(args.html_out))
        os.chdir(serve_dir)
        with http.server.HTTPServer(
            ("", args.port), http.server.SimpleHTTPRequestHandler
        ) as httpd:
            print(f"Serving at http://localhost:{args.port}/", file=sys.stderr)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
