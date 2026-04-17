`parse.py` handles pulling and generating data.
`template.html` uses AlpineJS and TailwindCSS, and contains Jinja2 templates for JSON data injection from `parse.py`
To test, run `uv run parse.py --html-out index.html`, run this after every change to test.
CarComplaints results are cached in `.carcomplaints_cache.json`
CarGurus results are cached in `.cargurus_cache.json`
AutoReliabilityIndex results are cached in `.ari_cache.json`
Python indents should be multiples of 4 spaces.
CarComplaints aggregates NHTSA complaints as well as user complaints, and for the best and worst models provides a badge. It may be abbreviated as `CC`
AutoReliabilityIndex provides a score from 0 to 100 based on NHTSA recalls, owner complaints, and independent repair data. It may be abbreviated as `ARI`
Using the Firefox MCP, connect to `http://localhost:8000/` to profile loading performance.
