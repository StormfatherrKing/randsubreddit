#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes NSFW subreddits from two sources:
  1. subriff.com  — trending/fastest-growing NSFW communities (DOM scrape)
  2. nsfwdog.com  — established NSFW communities (Firestore API interception)

nsfwdog is built on Firebase/Firestore. Instead of scraping HTML, we intercept
the network requests the site makes to the Firestore API and extract the raw
JSON data. This is more reliable than DOM scraping and captures everything the
site has indexed regardless of scroll position or pagination.

Merges both lists, applies a gay/trans keyword filter, and writes:
  NSFWsubreddits.txt  (one subreddit per line, no r/ prefix)

Dependencies:
    pip install playwright
    playwright install chromium

Usage:
    python subriff_nsfw_scraper.py
    python subriff_nsfw_scraper.py --no-filter
    python subriff_nsfw_scraper.py --filter-log
    python subriff_nsfw_scraper.py --sort
    python subriff_nsfw_scraper.py --skip-subriff
    python subriff_nsfw_scraper.py --skip-nsfwdog
    python subriff_nsfw_scraper.py --visible
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, Route, Request
except ImportError:
    sys.exit(
        "playwright not installed.\n"
        "Run:  pip install playwright && playwright install chromium"
    )


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

PAGE_LOAD_WAIT  = 2_500
SCROLL_WAIT     = 1_200
MAX_PAGES       = 50
MAX_SCROLLS     = 80


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


# ---------------------------------------------------------------------------
# Subriff scraper (DOM)
# ---------------------------------------------------------------------------

def subriff_extract_page(page) -> list[str]:
    links = page.locator("table a[href*='/subreddit/']").all()
    names = []
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/subreddit/([^/?]+)", href)
        if m:
            names.append(m.group(1))
    return names


def scrape_subriff(args: argparse.Namespace) -> list[str]:
    all_names: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        print("  [subriff] Opening https://subriff.com …")
        page.goto("https://subriff.com/", wait_until="networkidle")
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        page.locator(f"a:has-text('{args.order.capitalize()}')").first.click()
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        min_map = {0: None, 10_000: "10k", 50_000: "50k",
                   100_000: "100k", 500_000: "500k"}
        min_label = min_map.get(args.min_subs)
        if min_label:
            page.locator(f"a:has-text('{min_label}')").first.click()
            page.wait_for_timeout(PAGE_LOAD_WAIT)

        print("  [subriff] Enabling NSFW filter …")
        page.locator("a:has-text('NSFW')").first.click()
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        for page_num in range(1, MAX_PAGES + 1):
            names = subriff_extract_page(page)
            print(f"  [subriff] Page {page_num}: {len(names)} subreddits")
            all_names.extend(names)
            nxt = page.locator("a:has-text('Next')").first
            if not nxt.is_visible():
                print("  [subriff] No more pages.")
                break
            nxt.click()
            page.wait_for_timeout(PAGE_LOAD_WAIT)

        browser.close()

    print(f"  [subriff] Total scraped: {len(all_names)}")
    return all_names


# ---------------------------------------------------------------------------
# nsfwdog scraper (Firestore network interception)
# ---------------------------------------------------------------------------

def extract_subreddit_names_from_firestore_json(data: dict | list) -> list[str]:
    """
    Walk any Firestore REST response and pull out subreddit names.
    Firestore REST responses look like:
      {"documents": [{"fields": {"name": {"stringValue": "gonewild"}, ...}}, ...]}
    or a single document:
      {"fields": {"name": {"stringValue": "gonewild"}, ...}}
    We also try "subreddit", "subredditName", "title", "id", "slug" fields.
    Additionally we look for reddit.com/r/<name> URLs embedded in the data.
    """
    names = []
    raw_str = json.dumps(data)

    # Pattern 1: reddit.com/r/<name> URLs anywhere in the JSON
    for m in re.finditer(r'reddit\.com/r/([A-Za-z0-9_]{2,50})', raw_str):
        names.append(m.group(1))

    # Pattern 2: common field names that likely hold the subreddit name
    name_fields = {"name", "subreddit", "subredditName", "subreddit_name",
                   "title", "id", "slug", "community", "displayName"}

    def walk(obj):
        if isinstance(obj, dict):
            # Firestore stringValue wrapper
            for key, val in obj.items():
                if key in name_fields and isinstance(val, dict) and "stringValue" in val:
                    candidate = val["stringValue"]
                    # Must look like a subreddit name (no spaces, reasonable length)
                    if re.match(r'^[A-Za-z0-9_]{2,50}$', candidate):
                        names.append(candidate)
                elif key in name_fields and isinstance(val, str):
                    candidate = val.strip().lstrip("r/")
                    if re.match(r'^[A-Za-z0-9_]{2,50}$', candidate):
                        names.append(candidate)
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return names


