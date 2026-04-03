#!/usr/bin/env python3
"""Scrape https://comma.ai/vehicles and output structured JSON.

Usage:
  python3 scrape_vehicles.py [output.json]
  python3 scrape_vehicles.py [output.json] --html [file.html]
  python3 scrape_vehicles.py [output.json] --serve [--port 8080]
"""
import argparse
import datetime
import http.server
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

URL = "https://comma.ai/vehicles"
CACHE_HTML = ".vehicles_cache.html"
CACHE_ETAG = ".vehicles_cache.etag"

# HTML5 void elements — no closing tag, so don't count them for depth
VOID_ELEMENTS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}


def parse_trims(support):
    if 'all packages and trims' in support:
        return 'All'
    m = re.search(r'come equipped with (.+?)\.', support)
    return m.group(1) if m else support


def parse_min_speed(description):
    if 'at all speeds' in description:
        return 0
    m = re.search(r'(?:while driving )?above (\d+) mph', description)
    if m:
        return int(m.group(1))
    return None


def strip_tags(text):
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


class VehicleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.vehicles = []
        self.current_make = None
        self._depth = 0  # only non-void elements

        # Section entry depths (set when we enter each section)
        self._make_header_depth = None
        self._car_row_depth = None
        self._label_depth = None
        self._detail_depth = None
        self._tier_depth = None
        self._support_card_depth = None
        self._support_hgroup_depth = None

        # State flags
        self._in_make_h3 = False
        self._in_model = False
        self._in_year = False
        self._in_harness = False

        # Per-row accumulators
        self._row = {}
        self._tier_html = ''
        self._support_html = ''
        self._harness_text = ''

    @property
    def _in_make_header(self): return self._make_header_depth is not None
    @property
    def _in_car_row(self):     return self._car_row_depth is not None
    @property
    def _in_label(self):       return self._label_depth is not None
    @property
    def _in_detail(self):      return self._detail_depth is not None
    @property
    def _in_tier(self):        return self._tier_depth is not None
    @property
    def _in_support_card(self): return self._support_card_depth is not None
    @property
    def _in_support_hgroup(self): return self._support_hgroup_depth is not None

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self._depth += 1
        d = self._depth
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').split())
        href = attrs.get('href', '')

        if 'car-make-header' in classes:
            self._make_header_depth = d

        if 'car-row' in classes:
            self._car_row_depth = d
            self._row = {
                'make': self.current_make, 'model': '', 'years': [],
                'description': '', 'support': '', 'harness': '', 'shop_url': ''
            }
            self._tier_html = ''
            self._support_html = ''
            self._harness_text = ''
            self._label_depth = None
            self._detail_depth = None
            self._tier_depth = None
            self._support_card_depth = None
            self._support_hgroup_depth = None
            self._in_model = False
            self._in_year = False
            self._in_harness = False

        if self._in_make_header and tag == 'h3':
            self._in_make_h3 = True

        if self._in_car_row:
            if tag == 'label' and not self._in_label and not self._in_detail:
                self._label_depth = d

            if 'detail-content' in classes and not self._in_detail:
                self._detail_depth = d
                self._label_depth = None

            if self._in_label:
                if 'model' in classes:
                    self._in_model = True
                if 'year' in classes:
                    self._in_year = True

            if self._in_detail:
                if 'car-detail-tier' in classes and not self._in_tier:
                    self._tier_depth = d

                if (not self._in_tier and 'card' in classes and 'elevated' in classes
                        and not self._in_support_card):
                    self._support_card_depth = d

                if self._in_support_card and tag == 'hgroup' and not self._in_support_hgroup:
                    self._support_hgroup_depth = d

                if '/shop/comma-four' in href:
                    self._row['shop_url'] = 'https://comma.ai' + href

                if tag == 'strong' and not self._in_tier and not self._in_harness:
                    self._in_harness = True
                    self._harness_text = ''

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        d = self._depth
        self._depth -= 1

        if self._in_make_header and d == self._make_header_depth:
            self._make_header_depth = None
            self._in_make_h3 = False

        if self._in_car_row and d == self._car_row_depth:
            self._car_row_depth = None
            row = self._row
            row['description'] = strip_tags(self._tier_html)
            row['support'] = re.sub(r'^\s*Support\s*', '',
                                    strip_tags(self._support_html)).strip()
            if row['model'].strip():
                self.vehicles.append(row)
            return

        if self._in_car_row:
            if self._in_label and d == self._label_depth:
                self._label_depth = None
                self._in_model = False
                self._in_year = False

            if self._in_detail and d == self._detail_depth:
                self._detail_depth = None

            if self._in_model and tag in ('div', 'strong'):
                self._in_model = False

            if self._in_year and tag == 'div':
                self._in_year = False

            if self._in_tier and d == self._tier_depth:
                self._tier_depth = None

            if self._in_support_card and d == self._support_card_depth:
                self._support_card_depth = None
                self._support_hgroup_depth = None

            if self._in_support_hgroup and d == self._support_hgroup_depth:
                self._support_hgroup_depth = None

            if self._in_harness and tag == 'strong':
                text = self._harness_text.strip()
                if text.lower().startswith('car harness:'):
                    self._row['harness'] = text[len('car harness:'):].strip()
                self._in_harness = False

    def handle_data(self, data):
        if self._in_make_h3:
            cleaned = re.sub(r'\s*\(\d+\)\s*', '', data).strip()
            if cleaned:
                self.current_make = cleaned

        if self._in_car_row:
            if self._in_model:
                self._row['model'] += data
            if self._in_year:
                for y in data.split(','):
                    y = y.strip()
                    if re.match(r'^\d{4}$', y):
                        self._row['years'].append(int(y))
            if self._in_tier:
                self._tier_html += data
            if self._in_support_hgroup:
                self._support_html += data
            if self._in_harness:
                self._harness_text += data


