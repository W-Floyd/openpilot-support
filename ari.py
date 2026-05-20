#!/usr/bin/env python3
import concurrent.futures
import html.parser
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

from car_data import to_ascii
from proxy import _urlopen_proxied

ARI_CACHE_FILE = os.path.join(HERE, ".ari_cache.json")


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
