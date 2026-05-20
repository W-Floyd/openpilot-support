#!/usr/bin/env python3
import concurrent.futures
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

from car_data import FAMILY_MAPPINGS, resolve_alias, to_ascii
from proxy import _urlopen_proxied

CARGURUS_CACHE_FILE = os.path.join(HERE, ".cargurus_cache.json")
CARGURUS_IDS_FILE = os.path.join(HERE, ".cargurus_ids.json")

_CG_TRIVIAL_PACKAGES = {"All", "Any", ""}


def _cg_model(model: str) -> str:
    """Strip trailing parenthetical from model name for CarGurus lookups."""
    import re
    return re.sub(r"\s*\([^)]*\)\s*$", "", model).strip()


def cargurus_car_key(car: dict) -> str | None:
    years = sorted(set(car["years"]))
    if not years:
        return None
    make = to_ascii(car["make"])
    model = to_ascii(car["model"])
    pkg = car.get("package", "")
    suffix = f"|{pkg}" if pkg not in _CG_TRIVIAL_PACKAGES else ""
    return f"{make}|{model}|{years[0]}-{years[-1]}{suffix}"


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
