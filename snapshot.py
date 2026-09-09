#!/usr/bin/env python3
"""
Hourly snapshot of the Guild Wars 2 5v5 ranked ladder (top 250, NA + EU).

Writes two files:
  data/history.json          rolling aggregate for the CURRENT season
  data/seasons/<id>.json     archived aggregate of a season that has ended

Design notes:
  - The API only ever exposes the current top 250. Cumulative stats
    ("who has ever been on the board") only exist because this runs on a
    schedule and accumulates. There is no way to backfill.
  - Players are keyed by account name, so someone dropping off and coming
    back updates their existing record instead of creating a second one.
  - On any API failure the previous history is left untouched. Never
    overwrite good data with an empty board.
"""

import json
import os
import sys
import time
import http.client
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.guildwars2.com/v2"
REGIONS = ("na", "eu")
DATA_DIR = "data"
HISTORY = os.path.join(DATA_DIR, "history.json")
ARCHIVE_DIR = os.path.join(DATA_DIR, "seasons")
PLAYER_DIR = os.path.join(DATA_DIR, "players")
MAX_SERIES = 4000            # ~5.5 months of hourly points
MAX_POINTS = 4000            # per-player cap
FORCE_POINT_AFTER_H = 6      # record a point even when nothing changed, this often
UA = "gw2-ladder-tracker (+github actions)"


def fetch(path, tries=4):
    """GET JSON with linear backoff. Returns (data, headers)."""
    url = API + path
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                # Lower-case the keys: dict() on an HTTPMessage loses its
                # case-insensitivity, and HTTP/2 always sends lower-case names,
                # which would silently turn X-Result-Total into 0.
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                return json.loads(r.read().decode("utf-8")), hdrs
        # OSError covers connection resets and incomplete reads mid-body, which
        # URLError does not - those must be retried, not fatal.
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                TimeoutError, ValueError, http.client.HTTPException) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("failed after %d tries: %s (%s)" % (tries, url, last))


def active_season():
    ids, _ = fetch("/pvp/seasons")
    tail = ids[-20:]
    objs, _ = fetch("/pvp/seasons?lang=en&ids=" + ",".join(tail))
    for s in objs:
        if s.get("active"):
            return s
    dated = sorted([s for s in objs if s.get("start")], key=lambda s: s["start"], reverse=True)
    if not dated:
        raise RuntimeError("no season with a start date found")
    return dated[0]


def scoring_ids(season):
    """Scoring UUIDs differ per season - always read them from the season object."""
    out = {}
    board = (season.get("leaderboards") or {}).get("ladder") or {}
    for s in board.get("scorings") or []:
        name = str(s.get("name", "")).strip().lower()
        if name:
            out[name] = s.get("id")
    return out


def board(season_id, region):
    """Fetch the whole board, bypassing any CDN edge cache.

    The API sets Cache-Control: max-age=3600, and edges hold their own copies of
    differing ages. A snapshot served from a stale edge would silently miss every
    player who was only on the board during that window - and "distinct players
    ever" can never recover a player it never saw. The cache-buster costs one
    origin request per region per hour, which is nothing against that risk.
    The upstream Date is logged so a stale read is visible rather than silent.
    """
    rows, total, served = [], 0, None
    bust = int(time.time())
    for page in range(2):                       # 250 entries = 2 pages of 200
        data, headers = fetch(
            "/pvp/seasons/%s/leaderboards/ladder/%s?page=%d&page_size=200&_cb=%d"
            % (season_id, region, page, bust))
        if page == 0:
            total = int(headers.get("x-result-total", 0) or 0)
            served = headers.get("date")
        if not isinstance(data, list) or not data:
            break
        rows.extend(data)
        if len(data) < 200:          # short page = last page, regardless of header
            break
    if total and len(rows) != total:
        # Better to skip an hour than to record a truncated board as if it were
        # the real one - a short board is indistinguishable from mass churn.
        raise RuntimeError("incomplete board for %s: got %d of %d"
                           % (region, len(rows), total))
    return rows, total, served


