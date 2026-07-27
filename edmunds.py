#!/usr/bin/env python3
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from car_data import FAMILY_MAPPINGS, to_ascii
from proxy import (
    _BrowserDied,
    _ProxyBlocked,
    _ACTIVE_PROXIES,
    _find_next_proxy,
    _load_proxy_blacklist,
    _proxy_direct_ip,
    _save_proxy_blacklist,
)

EDMUNDS_CACHE_FILE = os.path.join(HERE, ".edmunds_cache.json")
EDMUNDS_HTML_CACHE_DIR = os.path.join(HERE, ".edmunds_html_cache")
AUTOTRADER_MODELS_CACHE_FILE = os.path.join(HERE, ".autotrader_models_cache.json")


def edmunds_slug(text: str) -> str:
    s = to_ascii(text).lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s)


def _cg_model(model: str) -> str:
    """Strip trailing parenthetical from model name for lookups."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", model).strip()


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


def _edmunds_html_cache_path(make: str, model: str, year: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{make}_{model}_{year}".lower()).strip("_")
    return os.path.join(EDMUNDS_HTML_CACHE_DIR, f"{slug}.html")


def _load_edmunds_html_cache(make: str, model: str, year: int) -> str | None:
    path = _edmunds_html_cache_path(make, model, year)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def _save_edmunds_html_cache(make: str, model: str, year: int, html: str) -> None:
    os.makedirs(EDMUNDS_HTML_CACHE_DIR, exist_ok=True)
    path = _edmunds_html_cache_path(make, model, year)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


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


async def _try_edmunds_candidate(
    make: str, candidate: str, year: int, page,
    cached_html: str | None = None,
    save_html_as: str | None = None,
) -> dict | None:
    """Fetch one candidate URL. Returns entry dict, None if not found, or raises on hard errors.

    save_html_as: model name to use when writing the HTML cache (defaults to candidate).
    """
    import asyncio

    url = edmunds_url(make, candidate, year)
    html_model = save_html_as or candidate
    try:
        if cached_html:
            await page.set_content(cached_html, wait_until="domcontentloaded")
            actual_title = await page.title()
            print(f"    car page: (cached HTML), title={actual_title!r}", file=sys.stderr)
            if not actual_title or "page not found" in actual_title.lower():
                return None
        else:
            response = await page.goto(url, timeout=12000, wait_until="domcontentloaded")
            actual_title = await page.title()
            if not actual_title:
                try:
                    await page.wait_for_function("document.title !== ''", timeout=3000)
                    actual_title = await page.title()
                except Exception:
                    pass
            print(f"    car page: HTTP {response.status if response else '?'}, title={actual_title!r}, url={page.url}", file=sys.stderr)
            if response and response.status == 404:
                return None
            if response and response.status >= 400:
                title = await page.title()
                headers = dict(response.headers)
                reported_ip = None
                if response.status == 403:
                    try:
                        body = await page.inner_text("body", timeout=3000)
                        m = re.search(r"IP\s*(?:Address)?[:\s]+(\d{1,3}(?:\.\d{1,3}){3})", body, re.IGNORECASE)
                        if m:
                            reported_ip = m.group(1)
                    except Exception:
                        pass
                print(
                    f"  HTTP {response.status} fetching Edmunds {make} {candidate} {year}\n"
                    f"    title: {title!r}\n"
                    f"    url: {page.url}\n"
                    + (f"    reported IP: {reported_ip}{' (matches direct — proxy not routing)' if reported_ip == _proxy_direct_ip else ''}\n" if reported_ip else "")
                    + f"    headers:{ {k: v for k, v in headers.items() if k.lower() in ('server','cf-ray','x-amz-cf-id','x-cache','via','location','content-type','x-powered-by')} }",
                    file=sys.stderr,
                )
                raise _ProxyBlocked(blacklist=True)
            if not actual_title or "page not found" in actual_title.lower():
                return None
        if not cached_html:
            await page.mouse.wheel(0, 600)
            await page.mouse.wheel(0, 600)

        async def _get_price_range() -> str | None:
            loc = page.locator("text=Price Range:").or_(page.locator("text=Price:")).first
            await loc.wait_for(timeout=15000)
            return await loc.text_content(timeout=5000)

        async def _check_not_found() -> None:
            await page.locator("text=Page Not Found").first.wait_for(timeout=15000)

        async def _race_details() -> tuple:
            t_sug = asyncio.create_task(
                _edmunds_single_price(page, "Edmunds Suggested Price", "Edmunds suggests you pay", timeout=8000)
            )
            t_avg = asyncio.create_task(
                _edmunds_single_price(page, "Average price", timeout=8000)
            )
            suggested = avg_used = None
            pending = {t_sug, t_avg}
            try:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        val = None if task.exception() else task.result()
                        if task is t_sug:
                            suggested = val
                        else:
                            avg_used = val
                        if val is not None:
                            for p in pending:
                                p.cancel()
                            return suggested, avg_used
                return suggested, avg_used
            finally:
                for t in (t_sug, t_avg):
                    t.cancel()
                await asyncio.gather(t_sug, t_avg, return_exceptions=True)

        t_price = asyncio.create_task(_get_price_range())
        t_details = asyncio.create_task(_race_details())
        t_notfound = asyncio.create_task(_check_not_found())

        try:
            done, _ = await asyncio.wait({t_price, t_notfound}, return_when=asyncio.FIRST_COMPLETED)
            if t_notfound in done and not t_notfound.exception():
                return None
            t_notfound.cancel()

            if t_price not in done:
                await asyncio.wait({t_price})
            price_str = None if t_price.exception() else t_price.result()

            try:
                suggested, avg_used = await t_details
            except Exception:
                suggested = avg_used = None
        finally:
            for t in (t_price, t_details, t_notfound):
                t.cancel()
            await asyncio.gather(t_price, t_details, t_notfound, return_exceptions=True)

        entry = _parse_edmunds_price(price_str or "")
        if not entry and avg_used is None:
            return None
        if not entry:
            entry = {}
        entry["url"] = url if cached_html else page.url
        entry["suggested"] = None if isinstance(suggested, Exception) else suggested
        entry["avg_used"] = None if isinstance(avg_used, Exception) else avg_used
        entry["lastUpdated"] = time.time()
        if not cached_html:
            _save_edmunds_html_cache(make, html_model, year, await page.content())
        return entry
    except _ProxyBlocked:
        raise
    except Exception as e:
        err = str(e)
        if "Target page, context or browser has been closed" in err or "Connection closed" in err:
            raise _BrowserDied()
        if "Timeout" in err and ("goto" in err or "navigation" in err):
            raise _ProxyBlocked(blacklist=True)
        if "NS_ERROR_PROXY" in err or "NS_ERROR_CONNECTION_REFUSED" in err or "NS_ERROR_NET_RESET" in err:
            raise _ProxyBlocked(blacklist=True)
        print(f"  Error fetching Edmunds {make} {candidate} {year}: {e}", file=sys.stderr)
        return None


async def _fetch_edmunds_price_with_page(
    make: str, model: str, year: int, page
) -> dict | None:
    # If we have cached HTML for this model, use it directly (no candidate iteration needed).
    cached_html = _load_edmunds_html_cache(make, model, year)
    if cached_html:
        return await _try_edmunds_candidate(make, model, year, page, cached_html=cached_html)

    # Build candidate list: original model name first, then family mapping variants.
    candidates: list[str] = [model]
    for variant in (FAMILY_MAPPINGS.get((make, model)) or []):
        if variant not in candidates:
            candidates.append(variant)

    for candidate in candidates:
        if candidate != model:
            print(f"    trying family variant: {candidate!r}", file=sys.stderr)
        result = await _try_edmunds_candidate(make, candidate, year, page, save_html_as=model)
        if result is not None:
            return result
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
    MAX_HOMEPAGE_BLOCKS = 5
    MAX_LAUNCH_FAILURES = 5
    MAX_DRIVER_RESTARTS = 5
    total = len(pending)
    idx = 0
    iter_times: list[float] = []
    retries: dict[int, int] = {}
    blacklist: set[str] = _load_proxy_blacklist()
    proxy: object = None
    homepage_blocks = 0
    launch_failures = 0
    driver_restarts = 0
    give_up = False

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

    # Outer loop restarts the playwright driver when it crashes.
    while idx < total and not give_up:
        async with async_playwright() as p:
            if proxy is None:
                proxy = await asyncio.to_thread(_find_next_proxy, blacklist) if _ACTIVE_PROXIES else None
                if _ACTIVE_PROXIES and proxy is None:
                    raise SystemExit("error: no proxies available — all exhausted or blacklisted.")
            browser: object = None
            ctx: object = None
            page: object = None
            playwright_alive = True

            async def _launch_browser() -> None:
                nonlocal browser, ctx, page
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
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
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser = ctx = page = None
                proxy = (
                    await asyncio.to_thread(_find_next_proxy, blacklist) if _ACTIVE_PROXIES else None
                )
                if _ACTIVE_PROXIES and proxy is None:
                    raise SystemExit("error: no proxies remaining — all exhausted or blacklisted.")

            while idx < total and playwright_alive:
                if browser is None:
                    try:
                        await _launch_browser()
                    except Exception as e:
                        err = str(e)
                        if "Connection closed" in err:
                            driver_restarts += 1
                            playwright_alive = False
                            if driver_restarts >= MAX_DRIVER_RESTARTS:
                                print(
                                    f"  Playwright driver died {driver_restarts} times in a row ({e})"
                                    f" — giving up, {total - idx} entries left unfetched",
                                    file=sys.stderr,
                                )
                                give_up = True
                            else:
                                print(
                                    f"  Playwright driver died ({e}) — restarting"
                                    f" ({driver_restarts}/{MAX_DRIVER_RESTARTS})",
                                    file=sys.stderr,
                                )
                            break
                        launch_failures += 1
                        if launch_failures >= MAX_LAUNCH_FAILURES:
                            print(
                                f"  Browser launch failed {launch_failures} times in a row ({e})"
                                f" — giving up, {total - idx} entries left unfetched",
                                file=sys.stderr,
                            )
                            give_up = True
                            break
                        print(f"  Browser launch failed ({e}) — rotating", file=sys.stderr)
                        await _rotate(_ProxyBlocked(blacklist=True))
                        continue
                    launch_failures = 0
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
                            print(f"  Homepage blocked", file=sys.stderr)
                    except Exception as e:
                        print(f"  Homepage failed ({e}) — rotating", file=sys.stderr)
                        homepage_ok = False
                    if not homepage_ok:
                        homepage_blocks += 1
                        if homepage_blocks >= MAX_HOMEPAGE_BLOCKS:
                            print(
                                f"  Edmunds blocked the homepage {homepage_blocks} times in a row"
                                f" — giving up, {total - idx} entries left unfetched",
                                file=sys.stderr,
                            )
                            give_up = True
                            break
                        backoff = min(60.0, 5.0 * 2 ** (homepage_blocks - 1))
                        print(
                            f"  Homepage retry {homepage_blocks}/{MAX_HOMEPAGE_BLOCKS}"
                            f" — waiting {backoff:.0f}s",
                            file=sys.stderr,
                        )
                        await _rotate()
                        await asyncio.sleep(backoff)
                        continue
                    homepage_blocks = 0

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
                    driver_restarts = 0
                except _ProxyBlocked as exc:
                    await _rotate(exc)
                except _BrowserDied:
                    print("  Browser died — skipping", file=sys.stderr)
                    cache[edmunds_cache_key(make, model, year)] = None
                    save_edmunds_cache(cache)
                    idx += 1
                    playwright_alive = False
                except Exception as e:
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
                try:
                    await browser.close()
                except Exception:
                    pass
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
