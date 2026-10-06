# Agent Assist -- Portable Demo

Standalone, sanitized snapshot of the AISLE One CRM "Agent Assist" report
(`/reports/agent-assist`), rebuilt for demo purposes with zero backend,
zero auth, zero live data connection.

## Files

- `agent-assist-scorecard-demo.html` -- **the portable file.** Single HTML
  file, opens directly via `file://` or double-click. Chart.js is pulled
  from a CDN (needs internet); everything else is fully inline.
- `index.html` / `styles.css` / `app.js` / `data.js` -- split source used
  to build the standalone file (easier to edit than the flattened version).
- `generate_data.py` -- regenerates `data.json` / `data.js`.
- `data.json` -- sanitized sample dataset (JSON form).

## Data provenance & sanitization

Source: `AISLEONE-CRM` repo, `backend/app/routes/Agent_Assist/__init__.py`.
That module ships its own built-in stub/demo-data generator (used whenever
BigQuery is unavailable) -- already 100% synthetic, no production numbers.

`generate_data.py` reproduces that generator's shape, then applies an
*additional* sanitization pass: one fixed multiplicative "family factor"
per metric type (AHT-seconds, percentages, NPS/sentiment, counts) plus
small independent per-point jitter. This guarantees the exported numbers
differ from both the app's own stub literals and any real prod figures,
while preserving every qualitative insight (AA beats Non-AA on AHT/CSAT/
NPS, endpoint rankings, trend shapes) because the same factor applies
consistently within a metric family.

To regenerate with a different random seed:

```bash
python3 generate_data.py   # writes data.json
python3 -c "import json; d=json.load(open('data.json')); open('data.js','w').write('const DATA = ' + json.dumps(d, indent=2) + ';')"
```

Then re-flatten into the standalone file (see chat history / rebuild
script) or just open `index.html` directly for local editing.

## What's covered

All 7 tabs from the real report:
1. Metric Definitions (static formulas -- generic, non-confidential)
2. Executive Summary -- 6 KPI cards, trend charts, acceptance rate,
   contact volume, channel matrix
3. AHT -- AA vs Non-AA trend + goal line, per-endpoint trends, tenure charts
4. Sentiment Score -- AA vs Non-AA trend, per-endpoint, tenure
5. Metrics by L3 Workflow -- table + chart
6. Core Metrics by Endpoint -- full 7-metric grid + AHT chart
7. Agent Utilization -- distinct agent KPI cards + weekly trend

Channel / Agent Group / Time Grain filters are live (client-side
recompute / resampling), matching the real report's filter bar.
