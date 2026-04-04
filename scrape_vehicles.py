#!/usr/bin/env python3
"""Fetch https://comma.ai/vehicles data from the source JSON and output structured JSON.

Usage:
  python3 scrape_vehicles.py [output.json]
  python3 scrape_vehicles.py [output.json] --html [file.html]
  python3 scrape_vehicles.py [output.json] --serve [--port 8080]
"""
import argparse
import base64
import datetime
import http.server
import json
import os
import re
import sys
import urllib.parse
import urllib.request

URL = "https://comma.ai/vehicles"
JSON_URL = "https://raw.githubusercontent.com/commaai/website/refs/heads/master/src/lib/vehicles.json"
CACHE_JSON = ".vehicles_cache.json"
CACHE_ETAG = ".vehicles_cache.etag"


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


def fetch_favicon():
    req = urllib.request.Request('https://comma.ai/favicon.png',
                                 headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


def fetch_json():
    cached_etag = None
    if os.path.exists(CACHE_ETAG):
        with open(CACHE_ETAG) as f:
            cached_etag = f.read().strip()

    headers = {'User-Agent': 'Mozilla/5.0'}
    if cached_etag:
        headers['If-None-Match'] = cached_etag

    req = urllib.request.Request(JSON_URL, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode('utf-8')
            etag = resp.headers.get('ETag')
            with open(CACHE_JSON, 'w') as f:
                f.write(data)
            if etag:
                with open(CACHE_ETAG, 'w') as f:
                    f.write(etag)
            print("Fetched fresh JSON", file=sys.stderr)
            return json.loads(data), etag
    except urllib.request.HTTPError as e:
        if e.code == 304 and os.path.exists(CACHE_JSON):
            with open(CACHE_JSON) as f:
                data = f.read()
            print("Using cached JSON (ETag matched)", file=sys.stderr)
            return json.loads(data), cached_etag
        raise


def scrape():
    data, etag = fetch_json()
    favicon = fetch_favicon()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    vehicles = []
    for make, entries in data.items():
        for entry in entries:
            support = entry.get('package', '')
            description = strip_tags(entry.get('detail_sentence', ''))
            model = entry.get('model', '').strip()
            years = sorted(set(
                int(y.strip()) for y in entry.get('year_list', '').split(',')
                if y.strip().isdigit()
            ))
            harness = entry.get('harness_connector', '').strip()
            vehicles.append({
                'make':                       make,
                'model':                      model,
                'years':                      years,
                'description':                description,
                'support':                    re.sub(r'\s+', ' ', support).strip(),
                'trims':                      parse_trims(support),
                'min_speed_mph':              parse_min_speed(description),
                'acc_resumes_from_stop':      'resumes from a stop' in description,
                'no_tight_turns':             'may not be able to take tight turns' in description,
                'traffic_light_support':      'traffic light and stop sign' in description.lower(),
                'traffic_light_experimental': 'traffic light and stop sign handling is also available in experimental mode' in description.lower(),
                'ebay_url':                   'https://www.ebay.com/sch/6001/i.html?_nkw=' + urllib.parse.quote_plus(f"{make} {model} ({','.join(str(y) for y in years)})") + '&_sop=2&_blrs=category_constraint',
                'harness':                    harness,
                'shop_url':                   'https://comma.ai/shop/comma-four',
            })
    return vehicles, etag, timestamp, favicon


def build_html(vehicles, etag=None, timestamp=None, favicon=None):
    data_json = json.dumps(vehicles)
    meta = ' · '.join(filter(None, [timestamp, f'ETag: {etag}' if etag else None]))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Vehicle Compatibility for comma.ai</title>
  {f'<link rel="icon" href="{favicon}">' if favicon else ''}
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

    #filter-row th {{ background: #ececec; padding: 0.2rem 0.4rem; cursor: default; border-bottom: 1px solid #e0e0e0; }}
    #filter-row th:hover {{ background: #ececec; }}
    #filter-row select, #filter-row input[type=text] {{
      width: 100%; padding: 0.15rem 0.25rem; font-size: 11px;
      border: 1px solid #ddd; border-radius: 3px; background: #fff; box-sizing: border-box;
    }}
    #filter-row select:focus, #filter-row input[type=text]:focus {{ outline: none; border-color: #888; }}

    td {{ padding: 0.25rem 0.75rem; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fafafa; }}

    .t {{ color: #16a34a; font-weight: 700; }}
    .f {{ color: #d1d5db; }}

    td.make {{ font-weight: 600; white-space: nowrap; padding: 0; }}
    td.make > div {{ display: flex; align-items: center; gap: 0.4em; padding: 0.25rem 0.75rem; }}
    td.make img {{ height: calc(1em + 0.5rem); width: auto; display: block; object-fit: contain; }}
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
        <th data-col="acc_resumes_from_stop">ACC From Stop<span class="si"></span></th>
        <th data-col="no_tight_turns">Tight Turns<span class="si"></span></th>
        <th data-col="traffic_light_support">Traffic Lights<span class="si"></span></th>
        <th data-col="traffic_light_experimental">Experimental<span class="si"></span></th>
        <th data-col="harness">Harness<span class="si"></span></th>
        <th>Shop</th>
        <th>eBay</th>
      </tr>
      <tr id="filter-row">
        <th><select id="fcol-make"><option value="">All</option></select></th>
        <th><input id="fcol-model" type="text" placeholder="filter…"></th>
        <th><input id="fcol-years" type="text" placeholder="year…"></th>
        <th><select id="fcol-min_speed_mph"><option value="">Any</option></select></th>
        <th><select id="fcol-trims"><option value="">Any</option></select></th>
        <th><select id="fcol-acc_resumes_from_stop"><option value="">Any</option><option value="true">✓</option><option value="false">✗</option></select></th>
        <th><select id="fcol-no_tight_turns"><option value="">Any</option><option value="false">✓</option><option value="true">✗</option></select></th>
        <th><select id="fcol-traffic_light_support"><option value="">Any</option><option value="true">✓</option><option value="false">✗</option></select></th>
        <th><select id="fcol-traffic_light_experimental"><option value="">Any</option><option value="true">✓</option><option value="false">✗</option></select></th>
        <th><select id="fcol-harness"><option value="">All</option></select></th>
        <th></th>
        <th></th>
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

// Populate column filter dropdowns
function populateCatFilter(id, field) {{
  const sel = document.getElementById(id);
  [...new Set(DATA.map(v => v[field]).filter(Boolean))].sort().forEach(val => {{
    const o = document.createElement('option');
    o.value = o.textContent = val;
    sel.appendChild(o);
  }});
}}
populateCatFilter('fcol-make', 'make');
populateCatFilter('fcol-trims', 'trims');
populateCatFilter('fcol-harness', 'harness');
[...new Set(DATA.map(v => v.min_speed_mph).filter(v => v !== null && v !== undefined))].sort((a, b) => a - b).forEach(v => {{
  const o = document.createElement('option');
  o.value = v;
  o.textContent = v === 0 ? 'All speeds' : v + ' mph+';
  document.getElementById('fcol-min_speed_mph').appendChild(o);
}});

// Sort state
let sortCol = 'make', sortDir = 1;


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

  const cfMake    = document.getElementById('fcol-make').value;
  const cfModel   = document.getElementById('fcol-model').value.toLowerCase();
  const cfYears   = document.getElementById('fcol-years').value.trim();
  const cfSpeed   = document.getElementById('fcol-min_speed_mph').value;
  const cfTrims   = document.getElementById('fcol-trims').value;
  const cfAcc     = document.getElementById('fcol-acc_resumes_from_stop').value;
  const cfTurns   = document.getElementById('fcol-no_tight_turns').value;
  const cfTL      = document.getElementById('fcol-traffic_light_support').value;
  const cfTLExp   = document.getElementById('fcol-traffic_light_experimental').value;
  const cfHarness = document.getElementById('fcol-harness').value;

  let rows = DATA.filter(v => {{
    if (search && !(v.make + ' ' + v.model).toLowerCase().includes(search)) return false;
    if (harness && v.harness !== harness) return false;
    if (spd === '0' && v.min_speed_mph !== 0) return false;
    if (spd === 'low' && (v.min_speed_mph === null || v.min_speed_mph > 15)) return false;
    if (spd === 'high' && (v.min_speed_mph === null || v.min_speed_mph <= 15)) return false;
    if (cfMake    && v.make !== cfMake) return false;
    if (cfModel   && !v.model.toLowerCase().includes(cfModel)) return false;
    if (cfYears   && !v.years.includes(parseInt(cfYears))) return false;
    if (cfSpeed   !== '' && v.min_speed_mph !== parseInt(cfSpeed)) return false;
    if (cfTrims   && v.trims !== cfTrims) return false;
    if (cfAcc     !== '' && v.acc_resumes_from_stop !== (cfAcc === 'true')) return false;
    if (cfTurns   !== '' && v.no_tight_turns !== (cfTurns === 'true')) return false;
    if (cfTL      !== '' && v.traffic_light_support !== (cfTL === 'true')) return false;
    if (cfTLExp   !== '' && v.traffic_light_experimental !== (cfTLExp === 'true')) return false;
    if (cfHarness && v.harness !== cfHarness) return false;
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
      <td class="make"><div>${{v.make}}</div></td>
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
  const sel = document.getElementById('fcol-trims');
  const active = !this.classList.contains('active');
  this.classList.toggle('active', active);
  sel.value = active ? 'All' : '';
  render();
}});

document.querySelectorAll('.toggle[data-field]').forEach(el => {{
  el.addEventListener('click', () => {{
    const sel = document.getElementById('fcol-' + el.dataset.field);
    const active = !el.classList.contains('active');
    el.classList.toggle('active', active);
    sel.value = active ? String('neg' in el.dataset ? false : true) : '';
    render();
  }});
}});

document.querySelectorAll('#filter-row select, #filter-row input').forEach(el => {{
  el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', render);
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

    vehicles, etag, timestamp, favicon = scrape()

    with open(args.output, 'w') as f:
        json.dump(vehicles, f, indent=2)
    print(f"Wrote {len(vehicles)} vehicles to {args.output}", file=sys.stderr)

    if args.html:
        with open(args.html, 'w') as f:
            f.write(build_html(vehicles, etag, timestamp, favicon))
        print(f"Wrote HTML to {args.html}", file=sys.stderr)

    if args.serve:
        page = build_html(vehicles, etag, timestamp, favicon).encode()

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
