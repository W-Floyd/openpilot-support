#!/usr/bin/env python3
import contextlib
import json
import os
import queue
import random
import sys
import threading
import time
import urllib.request

import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))

PROXIFLY_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.json"
PROXIFLY_CACHE_FILE = os.path.join(HERE, ".proxifly_cache.json")
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


class _BrowserDied(Exception):
    pass


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
    proxies = [p["proxy"] for p in raw if p.get("protocol") in ("http", "https", "socks4", "socks5")]
    print(f"  Loaded {len(proxies)} proxies.", file=sys.stderr)
    return proxies


def _fetch_ip(
    proxy: str | None = None, ip_check_url: str = IP_CHECK_URL, timeout: int = 5
) -> str | None:
    import requests
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = requests.get(
            ip_check_url, headers={"User-Agent": "Mozilla/5.0"},
            proxies=proxies, timeout=timeout,
        )
        return resp.json()["ip"]
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


@contextlib.contextmanager
def _urlopen_proxied(req, timeout: int = 10):
    import io
    import requests
    proxy = _pick_proxy()
    url = req.full_url if hasattr(req, "full_url") else req
    headers = dict(req.headers) if hasattr(req, "headers") else {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
    resp.raise_for_status()
    yield io.BytesIO(resp.content)
