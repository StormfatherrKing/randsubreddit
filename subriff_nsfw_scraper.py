#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes NSFW subreddits from two sources:
  1. subriff.com  — trending/fastest-growing NSFW communities (browser + DOM)
  2. nsfwdog.com  — 89k+ established communities via direct API
                    api2.nsfwdog.com/v1/subreddits/top/?ordering=-subscribers
                    Standard DRF pagination, 16 results per page.
                    Capped at --nsfwdog-limit (default 10,000) — sorted
                    largest-first so small/unknown subs are excluded.

Merges both lists, applies a gay/trans keyword filter, and writes:
  NSFWsubreddits.txt  (one subreddit per line, no r/ prefix)

Dependencies:
    pip install playwright requests
    playwright install chromium
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
    sys.exit("Run: pip install playwright requests && playwright install chromium")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Run: pip install playwright requests && playwright install chromium")


# ---------------------------------------------------------------------------
# Filter configuration
# ---------------------------------------------------------------------------

GAY_KEYWORDS = ["gay", "twink", "yaoi", "bara"]

TRANS_KEYWORDS = [
    "trans", "tgirl", "shemale", "ladyboy", "tranny",
    "futanari", "futa", "dickgirl", "femboy", "siss",
    "enby", "ftm", "mtf", "tgif",
]

USE_TRAP_FILTER = True
WHITELIST: set[str] = set()

SUBRIFF_WAIT      = 2_500
NSFWDOG_API_DELAY = 0.25

NSFWDOG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://nsfwdog.com/",
    "Origin": "https://nsfwdog.com",
}

NSFWDOG_START_URL = (
    "https://api2.nsfwdog.com/v1/subreddits/top/?ordering=-subscribers&page=1"
)


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
# Extract subreddit names from one page of results
# ---------------------------------------------------------------------------

SUBREDDIT_NAME_FIELDS = (
    "name", "subreddit_name", "subredditName",
    "display_name", "displayName", "slug",
)

def extract_from_results(data: dict, log_fields: bool = False) -> list[str]:
    """
    Extract subreddit names ONLY from the 'results' array in a DRF response.
    Each item in results is one subreddit — we grab its name field directly
    rather than walking the whole tree.
    """
    results = data.get("results", [])

    if log_fields and results:
        first = results[0] if isinstance(results[0], dict) else {}
        print(f"  [nsfwdog] Result item keys: {list(first.keys())}")
        print(f"  [nsfwdog] Sample item: { {k: str(v)[:40] for k, v in list(first.items())[:5]} }")

    names = []
    for item in results:
        if not isinstance(item, dict):
            continue
        for field in SUBREDDIT_NAME_FIELDS:
            val = item.get(field)
            if val and isinstance(val, str) and re.match(r'^[A-Za-z0-9_]{2,50}$', val.strip()):
                names.append(val.strip())
                break  # only take one name per result item

    return names


# ---------------------------------------------------------------------------
# Subriff (browser)
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
# nsfwdog (direct API, targeted extraction)
# ---------------------------------------------------------------------------

def scrape_nsfwdog(args: argparse.Namespace) -> list[str]:
    limit = args.nsfwdog_limit
    all_names: list[str] = []
    next_url: str | None = NSFWDOG_START_URL
    page_num = 0
    consecutive_empty = 0

    print(f"  [nsfwdog] Fetching top {limit:,} subreddits via API …")

    while next_url and len(all_names) < limit:
        page_num += 1

        try:
            r = req_lib.get(next_url, headers=NSFWDOG_HEADERS, timeout=15)
            if r.status_code in (400, 404):
                print(f"  [nsfwdog] API returned {r.status_code} — stopping.")
                break
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [nsfwdog] Error on page {page_num}: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            time.sleep(2)
            continue

        # Log the structure of page 1 and the item fields
        log_fields = (page_num == 1)
        if page_num == 1:
            print(f"  [nsfwdog] Total indexed: {data.get('count', '?'):,}")

        names = extract_from_results(data, log_fields=log_fields)
        next_url = data.get("next")  # follow DRF's own next URL

        if not names:
            consecutive_empty += 1
            if consecutive_empty >= 5:
                print(f"  [nsfwdog] 5 consecutive empty pages — stopping.")
                break
        else:
            consecutive_empty = 0
            all_names.extend(names)

        if page_num <= 3 or page_num % 100 == 0:
            print(f"  [nsfwdog] Page {page_num}: {len(names)} names  "
                  f"(total: {len(all_names):,} / {limit:,})")

        time.sleep(NSFWDOG_API_DELAY)

    result = dedupe(all_names)[:limit]
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
                   help="Max subreddits from nsfwdog (default: 10000)")
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
