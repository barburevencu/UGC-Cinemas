#!/usr/bin/env python3
"""
Scrape the films playing over a 7-day window (a start day + the 6 days after)
at a set of Paris repertory cinemas and write them all to a single CSV.

Source: AlloCiné's internal showtimes JSON endpoint (per cinema "salle" id).
No external dependencies — standard library only.

Usage:
    python3 scrape_ugc.py                 # today + next 6 days
    python3 scrape_ugc.py 2026-06-20      # that day + next 6 days
    python3 scrape_ugc.py 2026-06-20 -o out.csv

CSV columns: Movie title, Director, Year, cinema, date, time, Letterboxd, Poster

When a TMDB API key is available (set the TMDB_API_KEY environment variable,
or pass --tmdb-key), the "Movie title" is replaced with the film's English
title, the "Letterboxd" column is filled with its Letterboxd page, and the
"Poster" column with that page's poster image. Without a key, titles are left
as scraped and "Letterboxd"/"Poster" are blank.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# (AlloCiné salle id, display name) for each cinema to scrape.
# Grouped by neighbourhood, then alphabetical within each group (ignoring a
# leading "Le/La/Cinéma"). This order drives the cinema pills and the Cinemas
# view, so nearby houses — e.g. the two mk2 Odéons — sit together.
CINEMAS = [
    # Latin Quarter (5th)
    ("C0073", "Le Champo"),
    ("C0071", "Écoles Cinéma Club"),
    ("W7504", "Épée de Bois"),
    ("C0020", "Filmothèque du Quartier Latin"),
    ("C0072", "Le Grand Action"),
    ("C0076", "Cinéma du Panthéon"),
    ("C0074", "Reflet Médicis"),
    ("C0083", "Studio des Ursulines"),
    ("C0016", "Studio Galande"),
    # Odéon / Saint-Germain (6th)
    ("C0015", "Christine Cinéma Club"),
    ("C0093", "Le Lucernaire"),
    ("C0097", "mk2 Odéon St-Germain"),
    ("C0092", "mk2 Odéon St-Michel"),
    ("C0041", "Nouvel Odéon"),
    # Right Bank & outliers
    ("C0065", "Le Grand Rex"),            # 2nd
    ("C0089", "Max Linder Panorama"),     # 9th
    ("C2954", "mk2 Bibliothèque"),        # 13th
]

ENDPOINT = "https://www.allocine.fr/_/showtimes/theater-{salle}/d-{date}/"
# Page 2+ of a day's results use a trailing "p-{page}/" segment. Big cinemas
# (e.g. mk2 Bibliothèque) run more films than fit on one page; see fetch_all.
PAGED_ENDPOINT = "https://www.allocine.fr/_/showtimes/theater-{salle}/d-{date}/p-{page}/"

# Number of days to scrape, starting from the given (or today's) date.
DAYS = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}


def fetch(salle, date_str, page=1):
    """Fetch one page of the showtimes JSON for a cinema on a YYYY-MM-DD date."""
    if page > 1:
        url = PAGED_ENDPOINT.format(salle=salle, date=date_str, page=page)
    else:
        url = ENDPOINT.format(salle=salle, date=date_str)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP error for {salle}: {e.code} {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  ! Network error for {salle}: {e.reason}", file=sys.stderr)
    return None


def fetch_all(salle, date_str):
    """Fetch every page of showtimes for a cinema, merging results into one dict.

    AlloCiné paginates at ~15 films per page. Repertory houses fit on one page,
    but a multiplex can span several, so follow pagination.totalPages and append
    each page's results. Returns a dict shaped like a single-page response (with
    a combined "results" list), or None if even the first page failed.
    """
    first = fetch(salle, date_str, page=1)
    if first is None:
        return None
    results = list(first.get("results") or [])
    total_pages = ((first.get("pagination") or {}).get("totalPages")) or 1
    for page in range(2, total_pages + 1):
        data = fetch(salle, date_str, page=page)
        if data is None:
            continue
        results.extend(data.get("results") or [])
    first["results"] = results
    return first


def strip_disambiguation(name):
    """Drop AlloCiné/IMDb name-disambiguation suffixes, e.g. 'Kirk Jones (II)'."""
    return re.sub(r"\s*\((?:[IVXLCDM]+|\d+)\)\s*$", "", name).strip()


def get_director(movie):
    """Join all credited directors, e.g. 'Joel Coen, Ethan Coen'."""
    names = []
    for credit in movie.get("credits") or []:
        position = (credit.get("position") or {}).get("name")
        if position == "DIRECTOR":
            person = credit.get("person") or {}
            full = " ".join(
                p for p in (person.get("firstName"), person.get("lastName")) if p
            ).strip()
            full = strip_disambiguation(full)
            if full:
                names.append(full)
    return ", ".join(names)


def get_year(movie):
    """Production year, e.g. 2001."""
    year = (movie.get("data") or {}).get("productionYear")
    if year is None:
        year = movie.get("productionYear")
    return year or ""


def iter_showtimes(result):
    """Yield 'HH:MM' for every showtime of a movie, across all version groups."""
    showtimes = result.get("showtimes") or {}
    times = []
    for group in showtimes.values():
        if not isinstance(group, list):
            continue
        for st in group:
            starts_at = st.get("startsAt")
            if starts_at:
                # "2026-06-15T14:00:00" -> "14:00"
                times.append(starts_at[11:16])
    return sorted(set(times))


def collect_rows(data, cinema_name, date_str):
    rows = []
    for result in (data.get("results") or []):
        movie = result.get("movie") or {}
        original = movie.get("originalTitle") or movie.get("title") or ""
        french = movie.get("title") or ""
        director = get_director(movie)
        year = get_year(movie)
        for time in iter_showtimes(result):
            rows.append(
                {
                    "Movie title": original,   # replaced with English title later
                    "Original title": original,  # stable key for the CSV cache
                    "Director": director,
                    "Year": year,
                    "cinema": cinema_name,
                    "date": date_str,
                    "time": time,
                    # Internal-only: extra title used for TMDB matching.
                    # Not written to the CSV (write_csv lists the output fields).
                    "_french_title": french,
                }
            )
    return rows


TMDB_API = "https://api.themoviedb.org/3"
LETTERBOXD_TMDB = "https://letterboxd.com/tmdb/{id}/"
MAX_CANDIDATES = 6  # how many distinct results to consider when verifying


def tmdb_get(path, params, key):
    """GET a TMDB endpoint. Supports a v3 api key or a v4 bearer token."""
    headers = {"Accept": "application/json", "User-Agent": HEADERS["User-Agent"]}
    params = dict(params, language="en-US")
    if key.startswith("eyJ"):  # v4 bearer token (a JWT)
        headers["Authorization"] = f"Bearer {key}"
    else:  # v3 api key
        params["api_key"] = key
    url = TMDB_API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  ! TMDB HTTP error: {e.code} {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  ! TMDB network error: {e.reason}", file=sys.stderr)
    return None


def letterboxd_url(tmdb_id):
    """Resolve the canonical Letterboxd film URL from a TMDB id."""
    tmdb_link = LETTERBOXD_TMDB.format(id=tmdb_id)
    req = urllib.request.Request(tmdb_link, headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.url  # canonical .../film/<slug>/ after redirect
    except urllib.error.URLError:
        return tmdb_link  # the /tmdb/ form is itself a stable link


# The JSON-LD block on a film page carries the vertical (600x900) poster image.
# Its host path varies (/resized/film-poster/... or /resized/sm/upload/...), so
# match the first "image" field, which is the poster (og:image is a wide crop).
_LB_POSTER_RE = re.compile(
    r'"image":"(https://a\.ltrbxd\.com/resized/[^"]+)"')


def letterboxd_poster(film_url):
    """Fetch a Letterboxd film page and return its poster image URL ('' if none)."""
    if not film_url:
        return ""
    req = urllib.request.Request(film_url, headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError:
        return ""
    m = _LB_POSTER_RE.search(html)
    return m.group(1) if m else ""


def name_tokens(s):
    """Lowercase, accent-stripped word tokens (len > 2) of a name string."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2}


