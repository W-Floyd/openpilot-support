# CLAUDE.md

`parse.py` handles pulling and generating data. `template.html` uses AlpineJS and PureCSS with Jinja2 for JSON data injection.

To test, run `uv run parse.py --html-out index.html` after every change. Skip external fetches during development with `--no-fetch-cg --no-fetch-ari --no-fetch-cc`.

Unless adding support for a new fork, no not under any circumstances read files inside the openpilot or fork submodule directories. These may be found by checking `FORKS` in `parse.py` 

## Key flags
- `--no-cache-openpilot` — re-fetch all fork data (still saves to cache)
- `--retry-nulls-cg/ari/cc` / `--retry-nulls-all` — retry failed cache entries
- `--no-minify` — skip minification (useful for debugging)
- `--watch` — rebuild on template changes

## Architecture

Fork loading spawns a subprocess per fork (`--dump-fork`) to isolate capnp schema conflicts. Forks are merged in priority order; first fork wins for shared cars. CarGurus/ARI/CarComplaints are fetched in parallel and cached. Stale cache entries are pruned automatically each run.

`_clean_model_name()` and `_modify_package_from_model()` normalize fork-specific quirks: stripping years, moving harness suffixes and `ACC w <word>` suffixes from model→package, and handling Non-ACC/Non-SCC variants.

Filter/sort state is persisted to `localStorage` and URL params. URL params take priority on load.

## Cache files
- `.openpilot_cache.json` — car data per fork
- `.cargurus_cache.json` — CarGurus responses (keyed by query string)
- `.ari_cache.json` — reliability scores (keyed by `make|model|year`)
- `.carcomplaints_cache.json` — complaint/recall counts and seal badges

## Search providers

When adding or changing search parameter support in any provider URL function, always update the **Search Parameter Support** table in `README.md` and the `caps` objects in `searchProviders` in the same change.

## Conventions
- Python indents: 4 spaces
- CC = CarComplaints, ARI = AutoReliabilityIndex, CG = CarGurus
- Using the Firefox MCP, connect to `http://localhost:8000/` to profile loading performance.
