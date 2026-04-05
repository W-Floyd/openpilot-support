`parse.py` handles pulling and generating data.
`template.html` uses AlpineJS and TailwindCSS, and contains Jinja2 templates for JSON data injection from `parse.py`
To test, run `uv run parse.py --html-out index.html --json-out cars.json`.
CarComplaints results are cached in `.carcomplaints_cache.json`
CarGurus results are cached in `.cargurus_cache.json`
AutoReliabilityIndex results are cached in `.ari_cache.json`
