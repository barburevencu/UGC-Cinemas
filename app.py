#!/usr/bin/env python3
"""
Local browser app for the Paris repertory-cinema showtimes.

On launch it (re)runs the scraper from scrape_ugc.py — using the TMDB key from
the TMDB_API_KEY environment variable — to fetch the latest week of showtimes,
then opens a browser with views by day, by week, and by cinema.

Usage:
    export TMDB_API_KEY="your_key"      # enables English titles + Letterboxd
    python3 app.py                      # scrapes, then opens the browser

Environment:
    TMDB_API_KEY   TMDB v3 key or v4 token (optional but recommended)
    PORT           port to serve on (default 1895)

No external dependencies — standard library only.
"""

import csv
import datetime as dt
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scrape_ugc

PORT = int(os.environ.get("PORT", "1895"))
TMDB_KEY = os.environ.get("TMDB_API_KEY")
# CSV used both as the output and as a cache of already-enriched movies.
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "ugc.csv")
INDEX_PATH = os.path.join(HERE, "index.html")
DATA_PATH = os.path.join(HERE, "data.json")

# Per-cinema display metadata (address + a pastel, Wes-Anderson-ish colour).
CINEMA_META = {
    "Christine Cinéma Club":         ("4 rue Christine, 75006",        "#e8a09a"),
    "Écoles Cinéma Club":            ("23 rue des Écoles, 75005",      "#e6c26b"),
    "Le Champo":                     ("51 rue des Écoles, 75005",      "#8fb3ae"),
    "Filmothèque du Quartier Latin": ("9 rue Champollion, 75005",      "#a8b47c"),
    "Studio Galande":                ("42 rue Galande, 75005",         "#c9a0c4"),
    "Le Grand Action":               ("5 rue des Écoles, 75005",       "#e8a87c"),
    "MK2 Bibliothèque":              ("128-162 av. de France, 75013",  "#7fa8c9"),
    "Épée de Bois":                  ("100 rue Mouffetard, 75005",     "#d98c7a"),
    "Le Lucernaire":                 ("53 rue N.-D.-des-Champs, 75006", "#9db07c"),
    "Cinéma du Panthéon":            ("13 rue Victor Cousin, 75005",   "#b0a4d4"),
    "Le Grand Rex":                  ("1 bd Poissonnière, 75002",      "#d98cae"),
    "Max Linder Panorama":           ("24 bd Poissonnière, 75009",     "#6fa8a0"),
    "Nouvel Odéon":                  ("6 rue de l'École-de-Médecine, 75006", "#c7b06a"),
    "Studio des Ursulines":          ("10 rue des Ursulines, 75005",   "#9c8cc4"),
    "Reflet Médicis":                ("3 rue Champollion, 75005",      "#cf9b6f"),
}

# In-memory cache of the most recent scrape, guarded by a lock.
_lock = threading.Lock()
_state = {
    "status": "idle",          # "idle" | "scraping"
    "generated_at": None,      # ISO timestamp of the last successful scrape
    "rows": [],
    "error": None,
}


def run_scrape():
    """Scrape the next 7 days and store the result in the cache."""
    with _lock:
        if _state["status"] == "scraping":
            return
        _state["status"] = "scraping"
        _state["error"] = None
    try:
        # Use the existing CSV as a cache so known movies skip TMDB lookups,
        # then write the refreshed week back to it.
        cache = scrape_ugc.load_cache(CSV_PATH)
        rows = scrape_ugc.scrape(dt.date.today(), TMDB_KEY, cache=cache)
        scrape_ugc.write_csv(rows, CSV_PATH)
        with _lock:
            _state["rows"] = rows
            _state["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        write_data_json()
    except Exception as exc:  # keep the server alive on scrape failure
        with _lock:
            _state["error"] = str(exc)
        print(f"Scrape failed: {exc}")
    finally:
        with _lock:
            _state["status"] = "idle"


def cinema_list():
    out = []
    for _, name in scrape_ugc.CINEMAS:
        address, color = CINEMA_META.get(name, ("", "#888888"))
        out.append({"name": name, "address": address, "color": color})
    return out


def snapshot():
    with _lock:
        # Drop internal-only keys (e.g. _french_title) before exposing rows.
        rows = [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in _state["rows"]
        ]
        return {
            "status": _state["status"],
            "generated_at": _state["generated_at"],
            "error": _state["error"],
            "tmdb": bool(TMDB_KEY),
            "cinemas": cinema_list(),
            "dates": sorted({r["date"] for r in rows}),
            "rows": rows,
        }


def write_data_json():
    """Write the current snapshot to data.json so any static server can host it."""
    try:
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot(), f, ensure_ascii=False)
    except Exception as exc:
        print(f"Could not write {DATA_PATH}: {exc}")


def csv_is_fresh():
    """True if ugc.csv exists and was last written today."""
    if not os.path.exists(CSV_PATH):
        return False
    mtime = dt.date.fromtimestamp(os.path.getmtime(CSV_PATH))
    return mtime == dt.date.today()


def load_state_from_csv():
    """Load an already-fresh ugc.csv into memory without scraping."""
    rows = []
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:
        print(f"Could not read {CSV_PATH}: {exc}")
        return False
    with _lock:
        _state["rows"] = rows
        _state["generated_at"] = dt.datetime.fromtimestamp(
            os.path.getmtime(CSV_PATH)
        ).isoformat(timespec="seconds")
        _state["status"] = "idle"
    write_data_json()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet console
        pass

    def _send(self, code, body, content_type):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            maybe_daily_refresh()
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "index.html not found", "text/plain; charset=utf-8")
        elif self.path.startswith("/data.json"):
            maybe_daily_refresh()
            self._send(200, json.dumps(snapshot()), "application/json; charset=utf-8")
        elif self.path.startswith("/refresh"):
            threading.Thread(target=run_scrape, daemon=True).start()
            self._send(200, json.dumps({"status": "scraping"}),
                       "application/json; charset=utf-8")
        elif self.path.startswith("/favicon.svg"):
            try:
                with open(os.path.join(HERE, "favicon.svg"), "rb") as f:
                    self._send(200, f.read(), "image/svg+xml")
            except FileNotFoundError:
                self._send(404, "Not found", "text/plain; charset=utf-8")
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")



def maybe_daily_refresh():
    """Trigger a scrape if the data isn't from today and we're idle.

    Lets an always-on server refresh itself the first time it's visited each
    day, without a manual Refresh.
    """
    with _lock:
        busy = _state["status"] == "scraping"
    if not busy and not csv_is_fresh():
        threading.Thread(target=run_scrape, daemon=True).start()


def main():
    # Load whatever ugc.csv exists so we can serve it immediately — even if it's
    # stale, that's better than an empty spinner during a long cold scrape.
    loaded = os.path.exists(CSV_PATH) and load_state_from_csv()

    if csv_is_fresh() and loaded:
        with _lock:
            n = len(_state["rows"])
        print(f"ugc.csv already updated today ({n} rows) — skipping scrape.")
    else:
        # Stale or missing: refresh in the background while serving what we have.
        if loaded:
            with _lock:
                n = len(_state["rows"])
            print(f"Serving cached ugc.csv ({n} rows) while refreshing in the background…")
        else:
            print("No cached data yet — scraping…")
        threading.Thread(target=run_scrape, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Serving the cinema app at {url}")
    print(f"TMDB enrichment: {'ON' if TMDB_KEY else 'OFF (set TMDB_API_KEY)'}")
    print("Press Ctrl+C to stop.")
    # Skip auto-opening a browser when run as a background service (NO_BROWSER=1).
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
