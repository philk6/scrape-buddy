import os, sys, traceback

errors = []

# Test each import individually
try:
    import database
except Exception as e:
    errors.append({"module": "database", "error": str(e), "tb": traceback.format_exc()})

try:
    import browser_login
except Exception as e:
    errors.append({"module": "browser_login", "error": str(e), "tb": traceback.format_exc()})

try:
    import upc_enrichment
except Exception as e:
    errors.append({"module": "upc_enrichment", "error": str(e), "tb": traceback.format_exc()})

try:
    from scraper import fetch_html, debug_scrape, make_auth_fetch_fn
except Exception as e:
    errors.append({"module": "scraper", "error": str(e), "tb": traceback.format_exc()})

try:
    from strategies import run_best_strategy
except Exception as e:
    errors.append({"module": "strategies", "error": str(e), "tb": traceback.format_exc()})

try:
    from strategies.detail import run as detail_run
except Exception as e:
    errors.append({"module": "strategies.detail", "error": str(e), "tb": traceback.format_exc()})

try:
    from strategies import playwright_catalog
except Exception as e:
    errors.append({"module": "strategies.playwright_catalog", "error": str(e), "tb": traceback.format_exc()})

try:
    from upc_providers import default_providers
except Exception as e:
    errors.append({"module": "upc_providers", "error": str(e), "tb": traceback.format_exc()})

try:
    from pack_parser import enrich_all as enrich_all_pack
except Exception as e:
    errors.append({"module": "pack_parser", "error": str(e), "tb": traceback.format_exc()})

try:
    from identity_strengthening import strengthen_brand
except Exception as e:
    errors.append({"module": "identity_strengthening", "error": str(e), "tb": traceback.format_exc()})

try:
    from identity_resolution import resolve_identity
except Exception as e:
    errors.append({"module": "identity_resolution", "error": str(e), "tb": traceback.format_exc()})

try:
    from openai import OpenAI
except Exception as e:
    errors.append({"module": "openai", "error": str(e), "tb": traceback.format_exc()})

try:
    from flask_cors import CORS
except Exception as e:
    errors.append({"module": "flask_cors", "error": str(e), "tb": traceback.format_exc()})

try:
    from openpyxl import Workbook
except Exception as e:
    errors.append({"module": "openpyxl", "error": str(e), "tb": traceback.format_exc()})

try:
    from bs4 import BeautifulSoup
except Exception as e:
    errors.append({"module": "bs4", "error": str(e), "tb": traceback.format_exc()})

try:
    import requests
except Exception as e:
    errors.append({"module": "requests", "error": str(e), "tb": traceback.format_exc()})

# Print errors to stdout for Railway logs
print(f"=== DIAGNOSTIC: {len(errors)} import errors ===", flush=True)
for err in errors:
    print(f"FAIL: {err['module']}: {err['error']}", flush=True)
    print(err['tb'], flush=True)
if not errors:
    print("ALL IMPORTS OK", flush=True)

# Print Python version and sys.path
print(f"Python: {sys.version}", flush=True)
print(f"sys.path: {sys.path}", flush=True)

from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({
        "status": "diagnostic",
        "python": sys.version,
        "total_errors": len(errors),
        "import_errors": errors
    })

@app.route('/health')
def health():
    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting diagnostic server on port {port}", flush=True)
    app.run(host='0.0.0.0', port=port)
else:
    # For gunicorn
    import gunicorn
    print(f"Diagnostic app loaded via gunicorn", flush=True)
