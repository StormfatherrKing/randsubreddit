#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes NSFW subreddits from two sources:
  1. subriff.com  — trending/fastest-growing NSFW communities (browser + DOM)
  2. nsfwdog.com  — established NSFW communities via direct API
                    API: api2.nsfwdog.com/v1/subreddits/top/?ordering=-subscribers&page=N
                    Returns 16 subreddits per page, sorted by subscriber count descending.
                    Capped at --nsfwdog-limit (default 10,000) — small/obscure subs
                    excluded for safety.

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
NSFWDOG_API_DELAY = 0.25   # seconds between API calls

# nsfwdog API — discovered from network sniffing
NSFWDOG_API = "https://api2.nsfwdog.com/v1/subreddits/top/?ordering=-subscribers&page={page}"
NSFWDOG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://nsfwdog.com/",
    "Origin": "https://nsfwdog.com",
}


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
# Extract subreddit names from a nsfwdog API response
# ---------------------------------------------------------------------------

def extract_names(data) -> list[str]:
    """
    Pull subreddit names out of a nsfwdog API response.
    Tries known field names first, then falls back to pattern matching.
    """
    names = []

    def walk(obj, depth=0):
        if depth > 8:
            return
        if isinstance(obj, dict):
            for key in ("name", "subreddit", "subreddit_name", "subredditName",
                        "display_name", "displayName", "slug", "id"):
                val = obj.get(key)
                if isinstance(val, str) and re.match(r'^[A-Za-z0-9_]{2,50}$', val.strip()):
                    names.append(val.strip())
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(data)

    # Fallback: scan raw string for /view/<name> or reddit.com/r/<name>
    raw = str(data)
    for m in re.finditer(r'/view/([A-Za-z0-9_]{2,50})', raw):
        if m.group(1).lower() != "example":
            names.append(m.group(1))
    for m in re.finditer(r'reddit\.com/r/([A-Za-z0-9_]{2,50})', raw):
        names.append(m.group(1))

    return dedupe(names)


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
# nsfwdog (direct API)
# ---------------------------------------------------------------------------

def scrape_nsfwdog(args: argparse.Namespace) -> list[str]:
    limit = args.nsfwdog_limit
    all_names: list[str] = []
    page_num = 1
    consecutive_empty = 0

    print(f"  [nsfwdog] Fetching via API (top {limit:,} by subscribers) …")

    while len(all_names) < limit:
        url = NSFWDOG_API.format(page=page_num)
        try:
            r = req_lib.get(url, headers=NSFWDOG_HEADERS, timeout=15)
            if r.status_code == 404 or r.status_code == 400:
                print(f"  [nsfwdog] API returned {r.status_code} at page {page_num} — done.")
                break
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [nsfwdog] Error on page {page_num}: {e}")
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            time.sleep(1)
            page_num += 1
            continue

        names = extract_names(data)

        if not names:
            consecutive_empty += 1
            print(f"  [nsfwdog] Page {page_num}: 0 names (empty streak: {consecutive_empty})")
            if consecutive_empty >= 3:
                print("  [nsfwdog] 3 consecutive empty pages — stopping.")
                break
        else:
            consecutive_empty = 0
            new = [n for n in names if n.lower() not in {x.lower() for x in all_names}]
            all_names.extend(new)
            if page_num % 100 == 0 or page_num <= 5:
                print(f"  [nsfwdog] Page {page_num}: {len(new)} new  "
                      f"(total: {len(all_names):,} / {limit:,})")

        page_num += 1
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