def directors_match(a, b):
    """True if two director strings share a meaningful name token."""
    return bool(name_tokens(a) & name_tokens(b))


def tmdb_director(movie_id, key):
    data = tmdb_get(f"/movie/{movie_id}/credits", {}, key)
    if not data:
        return ""
    names = [c.get("name", "") for c in data.get("crew", [])
             if c.get("job") == "Director"]
    return ", ".join(n for n in names if n)


def tmdb_candidates(titles, year, key):
    """Distinct TMDB search results across title+year then title-only passes."""
    queries = []
    if year:
        queries += [(t, year) for t in titles]
    queries += [(t, None) for t in titles]
    seen, out = set(), []
    for query, yr in queries:
        params = {"query": query}
        if yr:
            params["year"] = str(yr)
        for m in (tmdb_get("/search/movie", params, key) or {}).get("results") or []:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def tmdb_by_director(director, year, titles, key):
    """Find a film in the director's TMDB filmography by year (and title).

    Used when the title search buries the right film under same-title results
    (e.g. "An" (2015) by Naomi Kawase). Accepts a film released within a year
    of `year` whose title matches, or — if none do — the only film the
    director made that exact year. Returns a TMDB movie dict or None.
    """
    try:
        year = int(year)
    except (TypeError, ValueError):
        return None
    wanted = {t.strip().casefold() for t in titles if t}
    for name in [n.strip() for n in director.split(",") if n.strip()][:2]:
        people = (tmdb_get("/search/person", {"query": name}, key) or {}).get("results") or []
        for person in people[:2]:
            if not directors_match(person.get("name", ""), name):
                continue
            credits = tmdb_get(f"/person/{person['id']}/movie_credits", {}, key) or {}
            films = {m["id"]: m for m in credits.get("crew") or []
                     if m.get("job") == "Director"}.values()
            near = [m for m in films
                    if (m.get("release_date") or "")[:4].isdigit()
                    and abs(int(m["release_date"][:4]) - year) <= 1]
            for m in near:
                if {(m.get("title") or "").casefold(),
                    (m.get("original_title") or "").casefold()} & wanted:
                    return m
            exact = [m for m in near if m["release_date"][:4] == str(year)]
            if len(exact) == 1:
                return exact[0]
    return None


