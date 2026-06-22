#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes NSFW subreddits from two sources:
  1. subriff.com  — trending/fastest-growing NSFW communities
  2. nsfwdog.com  — established NSFW communities sorted by subscriber count (largest first)

nsfwdog has 89k+ subreddits indexed. We cap at the top 10,000 by default
(--nsfwdog-limit) because:
  - Sorted largest-first, the top 10k are all well-known active communities
  - Small obscure subreddits below that threshold are unknown quantities
    and could contain illegal content

Merges both lists, applies a gay/trans keyword filter, and writes:
  NSFWsubreddits.txt  (one subreddit per line, no r/ prefix)

Dependencies:
    pip install playwright requests
    playwright install chromium

Usage:
    python subriff_nsfw_scraper.py
    python subriff_nsfw_scraper.py --nsfwdog-limit 5000
    python subriff_nsfw_scraper.py --no-filter
    python subriff_nsfw_scraper.py --filter-log
    python subriff_nsfw_scraper.py --sort
    python subriff_nsfw_scraper.py --skip-subriff
    python subriff_nsfw_scraper.py --skip-nsfwdog
    python subriff_nsfw_scraper.py --visible
"""

import argparse
import re
import sys
import time
from pathlib import Path

# Force stdout to flush after every line so GitHub Actions logs update in real time
sys.stdout.reconfigure(line_buffering=True)

try:
    import requests as req_lib
except ImportError:
    req_lib = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Run:  pip install playwright requests && playwright install chromium")


# ---------------------------------------------------------------------------
# Filter configuration
# ---------------------------------------------------------------------------

GAY_KEYWORDS = [
    "gay",
    "twink",
    "yaoi",
    "bara",
]

TRANS_KEYWORDS = [
    "trans",
    "tgirl",
    "shemale",
    "ladyboy",
    "tranny",
    "futanari",
    "futa",
    "dickgirl",
    "femboy",
    "siss",
    "enby",
    "ftm",
    "mtf",
    "tgif",
]

USE_TRAP_FILTER = True
WHITELIST: set[str] = set()

SUBRIFF_WAIT      = 2_500
NSFWDOG_WAIT      = 1_000
NSFWDOG_API_DELAY = 0.25


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def should_filter(name: str) -> tuple[bool, str]:
    lower = name.lower()
    if lower in {w.lower() for w in WHITELIST}:
        return False, ""
    for kw in GAY_KEYWORDS:
        if kw in lower:
            return True, f"gay keyword '{kw}'"
    for kw in TRANS_KEYWORDS:
        if kw in lower:
            return True, f"trans keyword '{kw}'"
    if USE_TRAP_FILTER and "trap" in lower and "strap" not in lower:
        return True, "trans keyword 'trap'"
    return False, ""


def apply_filter(names: list[str], verbose: bool = False) -> list[str]:
    kept, removed = [], []
    for name in names:
        exclude, reason = should_filter(name)
        if exclude:
            removed.append((name, reason))
        else:
            kept.append(name)
    if verbose and removed:
        print(f"  Filtered out {len(removed)} subreddits:")
        for name, reason in removed:
            print(f"    - {name}  ({reason})")
    return kept


def merge(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for lst in lists:
        for name in lst:
            if name.lower() not in seen:
                seen.add(name.lower())
                result.append(name)
    return result


def dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Subriff
# ---------------------------------------------------------------------------

def scrape_subriff(args: argparse.Namespace) -> list[str]:
    all_names: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )).new_page()

        print("  [subriff] Opening subriff.com …")
        page.goto("https://subriff.com/", wait_until="networkidle")
        page.wait_for_timeout(SUBRIFF_WAIT)

        page.locator(f"a:has-text('{args.order.capitalize()}')").first.click()
        page.wait_for_timeout(SUBRIFF_WAIT)

        min_map = {0: None, 10_000: "10k", 50_000: "50k",
                   100_000: "100k", 500_000: "500k"}
        min_label = min_map.get(args.min_subs)
        if min_label:
            page.locator(f"a:has-text('{min_label}')").first.click()
            page.wait_for_timeout(SUBRIFF_WAIT)

        print("  [subriff] Enabling NSFW filter …")
        page.locator("a:has-text('NSFW')").first.click()
        page.wait_for_timeout(SUBRIFF_WAIT)

        page_num = 0
        while True:
            page_num += 1
            links = page.locator("table a[href*='/subreddit/']").all()
            names = []
            for link in links:
                href = link.get_attribute("href") or ""
                m = re.search(r"/subreddit/([^/?]+)", href)
                if m:
                    names.append(m.group(1))
            print(f"  [subriff] Page {page_num}: {len(names)}")
            all_names.extend(names)
            nxt = page.locator("a:has-text('Next')").first
            if not nxt.is_visible():
                break
            nxt.click()
            page.wait_for_timeout(SUBRIFF_WAIT)

        browser.close()

    print(f"  [subriff] Done — {len(all_names)} total")
    return all_names


# ---------------------------------------------------------------------------
# nsfwdog — API sniffing helpers
# ---------------------------------------------------------------------------

def _extract_names_from_data(data) -> list[str]:
    names = []
    raw = str(data)

    for m in re.finditer(r'/view/([A-Za-z0-9_]{2,50})', raw):
        if m.group(1).lower() != "example":
            names.append(m.group(1))

    for m in re.finditer(r'reddit\.com/r/([A-Za-z0-9_]{2,50})', raw):
        names.append(m.group(1))

    def walk(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            for key in ("name", "subreddit", "subredditName", "id", "slug",
                        "community", "displayName", "title"):
                val = obj.get(key)
                if isinstance(val, str) and re.match(r'^[A-Za-z0-9_]{2,50}$', val.strip()):
                    names.append(val.strip())
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    if isinstance(data, (dict, list)):
        walk(data)

    return dedupe(names)


# ---------------------------------------------------------------------------
# nsfwdog — Phase 1: sniff API on first page load
# ---------------------------------------------------------------------------

def sniff_nsfwdog_api(args: argparse.Namespace) -> dict:
    captured = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )).new_page()

        def on_response(response):
            try:
                if "json" not in response.headers.get("content-type", ""):
                    return
                body = response.json()
                names = _extract_names_from_data(body)
                if names:
                    captured.append({
                        "url": response.url,
                        "req_headers": dict(response.request.headers),
                        "names": names,
                        "count": len(names),
                    })
                    print(f"  [nsfwdog-sniff] {len(names)} names in {response.url[:90]}")
            except Exception:
                pass

        page.on("response", on_response)

        print("  [nsfwdog] Phase 1 — sniffing API …")
        page.goto("https://nsfwdog.com/browse?dir=desc", wait_until="networkidle")
        page.wait_for_timeout(NSFWDOG_WAIT)

        dom_names = []
        for link in page.locator("a[href*='/view/']").all():
            href = link.get_attribute("href") or ""
            m = re.search(r"/view/([A-Za-z0-9_]+)", href)
            if m and m.group(1).lower() != "example":
                dom_names.append(m.group(1))

        browser.close()

    result = {"dom_names": dedupe(dom_names), "api_url": "", "api_headers": {}, "page_size": 0}

    if captured:
        best = max(captured, key=lambda x: x["count"])
        result["api_url"] = best["url"]
        result["api_headers"] = {
            k: v for k, v in best["req_headers"].items()
            if k.lower() not in ("host", "cookie", "content-length")
        }
        result["page_size"] = best["count"]
        result["dom_names"] = dedupe(dom_names + best["names"])
        print(f"  [nsfwdog] API found: {best['url'][:90]}")
        print(f"  [nsfwdog] Page size: {best['count']}")
    else:
        print(f"  [nsfwdog] No JSON API detected. DOM gave {len(dom_names)} names.")

    return result


# ---------------------------------------------------------------------------
# nsfwdog — Phase 2: direct API pagination
# ---------------------------------------------------------------------------

def fetch_via_api(api_info: dict, limit: int) -> list[str]:
    if req_lib is None:
        print("  [nsfwdog] 'requests' not installed, skipping API phase.")
        return api_info["dom_names"]

    base_url = api_info["api_url"]
    headers = api_info["api_headers"]
    page_size = max(api_info["page_size"], 20)
    all_names = list(api_info["dom_names"])

    print(f"  [nsfwdog] Phase 2 — direct API pagination (target: {limit:,}) …")

    page_num = 2
    empty_streak = 0

    while len(all_names) < limit:
        got = []
        for param in [f"page={page_num}", f"p={page_num}",
                      f"offset={(page_num-1)*page_size}",
                      f"skip={(page_num-1)*page_size}"]:
            sep = "&" if "?" in base_url else "?"
            try:
                r = req_lib.get(f"{base_url}{sep}{param}",
                                headers=headers, timeout=15)
                if r.status_code == 200:
                    names = _extract_names_from_data(r.json())
                    if names:
                        got = names
                        break
            except Exception:
                continue

        if not got:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"  [nsfwdog] API stopped yielding at page {page_num}.")
                break
        else:
            empty_streak = 0
            new = [n for n in got if n.lower() not in {x.lower() for x in all_names}]
            all_names.extend(new)
            if page_num % 50 == 0:
                print(f"  [nsfwdog] Page {page_num}: {len(all_names):,} total")

        page_num += 1
        time.sleep(NSFWDOG_API_DELAY)

    return dedupe(all_names)[:limit]


# ---------------------------------------------------------------------------
# nsfwdog — browser fallback
# ---------------------------------------------------------------------------

def fetch_via_browser(args: argparse.Namespace, seed: list[str], limit: int) -> list[str]:
    all_names = list(seed)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        page = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )).new_page()

        page.goto("https://nsfwdog.com/browse?dir=desc", wait_until="networkidle")
        page.wait_for_timeout(NSFWDOG_WAIT)

        page_num = 1
        while len(all_names) < limit:
            page_num += 1
            nxt = page.locator("a:has-text('Next'), button:has-text('Next')").first
            if not nxt.is_visible():
                print("  [nsfwdog] No more pages.")
                break
            nxt.click()
            page.wait_for_timeout(NSFWDOG_WAIT)

            try:
                page.wait_for_selector("a[href*='/view/']", timeout=8_000)
            except Exception:
                break

            names = []
            for link in page.locator("a[href*='/view/']").all():
                href = link.get_attribute("href") or ""
                m = re.search(r"/view/([A-Za-z0-9_]+)", href)
                if m and m.group(1).lower() != "example":
                    names.append(m.group(1))

            print(f"  [nsfwdog] Browser page {page_num}: {len(names)}  "
                  f"(total: {len(all_names) + len(names):,} / {limit:,})")
            all_names.extend(names)

        browser.close()

    return dedupe(all_names)[:limit]


# ---------------------------------------------------------------------------
# nsfwdog orchestrator
# ---------------------------------------------------------------------------

def scrape_nsfwdog(args: argparse.Namespace) -> list[str]:
    limit = args.nsfwdog_limit
    print(f"  [nsfwdog] Collecting top {limit:,} subreddits (sorted by size) …")

    api_info = sniff_nsfwdog_api(args)

    if api_info["api_url"]:
        result = fetch_via_api(api_info, limit)
        if len(result) <= len(api_info["dom_names"]) + 50:
            print("  [nsfwdog] API didn't paginate — switching to browser fallback.")
            result = fetch_via_browser(args, result, limit)
    else:
        print("  [nsfwdog] No API found — using browser pagination.")
        result = fetch_via_browser(args, api_info["dom_names"], limit)

    result = dedupe(result)[:limit]
    print(f"  [nsfwdog] Done — {len(result):,} unique subreddits")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="NSFWsubreddits.txt")
    p.add_argument("--sort", action="store_true")
    p.add_argument("--order", choices=["daily", "weekly", "monthly", "yearly"],
                   default="daily")
    p.add_argument("--min-subs", type=int, default=0)
    p.add_argument("--nsfwdog-limit", type=int, default=10_000,
                   help="Max subreddits from nsfwdog sorted largest-first (default: 10000)")
    p.add_argument("--no-filter", action="store_true")
    p.add_argument("--filter-log", action="store_true")
    p.add_argument("--skip-subriff", action="store_true")
    p.add_argument("--skip-nsfwdog", action="store_true")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--visible", action="store_false", dest="headless")
    return p.parse_args()


def write_output(names: list[str], path: str, sort: bool) -> None:
    if sort:
        names = sorted(names, key=str.lower)
    Path(path).write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"\nWrote {len(names)} subreddits to {Path(path).resolve()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    start = time.time()
    sources: list[list[str]] = []

    if not args.skip_subriff:
        print("\n=== subriff.com ===")
        sources.append(scrape_subriff(args))

    if not args.skip_nsfwdog:
        print(f"\n=== nsfwdog.com (top {args.nsfwdog_limit:,} by size) ===")
        sources.append(scrape_nsfwdog(args))

    combined = merge(*sources)
    print(f"\nCombined (before filter): {len(combined)}")

    if args.no_filter:
        final = combined
    else:
        print("Applying gay/trans filter …")
        final = apply_filter(combined, verbose=args.filter_log)
        print(f"  {len(combined)} → {len(final)} kept "
              f"({len(combined) - len(final)} removed)")

    write_output(final, args.out, args.sort)
    print(f"Done in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
