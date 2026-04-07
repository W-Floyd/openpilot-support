# Openpilot Vehicle Support Table - ![LLM](https://img.shields.io/badge/LLM%20Assisted-red)

A tool that aggregates vehicle support information from multiple openpilot forks into a single interactive table.

This is a personal project created for my own curiosity, very much LLM assisted.

## Forks Supported

- openpilot
- sunnypilot
- opgm
- BMW-E8x-E9x
- StarPilot

## Usage

Run the main script to generate HTML and JSON outputs:

```bash
uv run parse.py --html-out index.html
```

### Command-Line Options

- `--html-out PATH` - Output path for the HTML file
- `--json-out PATH` - Output path for the JSON data file
- `--serve` - Serve the HTML file on a local HTTP server after building
- `--port PORT` - Port for `--serve` (default: 8000)
- `--no-fetch-cg` - Skip fetching CarGurus data
- `--no-fetch-ari` - Skip fetching Auto Reliability Index data
- `--no-fetch-cc` - Skip fetching CarComplaints data
- `--no-minify` - Skip HTML/JS/CSS minification (useful for debugging)
- `--no-cache-openpilot` - Force re-fetching openpilot data for all forks (disable caching)

### Caching

The tool caches results to avoid re-fetching data on every run:

**Openpilot Forks**
- Cache file: `.openpilot_cache.json`
- Use `--no-cache-openpilot` to force a fresh fetch from all forks

**CarGurus**
- Cache file: `.cargurus_cache.json`
- Use `--no-fetch-cargurus` to skip fetching entirely

**CarComplaints**
- Cache file: `.carcomplaints_cache.json`
- Use `--no-fetch-cc` to skip fetching entirely

**Auto Reliability Index**
- Cache file: `.ari_cache.json`
- Use `--no-fetch-ari` to skip fetching entirely

## Output Files

- `index.html` - Interactive HTML table with vehicle support info
- `cars.json` - Raw JSON data for all vehicles
- `-favicon.svg` - Favicon for the HTML page
