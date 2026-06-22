#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes NSFW subreddits from two sources:
  1. subriff.com  — trending/fastest-growing NSFW communities (DOM + pagination)
  2. nsfwdog.com  — 89k+ established NSFW communities

For nsfwdog, we use a two-phase approach:
  Phase 1 — open the page in a browser, intercept ALL network responses to
             discover the real API endpoint and parameters.
  Phase 2 — hit that API directly with requests (no browser), paginating
             through all results. Much faster than clicking through pages.
  Fallback — if API sniffing fails, fall back to browser pagination (capped).

Merges both lists, applies a gay/trans keyword filter, and writes:
  NSFWsubreddits.txt  (one subreddit per line, no r/ prefix)

Dependencies:
    pip install playwright requests
    playwright install chromium

Usage:
    python subriff_nsfw_scraper.py
    python subriff_nsfw_scraper.py --nsfwdog-limit 10000   # top 10k by size
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

try:
    import requests as req_lib
except ImportError:
    req_lib = None  # handled at runtime if needed

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright not installed.\n"
        "Run:  pip install playwright requests && playwright install chromium"
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

PAGE_LOAD_WAIT      = 2_500   # ms — browser waits
NSFWDOG_API_TIMEOUT = 15      # seconds per requests call
NSFWDOG_API_DELAY   = 0.25    # seconds between API calls (be polite)
MAX_SUBRIFF_PAGES   = 50
MAX_NSFWDOG_BROWSER_PAGES = 100   # fallback only


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
# Subriff scraper (browser + DOM pagination)
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

        for page_num in range(1, MAX_SUBRIFF_PAGES + 1):
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
# nsfwdog — Phase 1: API sniffing
# ---------------------------------------------------------------------------

class ApiInfo:
    """Holds whatever we learned about the nsfwdog API from network interception."""
    def __init__(self):
        self.responses: list[dict] = []   # raw response dicts {url, headers, body}
        self.candidate_url: str = ""
        self.candidate_headers: dict = {}
        self.page_size: int = 0
        self.names_from_first_page: list[str] = []


def _looks_like_subreddit_list(data) -> list[str]:
    """
    Given a parsed JSON body, try to extract a list of subreddit names.
    Returns list of names found (empty if this doesn't look like subreddit data).
    """
    names = []
    raw = str(data)

    # Look for /view/<name> patterns (nsfwdog's own links embedded in JSON)
    for m in re.finditer(r'/view/([A-Za-z0-9_]{2,50})', raw):
        if m.group(1).lower() != "example":
            names.append(m.group(1))

    # Look for reddit.com/r/<name> patterns
    for m in re.finditer(r'reddit\.com/r/([A-Za-z0-9_]{2,50})', raw):
        names.append(m.group(1))

    # Walk JSON for common name-like fields
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


def sniff_nsfwdog_api(args: argparse.Namespace) -> ApiInfo:
    """
    Open nsfwdog in a browser for one page, intercept all JSON API responses,
    and try to identify the endpoint that returns the community list.
    """
    info = ApiInfo()
    captured = []   # list of (url, headers, parsed_body)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        def on_response(response):
            url = response.url
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = response.json()
                names = _looks_like_subreddit_list(body)
                if names:
                    captured.append({
                        "url": url,
                        "headers": dict(response.request.headers),
                        "body": body,
                        "names": names,
                    })
                    print(f"  [nsfwdog-sniff] Found {len(names)} names in: {url[:100]}")
            except Exception:
                pass

        page.on("response", on_response)

        print("  [nsfwdog] Phase 1 — sniffing API (loading first page) …")
        page.goto("https://nsfwdog.com/browse?dir=desc", wait_until="networkidle")
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        # Also grab from DOM as a baseline
        dom_names = []
        for link in page.locator("a[href*='/view/']").all():
            href = link.get_attribute("href") or ""
            m = re.search(r"/view/([A-Za-z0-9_]+)", href)
            if m and m.group(1).lower() != "example":
                dom_names.append(m.group(1))

        browser.close()

    info.names_from_first_page = dedupe(dom_names)

    if captured:
        # Pick the response with the most names
        best = max(captured, key=lambda x: len(x["names"]))
        info.candidate_url = best["url"]
        info.candidate_headers = best["headers"]
        info.page_size = len(best["names"])
        info.names_from_first_page = dedupe(
            info.names_from_first_page + best["names"]
        )
        print(f"  [nsfwdog] Best API candidate: {info.candidate_url[:100]}")
        print(f"  [nsfwdog] Page size detected: {info.page_size}")
    else:
        print("  [nsfwdog] No JSON API responses captured during sniffing.")
        print(f"  [nsfwdog] DOM gave {len(info.names_from_first_page)} names from first page.")

    return info


# ---------------------------------------------------------------------------
# nsfwdog — Phase 2: Direct API pagination with requests
# ---------------------------------------------------------------------------