def tmdb_enrich(titles, year, key, director=""):
    """Find a movie on TMDB given candidate titles (original, then French).

    Tries each title with the year, then without it. When an AlloCiné director
    is known, prefer the first candidate whose TMDB director shares a name with
    it — this rejects same-title-wrong-film matches (e.g. "Paprika"). If none
    match, look the film up in the director's filmography instead; only then
    fall back to the top search result.
    Returns (english_title, letterboxd_url), or ('', '').
    """
    candidates = [t for t in dict.fromkeys(titles) if t]
    results = tmdb_candidates(candidates, year, key)
    chosen = None
    if director:
        for m in results[:MAX_CANDIDATES]:
            if directors_match(tmdb_director(m["id"], key), director):
                chosen = m
                break
        if chosen is None:
            chosen = tmdb_by_director(director, year, candidates, key)
    if chosen is None:
        if not results:
            return "", ""
        chosen = results[0]
    english = chosen.get("title") or chosen.get("original_title") or ""
    return english, letterboxd_url(chosen["id"])


def cache_key(original, year):
    return (original.strip().casefold(), str(year))


# Movies to leave unmatched (no TMDB lookup), keyed like cache_key: use for
# films TMDB only matches wrongly, e.g. shorts programmes matched to one short.
NO_MATCH = {
    ("les amis animaux", "2014"),
}


def load_cache(path, log=print):
    """Load previously-enriched movies from a CSV into a lookup.

    Keyed by (original title, year) -> (English title, Letterboxd URL, poster URL).
    Only rows that already have a Letterboxd link are cached, so movies that
    failed to match before are retried on the next run.
    """
    cache = {}
    if not os.path.exists(path):
        return cache
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                original = row.get("Original title") or ""
                letterboxd = row.get("Letterboxd") or ""
                if original and letterboxd:
                    cache[cache_key(original, row.get("Year", ""))] = (
                        row.get("Movie title", ""),
                        letterboxd,
                        row.get("Poster", "") or "",
                    )
    except Exception as exc:
        log(f"  (could not read cache {path}: {exc})")
        return {}
    if cache:
        log(f"Loaded {len(cache)} cached movies from {path}.")
    return cache


