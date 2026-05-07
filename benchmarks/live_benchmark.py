"""
Live launch-readiness benchmark for Scrape Buddy.

This runner exercises the scraper against public retailer, distributor, and
wholesale catalog pages with conservative limits. It is designed for validation,
not bulk data collection.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

os.environ.setdefault("SCRAPEBUDDY_BENCHMARK_MODE", "1")
os.environ.setdefault("SCRAPEBUDDY_DETAIL_LIMIT", "8")
os.environ.setdefault("SCRAPEBUDDY_LISTING_LIMIT", "3")
os.environ.setdefault("SCRAPEBUDDY_REQUEST_TIMEOUT", "8")
os.environ.setdefault("SCRAPEBUDDY_PLAYWRIGHT_TIMEOUT_MS", "18000")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from bs4 import BeautifulSoup

from pack_parser import enrich_all as enrich_all_pack
from scraper import HEADERS, fetch_html, fetch_html_playwright
from strategies import run_best_strategy
from strategies.product_quality import build_quality_report, classify_scrape_error, normalize_products


CATALOG_SIGNALS = (
    "shop", "catalog", "category", "categories", "collections", "products",
    "department", "departments", "supplies", "wholesale", "store", "all",
    "restaurant", "equipment", "office", "industrial", "grocery", "beauty",
)
REJECT_SIGNALS = (
    "account", "login", "signin", "register", "cart", "checkout", "wishlist",
    "blog", "about", "contact", "privacy", "terms", "careers", "locations",
    "support", "help", "track", "order-status", "gift-card",
)
STATUSES = ("pass", "partial", "fail", "blocked", "timeout", "error")


@dataclass
class Target:
    label: str
    url: str
    segment: str = ""
    notes: str = ""


@dataclass
class Result:
    label: str
    segment: str
    input_url: str
    tested_url: str
    status: str
    product_count: int
    strategy_name: str
    quality_score: float
    name_coverage: float
    price_coverage: float
    sku_coverage: float
    barcode_coverage: float
    url_coverage: float
    image_coverage: float
    elapsed_seconds: float
    warnings: str
    error: str = ""
    reason: str = ""
    discovery: str = ""


def load_targets(path: Path) -> list[Target]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        targets = []
        for row in reader:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            targets.append(
                Target(
                    label=(row.get("label") or urlparse(url).netloc).strip(),
                    url=url,
                    segment=(row.get("segment") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return targets


def filter_targets(
    targets: list[Target],
    *,
    segments: str = "",
    labels: str = "",
) -> list[Target]:
    selected = targets
    if segments:
        wanted = {segment.strip().lower() for segment in segments.split(",") if segment.strip()}
        selected = [target for target in selected if target.segment.strip().lower() in wanted]
    if labels:
        wanted_labels = {label.strip().lower() for label in labels.split(",") if label.strip()}
        selected = [target for target in selected if target.label.strip().lower() in wanted_labels]
    return selected


def same_site(base: str, candidate: str) -> bool:
    base_host = urlparse(base).netloc.lower().removeprefix("www.")
    cand_host = urlparse(candidate).netloc.lower().removeprefix("www.")
    return base_host == cand_host or cand_host.endswith("." + base_host)


def score_catalog_link(text: str, href: str) -> int:
    combined = f"{text} {urlparse(href).path}".lower()
    if any(sig in combined for sig in REJECT_SIGNALS):
        return -10
    score = 0
    for signal in CATALOG_SIGNALS:
        if signal in combined:
            score += 2
    if re.search(r"/(c|cat|category|categories|collections|shop|products?)(/|$)", combined):
        score += 5
    if "all products" in combined or "shop all" in combined:
        score += 6
    if urlparse(href).path in ("", "/"):
        score -= 3
    return score


def discover_catalog_url(seed_url: str, timeout: int = 12) -> tuple[str, str]:
    parsed = urlparse(seed_url)
    if parsed.path not in ("", "/"):
        return seed_url, "provided catalog-like URL"

    try:
        response = requests.get(seed_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        return seed_url, f"homepage discovery failed: {exc}"

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[tuple[int, str, str]] = []
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        href = urljoin(response.url, link["href"])
        parsed_href = urlparse(href)
        if parsed_href.scheme not in ("http", "https"):
            continue
        if not same_site(response.url, href):
            continue
        path = parsed_href.path.lower()
        if any(path.endswith(ext) for ext in (".jpg", ".png", ".gif", ".webp", ".pdf", ".zip")):
            continue
        score = score_catalog_link(text, href)
        if score > 0:
            candidates.append((score, href, text[:80]))

    if not candidates:
        return response.url, "no catalog link discovered; testing homepage"

    candidates.sort(key=lambda item: (item[0], -len(urlparse(item[1]).path)), reverse=True)
    score, href, text = candidates[0]
    return href, f"discovered from homepage: {text or urlparse(href).path} (score={score})"


def robots_allowed(url: str, user_agent: str, timeout: int = 8) -> tuple[bool, str]:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        response = requests.get(robots_url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            return True, f"robots status {response.status_code}"
        rp.parse(response.text.splitlines())
        return rp.can_fetch(user_agent, url), robots_url
    except Exception as exc:
        return True, f"robots unavailable: {exc}"


def pct(products: list[dict], field: str) -> float:
    if not products:
        return 0.0
    filled = sum(1 for product in products if str(product.get(field) or "").strip())
    return round(filled / len(products), 3)


def barcode_pct(products: list[dict]) -> float:
    if not products:
        return 0.0
    filled = sum(
        1
        for product in products
        if product.get("upc") or product.get("ean") or product.get("gtin") or product.get("gtin_case")
    )
    return round(filled / len(products), 3)


def classify_result(products: list[dict], diagnostics: dict, error: str = "") -> str:
    if error:
        category = classify_scrape_error(error)
        if category == "timeout":
            return "timeout"
        if category == "blocked":
            return "blocked"
        return "error"
    if not products:
        return "fail"
    if len(products) >= 3 and pct(products, "product_name") >= 0.7:
        if diagnostics.get("score", 0) >= 0.65:
            return "pass"
        return "partial"
    return "partial"


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    proc.kill()


def run_target(target: Target, args: argparse.Namespace) -> Result:
    started = time.perf_counter()
    tested_url = target.url
    discovery_note = ""
    try:
        if args.discover:
            tested_url, discovery_note = discover_catalog_url(target.url, timeout=args.timeout)

        if args.respect_robots:
            allowed, robots_note = robots_allowed(tested_url, HEADERS.get("User-Agent", "*"))
            if not allowed:
                elapsed = round(time.perf_counter() - started, 2)
                return Result(
                    label=target.label,
                    segment=target.segment,
                    input_url=target.url,
                    tested_url=tested_url,
                    status="blocked",
                    product_count=0,
                    strategy_name="",
                    quality_score=0.0,
                    name_coverage=0.0,
                    price_coverage=0.0,
                    sku_coverage=0.0,
                    barcode_coverage=0.0,
                    url_coverage=0.0,
                    image_coverage=0.0,
                    elapsed_seconds=elapsed,
                    warnings="robots disallow",
                    error=f"Robots.txt disallows benchmark fetch ({robots_note})",
                    discovery=discovery_note,
                )

        try:
            html = fetch_html(tested_url)
            used_playwright = False
        except Exception:
            if not args.playwright:
                raise
            html = fetch_html_playwright(tested_url, wait_ms=args.playwright_wait_ms)
            used_playwright = True

        result = run_best_strategy(html, tested_url, use_playwright=used_playwright)
        products = normalize_products(result.get("products", []))
        if args.pack_parse:
            enrich_all_pack(products)
            products = normalize_products(products)

        diagnostics = build_quality_report(
            products,
            strategy_name=result.get("strategy_name", ""),
        )
        warnings = diagnostics.get("warnings", [])
        elapsed = round(time.perf_counter() - started, 2)
        status = classify_result(products, diagnostics)
        return Result(
            label=target.label,
            segment=target.segment,
            input_url=target.url,
            tested_url=tested_url,
            status=status,
            product_count=len(products),
            strategy_name=result.get("strategy_name", ""),
            quality_score=float(diagnostics.get("score", 0.0)),
            name_coverage=pct(products, "product_name"),
            price_coverage=pct(products, "price"),
            sku_coverage=pct(products, "sku"),
            barcode_coverage=barcode_pct(products),
            url_coverage=pct(products, "product_url"),
            image_coverage=pct(products, "image_url"),
            elapsed_seconds=elapsed,
            warnings=" | ".join(warnings),
            reason=result.get("reason", ""),
            discovery=discovery_note,
        )
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 2)
        message = str(exc)
        diagnostics = {"score": 0.0}
        return Result(
            label=target.label,
            segment=target.segment,
            input_url=target.url,
            tested_url=tested_url,
            status=classify_result([], diagnostics, message),
            product_count=0,
            strategy_name="",
            quality_score=0.0,
            name_coverage=0.0,
            price_coverage=0.0,
            sku_coverage=0.0,
            barcode_coverage=0.0,
            url_coverage=0.0,
            image_coverage=0.0,
            elapsed_seconds=elapsed,
            warnings="",
            error=message[:800],
            discovery=discovery_note,
        )


def run_target_subprocess(target: Target, args: argparse.Namespace) -> Result:
    payload = json.dumps(asdict(target))
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--one-target-json",
        payload,
        "--timeout",
        str(args.timeout),
        "--detail-limit",
        str(args.detail_limit),
        "--listing-limit",
        str(args.listing_limit),
        "--playwright-wait-ms",
        str(args.playwright_wait_ms),
        "--request-timeout",
        str(args.request_timeout),
        "--playwright-timeout-ms",
        str(args.playwright_timeout_ms),
    ]
    cmd.append("--discover" if args.discover else "--no-discover")
    cmd.append("--playwright" if args.playwright else "--no-playwright")
    cmd.append("--respect-robots" if args.respect_robots else "--no-respect-robots")
    cmd.append("--pack-parse" if args.pack_parse else "--no-pack-parse")

    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(timeout=args.site_timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(process)
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            elapsed = round(time.perf_counter() - started, 2)
            return Result(
                label=target.label,
                segment=target.segment,
                input_url=target.url,
                tested_url=target.url,
                status="timeout",
                product_count=0,
                strategy_name="",
                quality_score=0.0,
                name_coverage=0.0,
                price_coverage=0.0,
                sku_coverage=0.0,
                barcode_coverage=0.0,
                url_coverage=0.0,
                image_coverage=0.0,
                elapsed_seconds=elapsed,
                warnings="",
                error=f"Site timed out after {args.site_timeout}s",
            )

        output = stdout.splitlines()
        json_line = next((line for line in reversed(output) if line.startswith("RESULT_JSON=")), "")
        if json_line:
            return Result(**json.loads(json_line.removeprefix("RESULT_JSON=")))
        message = (stderr or stdout or f"exit code {process.returncode}").strip()
        raise RuntimeError(message[:800])
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 2)
        return Result(
            label=target.label,
            segment=target.segment,
            input_url=target.url,
            tested_url=target.url,
            status="error",
            product_count=0,
            strategy_name="",
            quality_score=0.0,
            name_coverage=0.0,
            price_coverage=0.0,
            sku_coverage=0.0,
            barcode_coverage=0.0,
            url_coverage=0.0,
            image_coverage=0.0,
            elapsed_seconds=elapsed,
            warnings="",
            error=str(exc)[:800],
        )


def write_reports(results: Iterable[Result], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rows = [asdict(result) for result in results]
    csv_path = out_dir / f"live-benchmark-{stamp}.csv"
    json_path = out_dir / f"live-benchmark-{stamp}.json"
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return csv_path, json_path


def summarize(results: list[Result]) -> dict:
    total = len(results)
    by_status = {status: sum(1 for r in results if r.status == status) for status in STATUSES}
    avg_score = round(sum(r.quality_score for r in results) / total, 3) if total else 0.0
    avg_elapsed = round(sum(r.elapsed_seconds for r in results) / total, 2) if total else 0.0
    return {
        "total": total,
        "by_status": by_status,
        "avg_quality_score": avg_score,
        "avg_elapsed_seconds": avg_elapsed,
        "pass_rate": round(by_status.get("pass", 0) / total, 3) if total else 0.0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Scrape Buddy against live public catalog targets.")
    parser.add_argument("--targets", type=Path, default=ROOT / "benchmarks" / "live_targets.csv")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "benchmark_results")
    parser.add_argument("--limit", type=int, default=10, help="Number of targets to run. Use 200 for launch benchmark.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--segments", default="", help="Comma-separated segment names to include.")
    parser.add_argument("--labels", default="", help="Comma-separated target labels to include.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel site workers. Keep modest; targets are live websites.")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between sites in seconds.")
    parser.add_argument("--timeout", type=int, default=12, help="Homepage discovery timeout.")
    parser.add_argument("--request-timeout", type=int, default=8)
    parser.add_argument("--playwright-timeout-ms", type=int, default=18_000)
    parser.add_argument("--detail-limit", type=int, default=8)
    parser.add_argument("--listing-limit", type=int, default=3)
    parser.add_argument("--discover", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--playwright", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--playwright-wait-ms", type=int, default=2500)
    parser.add_argument("--site-timeout", type=int, default=120)
    parser.add_argument("--respect-robots", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--pack-parse", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--one-target-json", default="", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ["SCRAPEBUDDY_BENCHMARK_MODE"] = "1"
    os.environ["SCRAPEBUDDY_DETAIL_LIMIT"] = str(args.detail_limit)
    os.environ["SCRAPEBUDDY_LISTING_LIMIT"] = str(args.listing_limit)
    os.environ["SCRAPEBUDDY_REQUEST_TIMEOUT"] = str(args.request_timeout)
    os.environ["SCRAPEBUDDY_PLAYWRIGHT_TIMEOUT_MS"] = str(args.playwright_timeout_ms)
    os.environ["SCRAPEBUDDY_PLAYWRIGHT_WAIT_MS"] = str(args.playwright_wait_ms)

    if args.one_target_json:
        target = Target(**json.loads(args.one_target_json))
        result = run_target(target, args)
        print("RESULT_JSON=" + json.dumps(asdict(result), ensure_ascii=False), flush=True)
        return 0

    targets = filter_targets(
        load_targets(args.targets),
        segments=args.segments,
        labels=args.labels,
    )
    selected = targets[args.offset: args.offset + args.limit]
    if not selected:
        print("No targets selected.")
        return 2

    print(
        f"Running {len(selected)} live target(s) with "
        f"detail_limit={args.detail_limit}, listing_limit={args.listing_limit}, "
        f"delay={args.delay}s"
    )
    results: list[Result] = []
    if args.workers <= 1:
        for index, target in enumerate(selected, 1):
            print(f"[{index}/{len(selected)}] {target.label} - {target.url}", flush=True)
            result = run_target_subprocess(target, args) if args.site_timeout else run_target(target, args)
            results.append(result)
            print(
                f"  -> {result.status} | products={result.product_count} | "
                f"score={result.quality_score:.2f} | {result.strategy_name or result.error[:90]}",
                flush=True,
            )
            if index < len(selected) and args.delay > 0:
                time.sleep(args.delay)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {}
            for index, target in enumerate(selected, 1):
                print(f"[submit {index}/{len(selected)}] {target.label} - {target.url}", flush=True)
                future = executor.submit(
                    run_target_subprocess if args.site_timeout else run_target,
                    target,
                    args,
                )
                futures[future] = (index, target)
                if index < len(selected) and args.delay > 0:
                    time.sleep(args.delay)

            completed_count = 0
            for future in as_completed(futures):
                completed_count += 1
                index, target = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = Result(
                        label=target.label,
                        segment=target.segment,
                        input_url=target.url,
                        tested_url=target.url,
                        status="error",
                        product_count=0,
                        strategy_name="",
                        quality_score=0.0,
                        name_coverage=0.0,
                        price_coverage=0.0,
                        sku_coverage=0.0,
                        barcode_coverage=0.0,
                        url_coverage=0.0,
                        image_coverage=0.0,
                        elapsed_seconds=0.0,
                        warnings="",
                        error=str(exc)[:800],
                    )
                results.append(result)
                print(
                    f"[done {completed_count}/{len(selected)} | target {index}] "
                    f"{result.label}: {result.status} | products={result.product_count} | "
                    f"score={result.quality_score:.2f} | {result.strategy_name or result.error[:90]}",
                    flush=True,
                )

    csv_path, json_path = write_reports(results, args.out_dir)
    summary = summarize(results)
    print(json.dumps(summary, indent=2))
    print(f"CSV report:  {csv_path}")
    print(f"JSON report: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
