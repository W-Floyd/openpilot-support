# Openpilot Vehicle Support Table - ![LLM](https://img.shields.io/badge/LLM%20Assisted-red)

A tool that aggregates vehicle support information from multiple openpilot forks into a single interactive table.

This is a personal project created for my own curiosity, very much LLM assisted.

## Forks Supported

- openpilot
- sunnypilot
- OPGM
- BMW-E8x-E9x
- StarPilot
- BluePilot

## Usage

```bash
uv run parse.py --html-out index.html
```

### Command-Line Options

| Flag | Description |
|------|-------------|
| `--html-out PATH` | Output path for the HTML file |
| `--json-out PATH` | Output path for raw JSON data |
| `--serve` / `--port PORT` | Serve output on localhost after building (default port 8000) |
| `--no-fetch-cg` | Skip fetching CarGurus data |
| `--no-fetch-ari` | Skip fetching AutoReliabilityIndex data |
| `--no-fetch-cc` | Skip fetching CarComplaints data |
| `--no-minify` | Skip HTML/JS/CSS minification (useful for debugging) |
| `--no-cache-openpilot` | Re-fetch all fork data (still updates cache) |
| `--retry-nulls-cg` | Retry previously failed CarGurus cache entries |
| `--retry-nulls-ari` | Retry previously failed ARI cache entries |
| `--retry-nulls-cc` | Retry previously failed CarComplaints cache entries |
| `--retry-nulls-all` | Retry all previously failed cache entries |
| `--watch` | Watch template.html and rebuild on changes |

### Caching

Results are cached to avoid re-fetching on every run. Stale entries (for cars no longer in any fork) are pruned automatically.

| Cache file | Data source |
|-----------|-------------|
| `.openpilot_cache.json` | Fork car documentation |
| `.cargurus_cache.json` | CarGurus listings |
| `.carcomplaints_cache.json` | CarComplaints complaint/recall data |
| `.ari_cache.json` | AutoReliabilityIndex scores |

## Search Parameter Support

The search parameters panel passes user-specified filters into each provider's URL. Keep this table in sync with the URL-building functions in `template.html`.

| Parameter    | eBay | AutoTrader | CarGurus | Cars.com | Carvana | CarMax |
|--------------|:----:|:----------:|:--------:|:--------:|:-------:|:------:|
| Min Mileage  |      |            | ✓        |          | ✓       | ✓      |
| Max Mileage  | ✓ (bracketed) | ✓          | ✓        | ✓        | ✓       | ✓      |
| Min Price    | ✓    | ✓          | ✓        | ✓        | ✓       | ✓      |
| Max Price    | ✓    | ✓          | ✓        | ✓        | ✓       | ✓      |
| Max Distance | ✓    | ✓          | ✓        | ✓        |         |        |
| Zip Code     | ✓    | ✓          | ✓        | ✓        |         |        |
| Model Years  | ✓    | ✓          | ✓        | ✓        | ✓       | ✓      |
| Trim/Package |      |            |          |          | ✓       | ✓      |

## Output Files

- `index.html` — Interactive HTML table with vehicle support info
- `index-favicon.svg` — Favicon for the HTML page
- `cars.json` — Raw JSON data for all vehicles (only when `--json-out` is passed)
