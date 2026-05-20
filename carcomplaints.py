#!/usr/bin/env python3
import concurrent.futures
import html.parser
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

from car_data import AUTOTRADER_MODELS_CACHE_FILE, FAMILY_MAPPINGS, to_ascii
from proxy import _urlopen_proxied

CC_CACHE_FILE = os.path.join(HERE, ".carcomplaints_cache.json")
CC_HTML_CACHE_DIR = os.path.join(HERE, ".carcomplaints_html_cache")

CC_SEALS = {
    "best.png": "Seal of Awesome",
    "good.png": "Seal of Pretty Good",
    "bad.png": "Beware of the Clunker",
    "worst.png": "Avoid Like The Plague",
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


def _cc_html_cache_path(make: str, raw_model: str, year: int) -> str:
    slug = to_ascii(f"{make}_{raw_model}_{year}").replace(" ", "_").lower()
    return os.path.join(CC_HTML_CACHE_DIR, f"{slug}.html")


def _load_cc_html_cache(make: str, raw_model: str, year: int) -> str | None:
    path = _cc_html_cache_path(make, raw_model, year)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def _save_cc_html_cache(make: str, raw_model: str, year: int, html: str) -> None:
    os.makedirs(CC_HTML_CACHE_DIR, exist_ok=True)
    with open(_cc_html_cache_path(make, raw_model, year), "w", encoding="utf-8") as f:
        f.write(html)


def _parse_cc_html(body: str, url: str) -> dict | None:
    p = CcParser()
    p.feed(body)

    complaints = _parse_cc_count(p.counts.get("prbNav", ""))
    recalls = _parse_cc_count(p.counts.get("rclNav", ""))
    tsbs = _parse_cc_count(p.counts.get("tsbNav", ""))
    investigations = _parse_cc_count(p.counts.get("invNav", ""))

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


def fetch_cc_response(make: str, raw_model: str, year: int) -> dict | None:
    url = cc_url(make, raw_model, year)
    cached = _load_cc_html_cache(make, raw_model, year)
    if cached:
        return _parse_cc_html(cached, url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen_proxied(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Error fetching '{url}': {e}", file=sys.stderr)
        return None
    _save_cc_html_cache(make, raw_model, year, body)
    return _parse_cc_html(body, url)


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
