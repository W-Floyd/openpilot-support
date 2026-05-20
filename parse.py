#!/usr/bin/env python3
import argparse
import concurrent.futures
import http.server
import json
import os
import random
import sys
import threading
import time

from proxy import (
    IP_CHECK_URL,
    _ACTIVE_PROXIES,
    _READY_PROXIES,
    _fetch_ip,
    _load_verified_proxy_cache,
    _proxy_pool_done,
    _start_proxy_pool,
    load_proxy_list,
)
from car_data import (
    FORKS,
    _load_cars_directly,
    get_fork_git_info,
    load_fork_cars,
    load_openpilot_cache,
    merge_fork_cars,
    save_openpilot_cache,
)
from cargurus import (
    build_cargurus_js_cache,
    enumerate_cargurus_ids,
    fetch_cargurus_cache,
    load_cargurus_cache,
    load_cargurus_ids,
    warn_unmatched_cargurus,
)
from ari import (
    fetch_ari_cache,
    load_ari_cache,
)
from edmunds import (
    fetch_edmunds_cache,
    load_edmunds_cache,
)
from jdpower import (
    fetch_jdpower_cache,
    load_jdpower_cache,
)
from carcomplaints import (
    fetch_cc_cache,
    load_cc_cache,
)
from html_gen import (
    _RELOAD_SCRIPT,
    _ReloadHandler,
    _notify_reload,
    generate_favicon_svg,
    generate_html,
)


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
        "--no-fetch-jdp",
        action="store_true",
        help="Skip fetching JD Power price data.",
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
        help="Seconds to sleep between Edmunds fetches (default: 10).",
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
        "--retry-nulls-jdp",
        action="store_true",
        help="Re-fetch JD Power cached entries whose stored value is null.",
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
        ) = args.retry_nulls_jdp = True

    if args.proxy:
        import proxy as _proxy_mod
        _proxy_mod._proxy_ip_check_url = args.ip_check_url
        _proxy_mod._proxy_direct_ip = _fetch_ip(ip_check_url=args.ip_check_url)
        if _proxy_mod._proxy_direct_ip is None:
            print(f"  Warning: could not reach {args.ip_check_url}.", file=sys.stderr)
        else:
            print(f"  Direct IP: {_proxy_mod._proxy_direct_ip}", file=sys.stderr)
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
            _proxy_mod._proxy_total_count = len(all_candidates)
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

    def _fetch_jdp():
        if not args.no_fetch_jdp:
            print("Fetching JD Power price data...", file=sys.stderr)
            return fetch_jdpower_cache(cars, retry_nulls=args.retry_nulls_jdp)
        return load_jdpower_cache()

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        fut_cg = pool.submit(_fetch_cg)
        fut_ari = pool.submit(_fetch_ari)
        fut_cc = pool.submit(_fetch_cc)
        fut_edm = pool.submit(_fetch_edmunds)
        fut_jdp = pool.submit(_fetch_jdp)
        raw_cache = fut_cg.result()
        ari_cache = fut_ari.result()
        cc_cache = fut_cc.result()
        edmunds_cache = fut_edm.result()
        jdpower_cache = fut_jdp.result()

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
                    jdpower_cache=jdpower_cache,
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

        here = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(here, "template.html")
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
