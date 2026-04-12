"""
database.py — SQLite persistence for The Syndicate UPC Scraper

Tables:
  scrape_runs  — one row per scrape job (label, url, timestamp, strategy, count)
  scrape_items — one row per product, linked to a run via run_id

Usage:
  import database
  database.init_db()          # call once at app startup
  run_id = database.save_scrape(label, url, strategy_id, strategy_name, products)
  runs   = database.get_history()
  run    = database.get_run(run_id)    # includes "products" list
  database.update_label(run_id, new_label)
  database.delete_run(run_id)

The DB file is created at scrape_history.db in the project root.
To add new product fields: add a column to scrape_items and update
save_scrape() + _row_to_product() below — no other files need to change.
"""

import sqlite3
import os
from datetime import datetime, timezone

# Database file lives in the same directory as this module
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape_history.db")


def get_connection() -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # rows accessible as dicts
    conn.execute("PRAGMA foreign_keys = ON")  # enforce ON DELETE CASCADE
    return conn


def _migrate_db(conn: sqlite3.Connection) -> None:
    """
    Add columns introduced in later versions to an existing database.
    SQLite does not support IF NOT EXISTS for ALTER TABLE, so we catch
    the OperationalError that fires when the column already exists.
    """
    new_columns = [
        ("scrape_items", "upc_source",            "TEXT DEFAULT ''"),
        ("scrape_items", "upc_match_confidence",   "TEXT DEFAULT ''"),
        ("scrape_items", "upc_enriched",           "TEXT DEFAULT '0'"),
        ("scrape_items", "missing_upc",            "TEXT DEFAULT '0'"),
        ("scrape_items", "upc_confidence_color",   "TEXT DEFAULT ''"),
        ("scrape_items", "upc_match_reason",       "TEXT DEFAULT ''"),
        ("scrape_items", "raw_pack_text",          "TEXT DEFAULT ''"),
        ("scrape_items", "unit_measure",           "TEXT DEFAULT ''"),
        ("scrape_items", "pack_confidence",        "TEXT DEFAULT ''"),
        # detail-page structured fields
        ("scrape_items", "unit_size",          "TEXT DEFAULT ''"),
        ("scrape_items", "unit_price",         "TEXT DEFAULT ''"),
        ("scrape_items", "pricing_unit",       "TEXT DEFAULT ''"),
        # multi-supplier extraction fields
        ("scrape_items", "bulk_price",         "TEXT DEFAULT ''"),
        ("scrape_items", "minimum_order_qty",  "TEXT DEFAULT ''"),
        ("scrape_items", "raw_price_text",     "TEXT DEFAULT ''"),
        ("scrape_items", "gtin_case",          "TEXT DEFAULT ''"),
        # scrape_runs — job lifecycle columns (DEFAULT 'completed' keeps old rows valid)
        ("scrape_runs",  "status",        "TEXT DEFAULT 'completed'"),
        ("scrape_runs",  "started_at",    "TEXT DEFAULT ''"),
        ("scrape_runs",  "finished_at",   "TEXT DEFAULT ''"),
        ("scrape_runs",  "error_message", "TEXT DEFAULT ''"),
    ]
    for table, column, typedef in new_columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
        except sqlite3.OperationalError:
            pass   # column already exists — safe to ignore


def init_db() -> None:
    """
    Create the database tables if they don't already exist.
    Safe to call on every app startup — uses IF NOT EXISTS.
    """
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                label         TEXT    NOT NULL,
                source_url    TEXT    NOT NULL,
                timestamp     TEXT    NOT NULL,   -- ISO-8601 UTC
                strategy_id   INTEGER,
                strategy_name TEXT,
                product_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scrape_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        INTEGER NOT NULL
                                  REFERENCES scrape_runs(id) ON DELETE CASCADE,
                product_name  TEXT DEFAULT '',
                brand         TEXT DEFAULT '',
                sku           TEXT DEFAULT '',
                upc           TEXT DEFAULT '',
                price         TEXT DEFAULT '',
                pack_size     TEXT DEFAULT '',
                case_pack     TEXT DEFAULT '',
                image_url     TEXT DEFAULT '',
                product_url   TEXT DEFAULT ''
                -- Add new product fields here in future versions
            );
        """)

        # Migrate: add enrichment columns if this is an existing database
        _migrate_db(conn)


# ── Write operations ──────────────────────────────────────────────────────────

def save_scrape(
    label: str,
    source_url: str,
    strategy_id: int,
    strategy_name: str,
    products: list,
) -> int:
    """
    Persist a completed scrape run and all its products.
    Returns the new run_id.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO scrape_runs
               (label, source_url, timestamp, strategy_id, strategy_name, product_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (label, source_url, timestamp, strategy_id, strategy_name, len(products)),
        )
        run_id = cur.lastrowid

        # Bulk-insert all product rows for this run
        conn.executemany(
            """INSERT INTO scrape_items
               (run_id, product_name, brand, sku, upc, price, pack_size, case_pack,
                image_url, product_url,
                upc_source, upc_match_confidence, upc_enriched, missing_upc,
                upc_confidence_color, upc_match_reason,
                raw_pack_text, unit_measure, pack_confidence,
                unit_size, unit_price, pricing_unit,
                bulk_price, minimum_order_qty, raw_price_text, gtin_case)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id,
                    p.get("product_name", ""),
                    p.get("brand", ""),
                    p.get("sku", ""),
                    p.get("upc", ""),
                    p.get("price", ""),
                    p.get("pack_size", ""),
                    p.get("case_pack", ""),
                    p.get("image_url", ""),
                    p.get("product_url", ""),
                    p.get("upc_source", ""),
                    p.get("upc_match_confidence", ""),
                    p.get("upc_enriched", "0"),
                    p.get("missing_upc", "0"),
                    p.get("upc_confidence_color", ""),
                    p.get("upc_match_reason", ""),
                    p.get("raw_pack_text", ""),
                    p.get("unit_measure", ""),
                    p.get("pack_confidence", ""),
                    p.get("unit_size", ""),
                    p.get("unit_price", ""),
                    p.get("pricing_unit", ""),
                    p.get("bulk_price", ""),
                    p.get("minimum_order_qty", ""),
                    p.get("raw_price_text", ""),
                    p.get("gtin_case", ""),
                )
                for p in products
            ],
        )

    return run_id


def update_label(run_id: int, label: str) -> bool:
    """Rename a scrape run. Returns True if a row was updated."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE scrape_runs SET label = ? WHERE id = ?", (label.strip(), run_id)
        )
    return cur.rowcount > 0


