#!/usr/bin/env python3
"""Scrape https://comma.ai/vehicles and output structured JSON.

Usage: python3 scrape_vehicles.py [output.json]
"""
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

URL = "https://comma.ai/vehicles"
output_path = sys.argv[1] if len(sys.argv) > 1 else "vehicles.json"

# HTML5 void elements — no closing tag, so don't count them for depth
VOID_ELEMENTS = {
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
}


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


CACHE_HTML = ".vehicles_cache.html"
CACHE_ETAG = ".vehicles_cache.etag"

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
except urllib.request.HTTPError as e:
    if e.code == 304 and os.path.exists(CACHE_HTML):
        with open(CACHE_HTML) as f:
            html = f.read()
        print("Using cached HTML (ETag matched)", file=sys.stderr)
    else:
        raise

parser = VehicleParser()
parser.feed(html)

vehicles = [
    {
        'make':        v['make'],
        'model':       v['model'].strip(),
        'years':       sorted(set(v['years'])),
        'description': v['description'],
        'support':     re.sub(r'\s+', ' ', v['support']).strip(),
        'all_trims':      'all packages and trims' in v['support'],
        'no_tight_turns':            'may not be able to take tight turns' in v['description'],
        'acc_resumes_from_stop':      'resumes from a stop' in v['description'],
        'traffic_light_support':     'traffic light and stop sign' in v['description'].lower(),
        'traffic_light_experimental': 'traffic light and stop sign handling is also available in experimental mode' in v['description'].lower(),
        'harness':     v['harness'].strip(),
        'shop_url':    v['shop_url'],
    }
    for v in parser.vehicles
]

with open(output_path, 'w') as f:
    json.dump(vehicles, f, indent=2)

print(f"Wrote {len(vehicles)} vehicles to {output_path}", file=sys.stderr)
