#!/usr/bin/env python3
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
JDPOWER_CACHE_FILE = os.path.join(HERE, ".jdpower_cache.json")
JDPOWER_HTML_CACHE_DIR = os.path.join(HERE, ".jdpower_html_cache")


def _jdp_slug(name: str) -> str:
    """Convert a make/model name to a JD Power URL slug (lowercase, non-alnum → hyphen)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).lower().strip("-")
    return s


def jdpower_key(make: str, model: str, year: int) -> str:
    import unicodedata
    def ascii(s):
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return f"{ascii(make)}|{ascii(model)}|{year}"


def _jdp_html_cache_path(make: str, model: str, year: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{make}_{model}_{year}".lower()).strip("_")
    return os.path.join(JDPOWER_HTML_CACHE_DIR, f"{slug}.html")


def _load_jdp_html_cache(make: str, model: str, year: int) -> str | None:
    path = _jdp_html_cache_path(make, model, year)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def _save_jdp_html_cache(make: str, model: str, year: int, html: str) -> None:
    os.makedirs(JDPOWER_HTML_CACHE_DIR, exist_ok=True)
    with open(_jdp_html_cache_path(make, model, year), "w", encoding="utf-8") as f:
        f.write(html)


def _extract_jdp_prices(html: str, url: str) -> dict | None:
    m = re.search(r"AggregateOffer", html)
    if not m:
        return None
    window = html[m.start():m.start() + 300]
    lo = re.search(r'lowPrice[\\\"]+\s*:\s*(\d+)', window)
    hi = re.search(r'highPrice[\\\"]+\s*:\s*(\d+)', window)
    if not lo or not hi:
        return None
    return {
        "min": int(lo.group(1)),
        "max": int(hi.group(1)),
        "url": url,
        "lastUpdated": time.time(),
    }


def _fetch_jdpower_entry(make: str, model: str, year: int) -> dict | None:
    url = f"https://www.jdpower.com/cars/{year}/{_jdp_slug(make)}/{_jdp_slug(model)}"
    cached = _load_jdp_html_cache(make, model, year)
    if cached:
        return _extract_jdp_prices(cached, url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="ignore")
        _save_jdp_html_cache(make, model, year, html)
        return _extract_jdp_prices(html, url)
    except urllib.error.HTTPError:
        return None
    except Exception as e:
        print(f"  Error fetching JD Power {make} {model} {year}: {e}", file=sys.stderr)
        return None


def load_jdpower_cache() -> dict:
    if os.path.exists(JDPOWER_CACHE_FILE):
        try:
            with open(JDPOWER_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_jdpower_cache(cache: dict) -> None:
    with open(JDPOWER_CACHE_FILE, "w") as f:
        json.dump(dict(sorted(cache.items())), f, indent=2)


def fetch_jdpower_cache(cars: list[dict], retry_nulls: bool = False) -> dict:
    cache = load_jdpower_cache()

    valid_keys = {
        jdpower_key(car["make"], car["model"], y)
        for car in cars
        for y in set(car["years"])
    }
    for k in list(cache.keys()):
        if k not in valid_keys:
            del cache[k]

    to_fetch = [
        (car["make"], car["model"], year, jdpower_key(car["make"], car["model"], year))
        for car in cars
        for year in sorted(set(car["years"]))
        if (key := jdpower_key(car["make"], car["model"], year))
        and (key not in cache or (retry_nulls and cache[key] is None))
    ]

    if not to_fetch:
        return cache

    print(f"  Fetching {len(to_fetch)} JD Power entries...", file=sys.stderr)

    def fetch_one(args):
        make, model, year, key = args
        return key, _fetch_jdpower_entry(make, model, year)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_one, args): args for args in to_fetch}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            key, entry = fut.result()
            cache[key] = entry
            done += 1
            if done % 100 == 0:
                print(f"  JD Power: {done}/{len(to_fetch)}", file=sys.stderr)
                save_jdpower_cache(cache)

    save_jdpower_cache(cache)
    return cache