def shape(rows, smap):
    def score(entry, key):
        sid = smap.get(key)
        if not sid:
            return None
        for s in entry.get("scores") or []:
            if s.get("id") == sid:
                return s.get("value")
        return None

    out = []
    for e in rows:
        w, l = score(e, "wins"), score(e, "losses")
        games = None if (w is None and l is None) else (w or 0) + (l or 0)
        out.append({
            "name": e.get("name"),
            "rank": e.get("rank"),
            "date": e.get("date"),
            "rating": score(e, "rating"),
            "wins": w, "losses": l, "games": games,
        })
    out.sort(key=lambda r: r["rank"] if r["rank"] is not None else 10 ** 9)
    return out


def blank(season, region):
    return {
        "region": region,
        "seasonId": season["id"],
        "snapshots": 0,
        "firstAt": None,
        "lastAt": None,
        "players": {},      # account name -> record
        "series": [],       # board shape over time
        "current": [],      # most recent full board
    }


def merge(agg, rows, iso):
    """Fold one snapshot into the aggregate. Idempotent per account name."""
    for r in rows:
        name = r["name"]
        if not name:
            continue
        p = agg["players"].get(name)
        if p is None:
            agg["players"][name] = {
                "f": iso, "l": iso, "n": 1,
                "br": r["rank"], "bt": r["rating"],
                "fg": r["games"], "lg": r["games"],
            }
        else:
            p["l"] = iso
            p["n"] = p.get("n", 0) + 1
            if r["rank"] is not None and (p.get("br") is None or r["rank"] < p["br"]):
                p["br"] = r["rank"]
            if r["rating"] is not None and (p.get("bt") is None or r["rating"] > p["bt"]):
                p["bt"] = r["rating"]
            # lg = games at the most recent sighting. Comparing it to the
            # previous value is how "stopped playing" is told apart from
            # "lost rating" when someone drops off the board.
            p["lg"] = r["games"]

    ratings = sorted([r["rating"] for r in rows if r["rating"] is not None])
    med = ratings[len(ratings) // 2] if ratings else None

    # Roster churn against the previous snapshot. This can only be measured here,
    # where both rosters are in hand - the page would otherwise have to download
    # every player file to reconstruct it.
    prev_names = {r.get("name") for r in (agg.get("current") or []) if r.get("name")}
    now_names = {r["name"] for r in rows if r.get("name")}
    entered = len(now_names - prev_names) if prev_names else len(now_names)
    left = len(prev_names - now_names)
    held = len(now_names & prev_names)
    stab = round(held / len(prev_names), 4) if prev_names else None

    agg["series"].append({
        "t": iso,
        "c": len(rows),
        "r1": rows[0]["rating"] if rows else None,
        "r250": ratings[0] if ratings else None,
        "med": med,
        "new": entered,                       # first appearance since last snapshot
        "gone": left,                         # on the board last time, not now
        "stab": stab,                         # share of the previous board still there
        "ever": len(agg["players"]),          # cumulative distinct accounts this season
    })
    if len(agg["series"]) > MAX_SERIES:
        agg["series"] = agg["series"][-MAX_SERIES:]

    agg["snapshots"] = agg.get("snapshots", 0) + 1
    agg["firstAt"] = agg.get("firstAt") or iso
    agg["lastAt"] = iso
    agg["current"] = rows
    return agg


def slug(name):
    """Filename for a player. Matches JavaScript encodeURIComponent() exactly, so
    the page can build the same URL client-side without a lookup table."""
    return urllib.parse.quote(name, safe="-_.!~*'()")


def update_player_files(rows, iso, season_id):
    """Append this snapshot to each present player's own file.

    One file per player keeps a detail view to a single small request instead of
    trawling the whole season. A point is only appended when something actually
    changed, or every FORCE_POINT_AFTER_H hours, so files and git diffs stay small.
    """
    now = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    written = 0
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        path = os.path.join(PLAYER_DIR, slug(name) + ".json")
        doc = load_optional(path)
        if not doc or doc.get("seasonId") != season_id:
            doc = {"name": name, "seasonId": season_id, "points": []}
        pts = doc["points"]
        point = [iso, r.get("rank"), r.get("rating"), r.get("wins"), r.get("losses")]
        if pts:
            last = pts[-1]
            unchanged = last[1:] == point[1:]
            try:
                age_h = (now - datetime.strptime(last[0], "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
            except (ValueError, TypeError):
                age_h = 999
            if unchanged and age_h < FORCE_POINT_AFTER_H:
                continue
        pts.append(point)
        if len(pts) > MAX_POINTS:
            doc["points"] = pts[-MAX_POINTS:]
        save(path, doc)
        written += 1
    return written


def load(path):
    """None if the file does not exist. Raises if it exists but cannot be parsed.

    Collapsing those two cases is how accumulated history gets silently replaced
    by a blank aggregate: a single corrupt byte would otherwise look exactly like
    a first run. Callers that can tolerate a missing file use load_optional().
    """
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_optional(path):
    """For per-player files, where losing one file is not worth failing the run."""
    try:
        return load(path)
    except (IOError, OSError, ValueError) as e:
        print("   ...unreadable, starting fresh: %s (%s)" % (path, e))
        return None


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def main():
    season = active_season()
    if not season.get("active"):
        # Between seasons the API still serves the finished board. Recording it
        # hourly would inflate every "times seen" count and paint a perfectly
        # stable ladder that nobody is playing.
        print("no active season (latest: %s). Nothing to record." % season.get("name"))
        return 0
    smap = scoring_ids(season)
    iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print("season: %s (%s)  active=%s" % (season.get("name"), season["id"], season.get("active")))

    boards, failed = {}, []
    for region in REGIONS:
        try:
            rows, total, served = board(season["id"], region)
        except RuntimeError as e:
            # The two regions are the same global ladder, so losing one is not
            # worth losing the hour. Record what we got and carry on.
            print("  %s: FETCH FAILED (%s)" % (region, e))
            failed.append(region)
            continue
        boards[region] = shape(rows, smap)
        print("  %s: %d entries (X-Result-Total %s) upstream Date: %s"
              % (region, len(rows), total, served))
    if len(failed) == len(REGIONS):
        raise RuntimeError("every region failed; leaving history untouched")

    hist = load(HISTORY)

    # Season rollover: archive the finished season, start a clean one.
    if hist and hist.get("seasonId") and hist["seasonId"] != season["id"]:
        old_id = hist["seasonId"]
        old = os.path.join(ARCHIVE_DIR, "%s.json" % old_id)
        save(old, hist)
        # Move the per-player files too. update_player_files() resets any file
        # whose seasonId does not match, so leaving them in place would delete a
        # whole season of rating history the first time each player reappeared.
        if os.path.isdir(PLAYER_DIR):
            dest = os.path.join(ARCHIVE_DIR, old_id, "players")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.move(PLAYER_DIR, dest)
            print("archived %d player files -> %s" % (len(os.listdir(dest)), dest))
        print("archived previous season -> %s" % old)
        hist = None

    if not hist:
        hist = {
            "seasonId": season["id"],
            "seasonName": season.get("name"),
            "start": season.get("start"),
            "end": season.get("end"),
            "ranks": [
                {"name": r.get("name"),
                 "ceiling": (r.get("tiers") or [{}])[-1].get("rating")}
                for r in season.get("ranks") or []
            ],
            "regions": {r: blank(season, r) for r in REGIONS},
        }

    changed = False
    for region in REGIONS:
        if region not in boards:
            continue                      # fetch failed; leave this region alone
        rows = boards[region]
        if not rows:
            # Empty board is normal at season start, but it is also what a
            # failed fetch looks like. Only record it while the aggregate is
            # still empty; never let it clear an accumulated history.
            if hist["regions"][region].get("snapshots", 0) == 0:
                hist["regions"][region] = merge(hist["regions"][region], [], iso)
                changed = True
            else:
                print("  %s: empty board, existing history kept" % region)
            continue
        hist["regions"][region] = merge(hist["regions"][region], rows, iso)
        changed = True
        # Per-player detail. Only NA is written: the API serves one global board
        # for both regions, so writing EU as well would duplicate every file.
        if region == "na":
            n = update_player_files(rows, iso, season["id"])
            print("  wrote %d player files" % n)

    hist["updated"] = iso
    hist["seasonName"] = season.get("name")
    hist["end"] = season.get("end")

    if not changed:
        print("nothing to write")
        return 0

    save(HISTORY, hist)
    for region in REGIONS:
        a = hist["regions"][region]
        print("  %s: %d snapshots, %d players ever seen"
              % (region, a.get("snapshots", 0), len(a.get("players", {}))))
    print("wrote %s" % HISTORY)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                       # noqa: BLE001 - fail loudly in CI
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
