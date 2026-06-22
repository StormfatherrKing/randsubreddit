#!/usr/bin/env python3
"""
subriff_nsfw_scraper.py
-----------------------
Scrapes the NSFW subreddit list from subriff.com and writes it to a .txt file
in the same one-per-line format as:
  https://raw.githubusercontent.com/vburnin8tor/RANDNsfw-Subs/refs/heads/main/NSFWsubreddits.txt

Dependencies:
    pip install playwright
    playwright install chromium

Usage:
    python subriff_nsfw_scraper.py                    # writes NSFWsubreddits.txt
    python subriff_nsfw_scraper.py --out mylist.txt   # custom output path
    python subriff_nsfw_scraper.py --sort             # alphabetical sort
    python subriff_nsfw_scraper.py --order weekly     # sort by weekly growth (daily/weekly/monthly/yearly)
    python subriff_nsfw_scraper.py --min-subs 10000   # minimum subscriber threshold

Schedule with cron to keep the list auto-updating, e.g.:
    0 6 * * * /usr/bin/python3 /path/to/subriff_nsfw_scraper.py
"""

import argparse
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit(
        "playwright not installed.\n"
        "Run:  pip install playwright && playwright install chromium"
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUBRIFF_URL = "https://subriff.com/"
NSFW_BUTTON_TEXT = "NSFW"
PAGE_LOAD_WAIT = 2_500   # ms to wait after each interaction
MAX_PAGES = 50           # safety cap so we never loop forever


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape NSFW subreddits from subriff.com")
    p.add_argument("--out", default="NSFWsubreddits.txt", help="Output file path")
    p.add_argument("--sort", action="store_true", help="Sort list alphabetically")
    p.add_argument(
        "--order",
        choices=["daily", "weekly", "monthly", "yearly"],
        default="daily",
        help="Growth order to use on subriff (default: daily)",
    )
    p.add_argument(
        "--min-subs",
        type=int,
        default=0,
        help="Minimum subscriber count filter (0 = all, or 10000/50000/100000/500000)",
    )
    p.add_argument("--headless", action="store_true", default=True,
                   help="Run browser headlessly (default: True)")
    p.add_argument("--visible", action="store_false", dest="headless",
                   help="Show the browser window while scraping")
    return p.parse_args()


def click_filter(page, label: str) -> None:
    """Click a filter anchor by its visible text."""
    page.locator(f"a:has-text('{label}')").first.click()
    page.wait_for_timeout(PAGE_LOAD_WAIT)


def extract_subreddits(page) -> list[str]:
    """Pull subreddit names from the table rows currently visible on the page."""
    # Each row links to https://subriff.com/subreddit/<Name>
    links = page.locator("table a[href*='/subreddit/']").all()
    names = []
    for link in links:
        href = link.get_attribute("href") or ""
        match = re.search(r"/subreddit/([^/\?]+)", href)
        if match:
            names.append(match.group(1))
    return names


def scrape(args: argparse.Namespace) -> list[str]:
    all_names: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        print(f"Opening {SUBRIFF_URL} …")
        page.goto(SUBRIFF_URL, wait_until="networkidle")
        page.wait_for_timeout(PAGE_LOAD_WAIT)

        # --- Apply growth order filter ---
        order_label = args.order.capitalize()   # Daily / Weekly / Monthly / Yearly
        print(f"Setting order: {order_label}")
        click_filter(page, order_label)

        # --- Apply minimum subscriber filter ---
        min_map = {0: "All", 10_000: "10k", 50_000: "50k",
                   100_000: "100k", 500_000: "500k"}
        min_label = min_map.get(args.min_subs, "All")
        if min_label != "All":
            print(f"Setting min subscribers: {min_label}")
            click_filter(page, min_label)

        # --- Click NSFW filter ---
        print("Enabling NSFW filter …")
        click_filter(page, NSFW_BUTTON_TEXT)

        # --- Paginate through all results ---
        for page_num in range(1, MAX_PAGES + 1):
            names = extract_subreddits(page)
            print(f"  Page {page_num}: {len(names)} subreddits found")
            all_names.extend(names)

            # Look for a "Next" pagination link
            next_link = page.locator("a:has-text('Next')").first
            if not next_link.is_visible():
                print("  No more pages.")
                break
            next_link.click()
            page.wait_for_timeout(PAGE_LOAD_WAIT)
        else:
            print(f"Stopped at page cap ({MAX_PAGES}).")

        browser.close()

    return all_names


def write_output(names: list[str], path: str, sort: bool) -> None:
    # Deduplicate preserving order (or sort if requested)
    seen = set()
    unique = []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique.append(n)

    if sort:
        unique.sort(key=str.lower)

    out = Path(path)
    out.write_text("\n".join(unique) + "\n", encoding="utf-8")
    print(f"\nWrote {len(unique)} subreddits to {out.resolve()}")


def main() -> None:
    args = parse_args()
    start = time.time()
    names = scrape(args)
    write_output(names, args.out, args.sort)
    elapsed = time.time() - start
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