def scrape_nsfwdog(args: argparse.Namespace) -> list[str]:
    """
    Open nsfwdog.com/browse?dir=desc, intercept all Firestore API calls,
    capture every JSON response, scroll to trigger lazy loads, and extract
    subreddit names from the raw Firestore data.
    """
    all_names: list[str] = []
    captured_responses: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        # Intercept responses from Firestore and any generic /api/ endpoints
        def handle_response(response):
            url = response.url
            # Capture Firestore REST calls and any JSON API that looks data-like
            if any(pattern in url for pattern in [
                "firestore.googleapis.com",
                "firebaseio.com",
                "/api/",
                "firebase.com",
            ]):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        data = response.json()
                        captured_responses.append(data)
                        names = extract_subreddit_names_from_firestore_json(data)
                        if names:
                            print(f"  [nsfwdog] API response → {len(names)} names found  ({url[:80]}…)")
                except Exception:
                    pass

        page.on("response", handle_response)

        url = "https://nsfwdog.com/browse?dir=desc"
        print(f"  [nsfwdog] Opening {url} …")
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        # Scroll to trigger any lazy loading / pagination
        print(f"  [nsfwdog] Scrolling to load all content (up to {MAX_SCROLLS} scrolls) …")
        last_count = 0
        stale = 0
        for i in range(MAX_SCROLLS):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(SCROLL_WAIT)

            # Also try clicking any "load more" buttons
            for btn_text in ["Load more", "Show more", "Next", "Load More"]:
                try:
                    btn = page.locator(
                        f"button:has-text('{btn_text}'), a:has-text('{btn_text}')"
                    ).first
                    if btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(PAGE_LOAD_WAIT)
                        print(f"  [nsfwdog] Clicked '{btn_text}'")
                except Exception:
                    pass

            # Count unique names so far
            current = set()
            for resp_data in captured_responses:
                for n in extract_subreddit_names_from_firestore_json(resp_data):
                    current.add(n.lower())

            if len(current) == last_count:
                stale += 1
                if stale >= 5:
                    print(f"  [nsfwdog] No new data after 5 scrolls — done.")
                    break
            else:
                stale = 0
                if i % 10 == 0:
                    print(f"  [nsfwdog] Scroll {i+1}: {len(current)} unique names so far")
            last_count = len(current)

        # Also do a final DOM extraction as a backup
        # Look for any reddit.com/r/ links that were rendered
        try:
            for link in page.locator("a[href*='reddit.com/r/']").all():
                href = link.get_attribute("href") or ""
                m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", href)
                if m:
                    all_names.append(m.group(1))
            if all_names:
                print(f"  [nsfwdog] DOM fallback found {len(all_names)} names from rendered links")
        except Exception:
            pass

        browser.close()

    # Combine from API interception
    for resp_data in captured_responses:
        all_names.extend(extract_subreddit_names_from_firestore_json(resp_data))

    # Deduplicate
    seen: set[str] = set()
    unique = []
    for n in all_names:
        if n.lower() not in seen:
            seen.add(n.lower())
            unique.append(n)

    print(f"  [nsfwdog] Total unique names captured: {len(unique)}")

    if not unique:
        print("  [nsfwdog] WARNING: No subreddits captured from nsfwdog.")
        print("  [nsfwdog] Run with --visible to watch what's happening in the browser.")

    return unique


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape NSFW subreddits from subriff.com + nsfwdog.com"
    )
    p.add_argument("--out", default="NSFWsubreddits.txt")
    p.add_argument("--sort", action="store_true")
    p.add_argument("--order", choices=["daily", "weekly", "monthly", "yearly"],
                   default="daily")
    p.add_argument("--min-subs", type=int, default=0)
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
        print("\n=== Scraping subriff.com (trending NSFW) ===")
        sources.append(scrape_subriff(args))
    else:
        print("Skipping subriff.com")

    if not args.skip_nsfwdog:
        print("\n=== Scraping nsfwdog.com (established NSFW, via API interception) ===")
        sources.append(scrape_nsfwdog(args))
    else:
        print("Skipping nsfwdog.com")

    combined = merge(*sources)
    print(f"\nCombined (before filter): {len(combined)}")

    if args.no_filter:
        final = combined
        print("Filtering disabled.")
    else:
        print("Applying gay/trans keyword filter …")
        final = apply_filter(combined, verbose=args.filter_log)
        print(f"  {len(combined)} → {len(final)} kept  "
              f"({len(combined) - len(final)} removed)")

    write_output(final, args.out, args.sort)
    print(f"Done in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
