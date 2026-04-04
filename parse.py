#!/usr/bin/env python3
import argparse
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
    "min_steer_speed": car_docs.min_steer_speed if car_docs.min_steer_speed is not None and not math.isinf(car_docs.min_steer_speed) else None,
    "min_enable_speed": car_docs.min_enable_speed if car_docs.min_enable_speed is not None and not math.isinf(car_docs.min_enable_speed) else None,
    "auto_resume": car_docs.auto_resume,
    "good_steering_torque": star_to_bool(row[Column.STEERING_TORQUE]),
    "openpilot_longitudinal": row[Column.LONGITUDINAL] if not isinstance(row[Column.LONGITUDINAL], Star) else star_to_bool(row[Column.LONGITUDINAL]),
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
  for i, car in enumerate(cars):
    query = cargurus_query(car)
    if query is None or query in cache:
      continue
    print(f"  [{i+1}/{len(cars)}] Fetching: {query}", file=sys.stderr)
    response = fetch_cargurus_response(query)
    cache[query] = response  # store None on failure so we don't retry
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
    if response and response.get("success") != "FAILURE" and response.get("filterCriteria", {}).get("makeModelTrimPaths"):
      result[key] = {"url": build_cargurus_url(response["filterCriteria"])}
    elif query in raw_cache:
      result[key] = {"error": True}
  return result


def generate_html(cars: list[dict], cargurus_js_cache: dict | None = None) -> str:
  here = os.path.dirname(__file__)
  env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
  template = env.get_template("template.html")
  html = template.render(
    cars_json=json.dumps(cars, separators=(',', ':')),
    cargurus_cache_json=json.dumps(cargurus_js_cache or {}, separators=(',', ':')),
  )
  return minify_html.minify(html, minify_js=True, minify_css=True)


def main():
  parser = argparse.ArgumentParser(description="Generate openpilot car support files.")
  parser.add_argument("--html-out", default=None, help="Path for generated HTML file.")
  parser.add_argument("--json-out", default=None, help="Path for generated JSON file.")
  parser.add_argument("--serve", action="store_true", help="Serve the HTML file on a local HTTP server after building.")
  parser.add_argument("--port", type=int, default=8000, help="Port for --serve (default: 8000).")
  parser.add_argument("--no-fetch-cargurus", action="store_true", help="Skip fetching CarGurus data for all cars.")
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

  if args.json_out:
    os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
    with open(args.json_out, "w") as f:
      json.dump(cars, f, indent=2)
    print(f"Written to {args.json_out}", file=sys.stderr)

  if args.html_out:
    os.makedirs(os.path.dirname(os.path.abspath(args.html_out)), exist_ok=True)
    with open(args.html_out, "w") as f:
      f.write(generate_html(cars, cargurus_js_cache))
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
