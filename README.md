# Scrape Buddy — v1

A simple web scraper app. Paste a supplier category page URL, click Scrape, and get a table of product data.

---

## File Structure

```
Scrape Buddy/
├── app.py              ← Flask server + /api/scrape endpoint
├── scraper.py          ← Fetch + parse logic
├── templates/
│   └── index.html      ← Frontend (URL input, Scrape button, results table)
├── requirements.txt    ← Python dependencies
└── README.md
```

---

## Setup

**Requires Python 3.8+**

```bash
# 1. (Optional but recommended) Create a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt
```

---

## Run

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

---

## How It Works

### 1. Frontend (`templates/index.html`)
- Plain HTML page served by Flask
- User pastes a URL and clicks "Scrape"
- JavaScript POSTs `{ "url": "..." }` to `/api/scrape`
- Results are rendered in a table (image, name, brand, price, SKU, link)

### 2. Backend (`app.py`)
- `GET /` — serves the frontend
- `POST /api/scrape` — validates the URL, calls the scraper, returns JSON

### 3. Scraper (`scraper.py`)
The scraper uses **CSS class heuristics** — a best-effort approach that works across many e-commerce sites without site-specific config:

1. **Fetch**: Downloads the page HTML using `requests` with a browser User-Agent header to avoid basic bot blocks.

2. **Find product cards**: Scans all elements for class names containing keywords like `product`, `item`, `card`, `listing`, `tile`. Groups them by element pattern and picks the most repeated pattern (avoids picking up wrapper divs).

3. **Extract fields** from each card:
   | Field          | Strategy |
   |----------------|----------|
   | `product_name` | Heading tags (h1–h5), then elements with "name"/"title" in class |
   | `brand`        | Elements with "brand"/"vendor"/"manufacturer" in class |
   | `price`        | Elements with "price"/"cost"/"amount" in class, or text with £/$€ |
   | `product_url`  | First `<a>` tag (resolved to absolute URL) |
   | `image_url`    | `<img>` src/data-src (handles lazy loading) |
   | `sku`          | `data-sku`/`data-product-id` attributes, or elements with "sku"/"mpn" in class |

4. **Clean up**: Missing fields default to `""`. Results are deduplicated by product URL.

### Limitations (v1)
- **No JavaScript rendering** — sites that load products via JS (React, Vue, etc.) won't work. Use Playwright in a future version.
- **No pagination** — only scrapes the single page provided.
- **Heuristic matching** — may miss products on heavily customised sites.
- **No proxy/rotation** — some sites may block repeated requests.

---

## Extending in Future Versions

- **JS-rendered pages**: Replace `requests` with Playwright (`pip install playwright`)
- **Pagination**: Loop through page numbers or "next" links
- **Site-specific scrapers**: Add a `sites/` directory with per-domain extractors
- **Structured data**: Parse JSON-LD (`<script type="application/ld+json">`) for richer data
- **Export**: Add a "Download CSV" button on the frontend
- **Database**: Store results with SQLite for history/comparison
