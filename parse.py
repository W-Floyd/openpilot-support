#!/usr/bin/env python3
import argparse
import http.server
import json
import math
import os
import sys

import jinja2

# Add opendbc to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "openpilot", "opendbc_repo"))

from opendbc.car.docs import get_all_car_docs
from opendbc.car.docs_definitions import Column, ExtraCarsColumn, Star


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


def generate_html(cars: list[dict]) -> str:
  here = os.path.dirname(__file__)
  env = jinja2.Environment(loader=jinja2.FileSystemLoader(here))
  template = env.get_template("template.html")
  return template.render(cars_json=json.dumps(cars))


def main():
  parser = argparse.ArgumentParser(description="Generate openpilot car support files.")
  parser.add_argument("--html-out", default=None, help="Path for generated HTML file.")
  parser.add_argument("--json-out", default=None, help="Path for generated JSON file.")
  parser.add_argument("--serve", action="store_true", help="Serve the HTML file on a local HTTP server after building.")
  parser.add_argument("--port", type=int, default=8000, help="Port for --serve (default: 8000).")
  args = parser.parse_args()

  print("Loading car docs...", file=sys.stderr)
  all_car_docs = get_all_car_docs()
  print(f"Found {len(all_car_docs)} cars.", file=sys.stderr)

  cars = [car_docs_to_dict(cd) for cd in all_car_docs]

  if args.json_out:
    os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
    with open(args.json_out, "w") as f:
      json.dump(cars, f, indent=2)
    print(f"Written to {args.json_out}", file=sys.stderr)

  if args.html_out:
    os.makedirs(os.path.dirname(os.path.abspath(args.html_out)), exist_ok=True)
    with open(args.html_out, "w") as f:
      f.write(generate_html(cars))
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
