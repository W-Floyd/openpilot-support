#!/usr/bin/env python3
import argparse
import concurrent.futures
import html.parser
import http.server
import json
import math
import os
import sys
import unicodedata
import urllib.parse
import urllib.request

import jinja2
import minify_html

# Add opendbc to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "openpilot", "opendbc_repo"))

from opendbc.car.docs import get_all_car_docs
from opendbc.car.docs_definitions import Column, ExtraCarsColumn, Star

CARGURUS_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".cargurus_cache.json")


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
    row = car_docs.row

    def star_to_bool(val) -> bool | None:
        if isinstance(val, Star):
            return val == Star.FULL
        return None

    return {
        "make": car_docs.make,
        "model": car_docs.model,
        "years": parse_years(car_docs.years),
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


def cargurus_car_key(car: dict) -> str | None:
    years = sorted(set(car["years"]))
    if not years:
        return None
    make = to_ascii(car["make"])
    model = to_ascii(car["model"])
    return f"{make}|{model}|{years[0]}-{years[-1]}"


def to_ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


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

def _model_range(make: str, model: str, start: int, end: int, mapped: str) -> dict[tuple[str, str, int], str]:
    return {(make, model, year): mapped for year in range(start, end + 1)}


MODEL_MAPPINGS: dict[tuple[str, str, int], str] = {
    **_model_range("Lexus", "ES", 1989, 1990, "ES250"),
    **_model_range("Lexus", "ES", 1991, 2002, "ES300"),
    **_model_range("Lexus", "ES", 2003, 2006, "ES330"),
    **_model_range("Lexus", "ES", 2007, 2026, "ES350"),
    **_model_range("Lexus", "RX", 1998, 2003, "RX300"),
    # RX 300 and RX 330 lived at the same time, 2 engine sizes
    **_model_range("Lexus", "RX", 2007, 2026, "RX350"),
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


def cc_url(make: str, model: str, year: int) -> str:
    raw_model = MODEL_MAPPINGS.get((make, model, year), model)
    return f"https://www.carcomplaints.com/{cc_slug(make)}/{cc_slug(raw_model)}/{year}/"


def cc_cache_key(make: str, model: str, year: int) -> str:
    return f"{to_ascii(make)}|{to_ascii(model)}|{year}"


def fetch_cc_response(make: str, model: str, year: int) -> dict | None:
    url = cc_url(make, model, year)
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
        (car["make"], car["model"], year)
        for car in cars
        for year in sorted(set(car["years"]))
        if cc_cache_key(car["make"], car["model"], year) not in cache
    ]
    total = len(pending)

    def fetch_one(entry: tuple[str, str, int], idx: int) -> tuple[str, object]:
        make, model, year = entry
        print(
            f"  [{idx}/{total}] Fetching CarComplaints: {make} {model} {year}",
            file=sys.stderr,
        )
        return cc_cache_key(make, model, year), fetch_cc_response(make, model, year)

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


def generate_html(
    cars: list[dict],
    cargurus_js_cache: dict | None = None,
    ari_cache: dict | None = None,
    cc_cache: dict | None = None,
) -> str:
    here = os.path.dirname(__file__)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
    template = env.get_template("template.html")
    model_mappings_json = json.dumps(
        {f"{make}|{model}|{year}": mapped for (make, model, year), mapped in MODEL_MAPPINGS.items()},
        separators=(",", ":"),
    )
    rendered = template.render(
        cars_json=json.dumps(cars, separators=(",", ":")),
        cargurus_cache_json=json.dumps(cargurus_js_cache or {}, separators=(",", ":")),
        ari_cache_json=json.dumps(ari_cache or {}, separators=(",", ":")),
        cc_cache_json=json.dumps(cc_cache or {}, separators=(",", ":")),
        model_mappings_json=model_mappings_json,
    )
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
        "--no-fetch-cargurus",
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
    args = parser.parse_args()

    print("Loading car docs...", file=sys.stderr)
    all_car_docs = get_all_car_docs()
    print(f"Found {len(all_car_docs)} cars.", file=sys.stderr)

    cars = [car_docs_to_dict(cd) for cd in all_car_docs]

    if not args.no_fetch_cargurus:
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
            f.write(generate_html(cars, cargurus_js_cache, ari_cache, cc_cache))
        print(f"Written to {args.html_out}", file=sys.stderr)

    if args.serve:
        if not args.html_out:
            print("Error: --serve requires --html-out", file=sys.stderr)
            sys.exit(1)

        serve_dir = os.path.dirname(os.path.abspath(args.html_out))
        serve_file = os.path.basename(args.html_out)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                if path == "/":
                    path = f"/{serve_file}"
                return super().translate_path(path)

            def log_message(self, format, *a):
                print(format % a, file=sys.stderr)

        os.chdir(serve_dir)
        with http.server.HTTPServer(("", args.port), Handler) as httpd:
            print(f"Serving at http://localhost:{args.port}/", file=sys.stderr)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