def enrich_rows(rows, key, cache=None):
    """Replace 'Movie title' with the English title and fill 'Letterboxd'.

    Movies already present in `cache` (e.g. loaded from a prior ugc.csv) are
    reused as-is and never re-looked-up on TMDB.
    """
    cache = cache if cache is not None else {}  # persistent: matches only
    seen = {}  # this run: every looked-up movie (matches and misses)
    hits = 0
    for row in rows:
        original = row["Original title"]
        french = row.get("_french_title", "")
        ck = cache_key(original, row["Year"])
        if ck in NO_MATCH:
            english, letterboxd, poster = "", "", ""
        elif ck in cache:
            english, letterboxd, poster = cache[ck]
            hits += 1
        elif ck in seen:
            english, letterboxd, poster = seen[ck]
        elif not original.strip():
            english, letterboxd, poster = "", "", ""
        else:
            english, letterboxd = tmdb_enrich(
                [original, french], row["Year"], key, row.get("Director", ""))
            poster = letterboxd_poster(letterboxd)
            print(f"  TMDB: {original} ({row['Year']}) -> "
                  f"{english or 'no match — kept original'}"
                  f"{' +poster' if poster else ''}")
            seen[ck] = (english, letterboxd, poster)
            if letterboxd:  # only persist successful matches to the cache/CSV
                cache[ck] = (english or original, letterboxd, poster)
        # Replace the title with the English one when we have a match.
        if english:
            row["Movie title"] = english
        row["Letterboxd"] = letterboxd
        row["Poster"] = poster
    if hits:
        print(f"  Reused {hits} showtimes from cache (no TMDB lookup).")


def write_csv(rows, path):
    fields = [
        "Movie title", "Original title", "Director", "Year", "cinema",
        "date", "time", "Letterboxd", "Poster",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        # extrasaction="ignore" drops internal-only keys like "_french_title".
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "date",
        nargs="?",
        default=dt.date.today().isoformat(),
        help="Start day of the 7-day window, YYYY-MM-DD (default: today)",
    )
    p.add_argument(
        "-o",
        "--output",
        default="ugc.csv",
        help="Output CSV path (default: ugc.csv)",
    )
    p.add_argument(
        "--tmdb-key",
        default=os.environ.get("TMDB_API_KEY"),
        help="TMDB API key/token (default: TMDB_API_KEY env var). "
        "Enables the English-title and Letterboxd columns.",
    )
    return p.parse_args()


def scrape(start, tmdb_key=None, days=DAYS, log=print, cache=None):
    """Scrape `days` days from `start` (a date) across all cinemas.

    Returns the list of showtime rows, enriched via TMDB when a key is given.
    `cache` is an optional (original title, year) -> (English title, Letterboxd)
    lookup (e.g. from load_cache) used to skip already-known movies.
    `log` is a callable for progress messages (use a no-op to silence).
    """
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(days)]

    all_rows = []
    for date_str in dates:
        log(f"{date_str}:")
        for salle, name in CINEMAS:
            data = fetch_all(salle, date_str)
            if data is None:
                continue
            rows = collect_rows(data, name, date_str)
            log(f"  {name}: {len(rows)} showtimes")
            all_rows.extend(rows)

    if tmdb_key:
        log("\nLooking up English titles + Letterboxd links on TMDB...")
        enrich_rows(all_rows, tmdb_key, cache)
    else:
        log("\nNo TMDB key — titles left as-is and Letterboxd left blank.")

    # Sort by date, then cinema, time, title (now the English title).
    all_rows.sort(
        key=lambda r: (r["date"], r["cinema"], r["time"], r["Movie title"])
    )
    return all_rows


def main():
    args = parse_args()
    try:
        start = dt.date.fromisoformat(args.date)
    except ValueError:
        sys.exit(f"Invalid date '{args.date}'. Use YYYY-MM-DD.")

    # Reuse the existing CSV as a cache so known movies skip TMDB lookups.
    cache = load_cache(args.output)
    all_rows = scrape(start, args.tmdb_key, cache=cache)
    write_csv(all_rows, args.output)

    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(DAYS)]

    print(
        f"\nWrote {len(all_rows)} showtimes for "
        f"{dates[0]}..{dates[-1]} to {args.output}"
    )


if __name__ == "__main__":
    main()
