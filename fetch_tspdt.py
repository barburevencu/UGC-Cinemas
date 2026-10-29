#!/usr/bin/env python3
"""Snapshot the TSPDT "1,000 Greatest Films" Letterboxd list into tspdt.json.

The list changes once a year, so this is run by hand (not by the nightly
scrape). The site matches showtimes to it by Letterboxd film slug; a film's
rank is its position in "slugs" plus one.

Usage: python3 fetch_tspdt.py [list URL]
"""

import json
import re
import sys
import urllib.request

LIST_URL = "https://letterboxd.com/tspdtfanaccount/list/the-1000-greatest-films-21st-edition-2026/"
EDITION = "21st edition (2026)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else LIST_URL).rstrip("/") + "/"
    slugs, page = [], 1
    while True:
        html = fetch(base if page == 1 else f"{base}page/{page}/")
        found = re.findall(r'data-item-slug="([^"]+)"', html)
        if not found:
            break
        slugs.extend(s for s in found if s not in slugs)
        page += 1
    if len(slugs) < 1000:
        sys.exit(f"Only found {len(slugs)} films; not overwriting tspdt.json")
    with open("tspdt.json", "w", encoding="utf-8") as f:
        json.dump({"edition": EDITION, "source": base, "slugs": slugs}, f, indent=0)
    print(len(slugs), "films written to tspdt.json")


if __name__ == "__main__":
    main()