def delete_run(run_id: int) -> bool:
    """
    Delete a scrape run and all its products (cascade).
    Returns True if a row was deleted.
    """
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM scrape_runs WHERE id = ?", (run_id,))
    return cur.rowcount > 0


# ── Async job helpers ─────────────────────────────────────────────────────────

def create_run(label: str, source_url: str) -> int:
    """
    Create a scrape run record in 'running' status before the scrape starts.
    Returns the new run_id immediately so the UI can track progress.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO scrape_runs
               (label, source_url, timestamp, status, started_at, product_count)
               VALUES (?, ?, ?, 'running', ?, 0)""",
            (label, source_url, timestamp, timestamp),
        )
    return cur.lastrowid


def complete_run(
    run_id:        int,
    strategy_id:   int,
    strategy_name: str,
    products:      list,
) -> None:
    """
    Mark a run as completed and bulk-insert its products.
    If the run was deleted before this is called, the insert is silently skipped.
    """
    finished_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            conn.execute(
                """UPDATE scrape_runs
                   SET status='completed', finished_at=?, strategy_id=?,
                       strategy_name=?, product_count=?
                   WHERE id=?""",
                (finished_at, strategy_id, strategy_name, len(products), run_id),
            )
            conn.executemany(
                """INSERT INTO scrape_items
                   (run_id, product_name, brand, sku, upc, price, pack_size, case_pack,
                    image_url, product_url,
                    upc_source, upc_match_confidence, upc_enriched, missing_upc,
                    upc_confidence_color, upc_match_reason,
                    raw_pack_text, unit_measure, pack_confidence,
                    unit_size, unit_price, pricing_unit,
                    bulk_price, minimum_order_qty, raw_price_text, gtin_case)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        p.get("product_name", ""),
                        p.get("brand", ""),
                        p.get("sku", ""),
                        p.get("upc", ""),
                        p.get("price", ""),
                        p.get("pack_size", ""),
                        p.get("case_pack", ""),
                        p.get("image_url", ""),
                        p.get("product_url", ""),
                        p.get("upc_source", ""),
                        p.get("upc_match_confidence", ""),
                        p.get("upc_enriched", "0"),
                        p.get("missing_upc", "0"),
                        p.get("upc_confidence_color", ""),
                        p.get("upc_match_reason", ""),
                        p.get("raw_pack_text", ""),
                        p.get("unit_measure", ""),
                        p.get("pack_confidence", ""),
                        p.get("unit_size", ""),
                        p.get("unit_price", ""),
                        p.get("pricing_unit", ""),
                        p.get("bulk_price", ""),
                        p.get("minimum_order_qty", ""),
                        p.get("raw_price_text", ""),
                        p.get("gtin_case", ""),
                    )
                    for p in products
                ],
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"[complete_run] run_id={run_id} could not be saved (may have been deleted): {e}"
        )


def fail_run(run_id: int, error_message: str) -> None:
    """Mark a run as failed with an error message."""
    finished_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """UPDATE scrape_runs
               SET status='failed', finished_at=?, error_message=?
               WHERE id=?""",
            (finished_at, str(error_message)[:500], run_id),
        )


# ── Read operations ───────────────────────────────────────────────────────────

def get_history() -> list:
    """
    Return all scrape runs, newest first, without product rows.
    Each entry: {id, label, source_url, timestamp, strategy_name, product_count}
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, label, source_url, timestamp, strategy_name, product_count,
                      status, error_message
               FROM scrape_runs
               ORDER BY id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: int) -> dict | None:
    """
    Return a single scrape run including its product rows.
    Returns None if the run_id doesn't exist.
    """
    with get_connection() as conn:
        run = conn.execute(
            "SELECT * FROM scrape_runs WHERE id = ?", (run_id,)
        ).fetchone()

        if run is None:
            return None

        items = conn.execute(
            """SELECT product_name, brand, sku, upc, price, pack_size,
                      case_pack, image_url, product_url,
                      upc_source, upc_match_confidence, upc_enriched, missing_upc,
                      upc_confidence_color, upc_match_reason,
                      raw_pack_text, unit_measure, pack_confidence,
                      unit_size, unit_price, pricing_unit,
                      bulk_price, minimum_order_qty, raw_price_text, gtin_case
               FROM scrape_items WHERE run_id = ? ORDER BY id""",
            (run_id,),
        ).fetchall()

    return {
        **dict(run),
        "products": [dict(i) for i in items],
    }
