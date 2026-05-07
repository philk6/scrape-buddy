# Live Benchmark

Use this benchmark to validate Scrape Buddy against many public retailer,
distributor, and wholesale catalog sites.

The runner is intentionally conservative:

- benchmark mode is enabled by default
- detail-page visits default to 8 per site
- listing pagination defaults to 3 pages per site
- requests run sequentially with a delay between sites
- `robots.txt` is respected by default
- reports are written to `benchmark_results/`

Smoke test:

```powershell
.\.venv\Scripts\python.exe benchmarks\live_benchmark.py --limit 10 --delay 2
```

Launch benchmark:

```powershell
.\.venv\Scripts\python.exe benchmarks\live_benchmark.py --limit 200 --delay 3 --detail-limit 8 --listing-limit 3
```

Faster launch benchmark with modest parallelism across different domains:

```powershell
.\.venv\Scripts\python.exe benchmarks\live_benchmark.py --limit 200 --workers 4 --delay 0.5 --site-timeout 60 --request-timeout 8 --detail-limit 8 --listing-limit 3
```

Deeper follow-up for specific failures:

```powershell
.\.venv\Scripts\python.exe benchmarks\live_benchmark.py --offset 40 --limit 20 --delay 3 --detail-limit 20 --listing-limit 6
```

Statuses:

- `pass`: at least 3 products and acceptable quality score
- `partial`: products found but coverage is weak
- `fail`: no products found
- `blocked`: robots, anti-bot, CAPTCHA, or access denial
- `timeout`: the site did not finish inside the configured per-site limit
- `error`: network or runtime failure
