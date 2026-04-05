import os, json, traceback, sys
from flask import Flask, jsonify

app = Flask(__name__)
errors = []

modules = [
    "database",
    "browser_login",
    "upc_enrichment",
    "scraper",
    "strategies",
    "strategies.router",
    "strategies.listing",
    "strategies.detail",
    "strategies.page_classifier",
    "strategies.playwright_catalog",
    "upc_providers",
    "upc_providers.scorer",
    "upc_providers.openfoodfacts",
    "upc_providers.upcdatabase",
    "pack_parser",
    "target_selector",
]

for mod in modules:
    try:
        __import__(mod)
    except Exception as e:
        errors.append({"module": mod, "error": str(e), "type": type(e).__name__, "tb": traceback.format_exc()[-500:]})

@app.route("/")
def index():
    return jsonify({"status": "diagnostic", "python": sys.version, "total_errors": len(errors), "import_errors": errors})

@app.route("/health")
def health():
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