def fetch_nsfwdog_via_api(info: ApiInfo, limit: int) -> list[str]:
    """
    Given a discovered API URL, paginate through it directly using requests.
    Tries common pagination patterns (page=N, offset=N, cursor, after).
    """
    if req_lib is None:
        print("  [nsfwdog] 'requests' not installed — cannot use direct API mode.")
        return info.names_from_first_page

    base_url = info.candidate_url
    headers = {k: v for k, v in info.candidate_headers.items()
               if k.lower() not in ("host", "cookie", "content-length")}

    all_names = list(info.names_from_first_page)
    page_size = max(info.page_size, 20)

    print(f"  [nsfwdog] Phase 2 — direct API pagination (limit={limit}) …")

    # Try page-based pagination first
    page_num = 2  # page 1 already captured
    consecutive_empty = 0

    while len(all_names) < limit:
        # Try different pagination params — nsfwdog may use any of these
        tried_urls = []
        for param in [f"page={page_num}", f"p={page_num}",
                      f"offset={(page_num-1)*page_size}", f"skip={(page_num-1)*page_size}"]:
            sep = "&" if "?" in base_url else "?"
            tried_urls.append(f"{base_url}{sep}{param}")

        got_names = []
        for url in tried_urls:
            try:
                r = req_lib.get(url, headers=headers, timeout=NSFWDOG_API_TIMEOUT)
                if r.status_code != 200:
                    continue
                data = r.json()
                names = _looks_like_subreddit_list(data)
                if names:
                    got_names = names
                    break
            except Exception:
                continue

        if not got_names:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print(f"  [nsfwdog] API pagination stopped at page {page_num} "
                      f"(3 empty responses).")
                break
        else:
            consecutive_empty = 0
            new = [n for n in got_names if n.lower() not in
                   {x.lower() for x in all_names}]
            all_names.extend(new)
            if page_num % 50 == 0:
                print(f"  [nsfwdog] Page {page_num}: {len(all_names)} total so far")

        page_num += 1
        time.sleep(NSFWDOG_API_DELAY)

    return dedupe(all_names)[:limit]


# ---------------------------------------------------------------------------
# nsfwdog — browser fallback (slower, capped)
# ---------------------------------------------------------------------------

def scrape_nsfwdog_browser_fallback(args: argparse.Namespace,
                                     seed_names: list[str],
                                     limit: int) -> list[str]:
    """
    Browser-based fallback: paginate through nsfwdog using Next buttons.
    Slower than the API approach but works regardless of API discovery.
    """
    all_names = list(seed_names)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        page.goto("https://nsfwdog.com/browse?dir=desc", wait_until="networkidle")
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        for page_num in range(2, MAX_NSFWDOG_BROWSER_PAGES + 1):
            if len(all_names) >= limit:
                print(f"  [nsfwdog] Reached limit of {limit}.")
                break

            nxt = page.locator("a:has-text('Next'), button:has-text('Next')").first
            if not nxt.is_visible():
                print("  [nsfwdog] No more pages.")
                break
            nxt.click()
            page.wait_for_timeout(PAGE_LOAD_WAIT)

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

            print(f"  [nsfwdog] Browser page {page_num}: {len(names)} subreddits")
            all_names.extend(names)

        browser.close()

    return dedupe(all_names)[:limit]


# ---------------------------------------------------------------------------
# nsfwdog orchestrator
# ---------------------------------------------------------------------------

def scrape_nsfwdog(args: argparse.Namespace) -> list[str]:
    limit = args.nsfwdog_limit

    # Phase 1: sniff
    info = sniff_nsfwdog_api(args)

    # Phase 2: use API if discovered, else browser fallback
    if info.candidate_url:
        all_names = fetch_nsfwdog_via_api(info, limit)
        # If API pagination gave nothing useful, fall back
        if len(all_names) <= len(info.names_from_first_page):
            print("  [nsfwdog] API pagination didn't yield more results — "
                  "switching to browser fallback.")
            all_names = scrape_nsfwdog_browser_fallback(
                args, info.names_from_first_page, limit
            )
    else:
        print("  [nsfwdog] No API found — using browser pagination fallback.")
        all_names = scrape_nsfwdog_browser_fallback(
            args, info.names_from_first_page, limit
        )

    result = dedupe(all_names)[:limit]
    print(f"  [nsfwdog] Total unique names: {len(result)}")
    return result


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
    p.add_argument("--min-subs", type=int, default=0,
                   help="subriff min subscriber filter")
    p.add_argument("--nsfwdog-limit", type=int, default=10_000,
                   help="Max subreddits to pull from nsfwdog (default 10000 — "
                        "sorted largest-first so you get the most active ones; "
                        "set to 0 for no limit)")
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
    if args.nsfwdog_limit == 0:
        args.nsfwdog_limit = 999_999  # effectively unlimited

    start = time.time()
    sources: list[list[str]] = []

    if not args.skip_subriff:
        print("\n=== Scraping subriff.com (trending NSFW) ===")
        sources.append(scrape_subriff(args))
    else:
        print("Skipping subriff.com")

    if not args.skip_nsfwdog:
        print(f"\n=== Scraping nsfwdog.com (limit: {args.nsfwdog_limit:,}) ===")
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