def fetch_html():
    cached_etag = None
    if os.path.exists(CACHE_ETAG):
        with open(CACHE_ETAG) as f:
            cached_etag = f.read().strip()

    headers = {'User-Agent': 'Mozilla/5.0'}
    if cached_etag:
        headers['If-None-Match'] = cached_etag

    req = urllib.request.Request(URL, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode('utf-8')
            etag = resp.headers.get('ETag')
            with open(CACHE_HTML, 'w') as f:
                f.write(html)
            if etag:
                with open(CACHE_ETAG, 'w') as f:
                    f.write(etag)
            print("Fetched fresh HTML", file=sys.stderr)
            return html, etag
    except urllib.request.HTTPError as e:
        if e.code == 304 and os.path.exists(CACHE_HTML):
            with open(CACHE_HTML) as f:
                html = f.read()
            print("Using cached HTML (ETag matched)", file=sys.stderr)
            return html, cached_etag
        raise


def scrape():
    html, etag = fetch_html()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    parser = VehicleParser()
    parser.feed(html)
    vehicles = [
        {
            'make':                      v['make'],
            'model':                     v['model'].strip(),
            'years':                     sorted(set(v['years'])),
            'description':               v['description'],
            'support':                   re.sub(r'\s+', ' ', v['support']).strip(),
            'trims':                     parse_trims(v['support']),
            'min_speed_mph':             parse_min_speed(v['description']),
            'acc_resumes_from_stop':     'resumes from a stop' in v['description'],
            'no_tight_turns':            'may not be able to take tight turns' in v['description'],
            'traffic_light_support':     'traffic light and stop sign' in v['description'].lower(),
            'traffic_light_experimental': 'traffic light and stop sign handling is also available in experimental mode' in v['description'].lower(),
            'ebay_url':                  'https://www.ebay.com/sch/6001/i.html?_nkw=' + urllib.parse.quote_plus(f"{v['make']} {v['model'].strip()} ({','.join(str(y) for y in sorted(set(v['years'])))})") + '&_sop=2',
            'harness':                   v['harness'].strip(),
            'shop_url':                  v['shop_url'],
        }
        for v in parser.vehicles
    ]
    return vehicles, etag, timestamp


def build_html(vehicles, etag=None, timestamp=None):
    data_json = json.dumps(vehicles)
    meta = ' · '.join(filter(None, [timestamp, f'ETag: {etag}' if etag else None]))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>comma.ai — Vehicle Compatibility</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; background: #f5f5f5; color: #111; }}

    #header {{ background: #000; color: #fff; padding: 1rem 1.5rem; display: flex; align-items: baseline; gap: 1rem; }}
    #header h1 {{ font-size: 1.2rem; font-weight: 700; }}
    #header a {{ color: #888; font-size: 13px; text-decoration: none; }}
    #header a:hover {{ color: #fff; }}

    #filters {{
      position: sticky; top: 0; z-index: 10;
      background: #fff; border-bottom: 1px solid #e0e0e0;
      padding: 0.65rem 1.5rem;
      display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
    }}

    #search {{
      padding: 0.35rem 0.65rem; border: 1px solid #ccc; border-radius: 4px;
      font-size: 14px; width: 200px;
    }}
    #search:focus {{ outline: none; border-color: #888; }}

    select {{
      padding: 0.35rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;
      font-size: 13px; background: #fff; cursor: pointer;
    }}
    select:focus {{ outline: none; border-color: #888; }}

    .sep {{ color: #ddd; }}

    .toggle {{
      padding: 0.25rem 0.6rem; border: 1px solid #ccc; border-radius: 999px;
      cursor: pointer; font-size: 12px; user-select: none; background: #fff;
      transition: background 0.1s, color 0.1s, border-color 0.1s;
      white-space: nowrap;
    }}
    .toggle:hover {{ border-color: #888; }}
    .toggle.active {{ background: #111; color: #fff; border-color: #111; }}

    #bar {{ padding: 0.4rem 1.5rem; font-size: 12px; color: #888; background: #f5f5f5; border-bottom: 1px solid #eee; }}

    .table-wrap {{ overflow-x: auto; }}

    table {{ width: 100%; border-collapse: collapse; background: #fff; }}

    th {{
      text-align: left; padding: 0.55rem 0.75rem;
      background: #f9f9f9; border-bottom: 2px solid #e0e0e0;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
      white-space: nowrap; cursor: pointer; user-select: none;
    }}
    th:hover {{ background: #efefef; }}
    th .si {{ margin-left: 3px; color: #bbb; font-size: 10px; }}
    th.asc .si::after {{ content: "▲"; color: #333; }}
    th.desc .si::after {{ content: "▼"; color: #333; }}
    th:not(.asc):not(.desc) .si::after {{ content: "⇅"; }}

    td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fafafa; }}

    .t {{ color: #16a34a; font-weight: 700; }}
    .f {{ color: #d1d5db; }}

    td.make {{ font-weight: 600; white-space: nowrap; }}
    td.years {{ white-space: nowrap; color: #555; font-size: 13px; }}
    td.speed {{ white-space: nowrap; }}
    td.harness {{ font-size: 12px; color: #555; white-space: nowrap; }}
    td.shop a {{ font-size: 12px; color: #2563eb; text-decoration: none; }}
    td.shop a:hover {{ text-decoration: underline; }}

    #empty {{ display: none; padding: 4rem; text-align: center; color: #aaa; font-size: 15px; }}
  </style>
</head>
<body>

<div id="header">
  <h1>comma.ai — Vehicle Compatibility</h1>
  <a href="{URL}" target="_blank">source ↗</a>
  <a href="https://github.com/W-Floyd/openpilot-support" target="_blank">github ↗</a>
  <span style="margin-left:auto; font-size:12px; color:#888;">{meta}</span>
</div>

<div id="filters">
  <input id="search" type="search" placeholder="Search make, model…" autocomplete="off">

  <span class="sep">|</span>

  <select id="sel-harness"><option value="">All harnesses</option></select>

  <select id="sel-speed">
    <option value="">Any min speed</option>
    <option value="0">All speeds (0 mph)</option>
    <option value="low">Low (&le;15 mph)</option>
    <option value="high">High (&gt;15 mph)</option>
  </select>

  <span class="sep">|</span>

  <span class="toggle" id="toggle-all-trims">All trims</span>
  <span class="toggle" data-field="acc_resumes_from_stop">ACC from stop</span>
  <span class="toggle" data-field="traffic_light_support">Traffic lights</span>
  <span class="toggle" data-field="no_tight_turns" data-neg>No tight turn warning</span>
</div>

<div id="bar"></div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th data-col="make">Make<span class="si"></span></th>
        <th data-col="model">Model<span class="si"></span></th>
        <th data-col="years">Years<span class="si"></span></th>
        <th data-col="min_speed_mph">Min Speed<span class="si"></span></th>
        <th data-col="trims">Trims<span class="si"></span></th>
        <th data-col="acc_resumes_from_stop">ACC Stop<span class="si"></span></th>
        <th data-col="no_tight_turns">Tight Turns<span class="si"></span></th>
        <th data-col="traffic_light_support">Traffic Lights<span class="si"></span></th>
        <th data-col="traffic_light_experimental">Experimental<span class="si"></span></th>
        <th data-col="harness">Harness<span class="si"></span></th>
        <th>Shop</th>
        <th>eBay</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="empty">No vehicles match the current filters.</div>
</div>

<script>
const DATA = {data_json};

// Populate harness dropdown
const harnesses = [...new Set(DATA.map(v => v.harness).filter(Boolean))].sort();
const selHarness = document.getElementById('sel-harness');
harnesses.forEach(h => {{
  const o = document.createElement('option');
  o.value = o.textContent = h;
  selHarness.appendChild(o);
}});

// Sort state
let sortCol = 'make', sortDir = 1;

// Toggle state: field -> true (require true) | false (require false)
const toggleState = {{}};
let filterAllTrims = false;

function years(v) {{
  if (!v.years.length) return '';
  const lo = v.years[0], hi = v.years[v.years.length - 1];
  return lo === hi ? String(lo) : lo + '–' + hi;
}}

function speed(v) {{
  if (v.min_speed_mph === null || v.min_speed_mph === undefined) return '—';
  return v.min_speed_mph === 0 ? 'All speeds' : v.min_speed_mph + ' mph+';
}}

function bool(val) {{
  return val ? '<span class="t">✓</span>' : '<span class="f">✗</span>';
}}

function render() {{
  const search = document.getElementById('search').value.toLowerCase();
  const harness = selHarness.value;
  const spd = document.getElementById('sel-speed').value;

  let rows = DATA.filter(v => {{
    if (search && !(v.make + ' ' + v.model).toLowerCase().includes(search)) return false;
    if (harness && v.harness !== harness) return false;
    if (spd === '0' && v.min_speed_mph !== 0) return false;
    if (spd === 'low' && (v.min_speed_mph === null || v.min_speed_mph > 15)) return false;
    if (spd === 'high' && (v.min_speed_mph === null || v.min_speed_mph <= 15)) return false;
    if (filterAllTrims && v.trims !== 'All') return false;
    for (const [field, req] of Object.entries(toggleState)) {{
      if (v[field] !== req) return false;
    }}
    return true;
  }});

  rows.sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (Array.isArray(av)) av = av[0] ?? 0;
    if (Array.isArray(bv)) bv = bv[0] ?? 0;
    if (av === null || av === undefined) av = -Infinity;
    if (bv === null || bv === undefined) bv = -Infinity;
    return sortDir * (typeof av === 'string' ? av.localeCompare(bv) : av - bv);
  }});

  document.getElementById('tbody').innerHTML = rows.map(v => `
    <tr>
      <td class="make">${{v.make}}</td>
      <td>${{v.model}}</td>
      <td class="years">${{years(v)}}</td>
      <td class="speed">${{speed(v)}}</td>
      <td>${{v.trims}}</td>
      <td>${{bool(v.acc_resumes_from_stop)}}</td>
      <td>${{bool(!v.no_tight_turns)}}</td>
      <td>${{bool(v.traffic_light_support)}}</td>
      <td>${{v.traffic_light_support ? bool(v.traffic_light_experimental) : '<span class="f">—</span>'}}</td>
      <td class="harness">${{v.harness}}</td>
      <td class="shop"><a href="${{v.shop_url}}" target="_blank">Buy →</a></td>
      <td class="shop"><a href="${{v.ebay_url}}" target="_blank">Search →</a></td>
    </tr>
  `).join('');

  document.getElementById('bar').textContent = `Showing ${{rows.length}} of ${{DATA.length}} vehicles`;
  document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
}}

document.getElementById('search').addEventListener('input', render);
selHarness.addEventListener('change', render);
document.getElementById('sel-speed').addEventListener('change', render);

document.getElementById('toggle-all-trims').addEventListener('click', function() {{
  filterAllTrims = !filterAllTrims;
  this.classList.toggle('active', filterAllTrims);
  render();
}});

document.querySelectorAll('.toggle[data-field]').forEach(el => {{
  el.addEventListener('click', () => {{
    const field = el.dataset.field;
    const neg = 'neg' in el.dataset;
    if (!el.classList.contains('active')) {{
      el.classList.add('active');
      toggleState[field] = !neg;
    }} else {{
      el.classList.remove('active');
      delete toggleState[field];
    }}
    render();
  }});
}});

document.querySelectorAll('th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.col;
    sortDir = sortCol === col ? -sortDir : 1;
    sortCol = col;
    document.querySelectorAll('th').forEach(h => h.classList.remove('asc', 'desc'));
    th.classList.add(sortDir === 1 ? 'asc' : 'desc');
    render();
  }});
}});

render();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Scrape comma.ai/vehicles")
    ap.add_argument('output', nargs='?', default='vehicles.json',
                    help="JSON output path (default: vehicles.json)")
    ap.add_argument('--html', metavar='FILE', nargs='?', const='index.html',
                    help="Write HTML output (default: index.html)")
    ap.add_argument('--serve', action='store_true',
                    help="Start a web server after scraping")
    ap.add_argument('--port', type=int, default=8080,
                    help="Port for --serve (default: 8080)")
    args = ap.parse_args()

    vehicles, etag, timestamp = scrape()

    with open(args.output, 'w') as f:
        json.dump(vehicles, f, indent=2)
    print(f"Wrote {len(vehicles)} vehicles to {args.output}", file=sys.stderr)

    if args.html:
        with open(args.html, 'w') as f:
            f.write(build_html(vehicles, etag, timestamp))
        print(f"Wrote HTML to {args.html}", file=sys.stderr)

    if args.serve:
        page = build_html(vehicles, etag, timestamp).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(page))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *_):
                pass  # suppress per-request logs

        server = http.server.HTTPServer(('', args.port), Handler)
        print(f"Serving at http://localhost:{args.port}", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.", file=sys.stderr)
            server.server_close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(0)
