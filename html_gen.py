#!/usr/bin/env python3
import http.server
import json
import os
import queue
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

import jinja2
import minify_html

from car_data import FAMILY_MAPPINGS
from carcomplaints import cc_cache_key
from edmunds import AUTOTRADER_MAPPINGS

ALPINE_JS_URL = "https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"
PURE_CSS_URL = "https://cdn.jsdelivr.net/npm/purecss@3.0.0/build/base-min.css"
ALPINE_CACHE_FILE = os.path.join(HERE, ".alpine_cache.js")
PURE_CSS_CACHE_FILE = os.path.join(HERE, ".pure_css_cache.css")

_reload_clients: list[queue.Queue] = []
_reload_lock = threading.Lock()

_RELOAD_SCRIPT = b'\n<script>(function(){var s=new EventSource("/reload");s.onmessage=function(){location.reload()}})()</script>\n'


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
        raw_models = FAMILY_MAPPINGS.get((make, model)) or [model]
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


def generate_html(
    cars: list[dict],
    cargurus_js_cache: dict | None = None,
    ari_cache: dict | None = None,
    cc_cache: dict | None = None,
    edmunds_cache: dict | None = None,
    jdpower_cache: dict | None = None,
    fork_info: list[dict] | None = None,
    minify: bool = True,
    html_out: str | None = None,
) -> str:
    here = os.path.dirname(__file__)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
    template = env.get_template("template.html")
    model_mappings_json = json.dumps(
        {
            f"{make}|{model}": mapped
            for (make, model), mapped in FAMILY_MAPPINGS.items()
        },
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
        edmunds_cache_json=json.dumps(
            {k: v for k, v in (edmunds_cache or {}).items() if v is not None},
            separators=(",", ":"),
        ),
        jdpower_cache_json=json.dumps(
            {k: v for k, v in (jdpower_cache or {}).items() if v is not None},
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
