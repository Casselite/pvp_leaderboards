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

# The na/eu path segment on the ladder endpoint is IGNORED by the API. It serves
# whichever regional board matches the caller's own IP. Verified 2026-09-09 by
# requesting the same URL from two places within seconds of each other:
#
#   from Europe, .../ladder/na  -> 166 entries, top thelordofdarkness.1982
#   from the US, .../ladder/eu  -> 102 entries, top ashkew.3204
#
# The two rosters had zero names in common. So a collector can only ever record
# the board of the region it physically runs in, and asking for both paths just
# fetched the same board twice. LADDER_REGION therefore describes where this
# runner sits - it is a label for the data, not a request parameter. A GitHub-
# hosted runner is in the US, hence the default.
REGION = (os.environ.get("LADDER_REGION") or "na").strip().lower()
if REGION not in ("na", "eu"):
    raise SystemExit("LADDER_REGION must be 'na' or 'eu', got %r" % REGION)
# Any path is fine since it is ignored; send the one we believe we are getting so
# the request is at least self-describing in ArenaNet's logs.
REGION_PATH = REGION

DATA_DIR = "data"
HISTORY = os.path.join(DATA_DIR, "history.json")
ARCHIVE_DIR = os.path.join(DATA_DIR, "seasons")
PLAYER_ROOT = os.path.join(DATA_DIR, "players")
PLAYER_DIR = os.path.join(PLAYER_ROOT, REGION)
BOARD = os.path.join(DATA_DIR, "board_%s.json" % REGION)
MAX_SERIES = 4000            # ~5.5 months of hourly points
MAX_POINTS = 4000            # per-player cap
FORCE_POINT_AFTER_H = 6      # record a point even when nothing changed, this often
MIN_SNAPSHOT_GAP_S = 900     # ignore a second history point inside this window
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


def tier_ladder(season):
    """Flatten ranks -> sub-tiers, ascending by rating ceiling."""
    out = []
    for r in season.get("ranks") or []:
        for i, t in enumerate(r.get("tiers") or []):
            if t and t.get("rating") is not None:
                out.append({"name": "%s %d" % (r.get("name"), i + 1),
                            "ceil": t["rating"]})
    out.sort(key=lambda x: x["ceil"])
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


def migrate_layout(hist):
    """One-off move from the old single-region layout to the per-region one.

    The old collector believed na and eu were the same global ladder, so it wrote
    every player file flat in data/players/ and recorded two identical series in
    history.json. Both assumptions were wrong: the files are the board of
    whichever region the runner sits in, and the second series is the first one
    under a false label.

    Idempotent - after the first run there is nothing left to move, and it is a
    no-op on a fresh checkout. Nothing is deleted without a copy being archived
    first, because the mislabelled series is still real observations.
    """
    moved_files = 0
    # Flat player files -> data/players/<REGION>/. Only *.json directly inside
    # data/players counts; the region directories themselves are skipped.
    if os.path.isdir(PLAYER_ROOT):
        stray = [f for f in os.listdir(PLAYER_ROOT)
                 if f.endswith(".json")
                 and os.path.isfile(os.path.join(PLAYER_ROOT, f))]
        if stray:
            os.makedirs(PLAYER_DIR, exist_ok=True)
            for f in stray:
                src = os.path.join(PLAYER_ROOT, f)
                dst = os.path.join(PLAYER_DIR, f)
                if os.path.exists(dst):
                    # Already migrated by an earlier interrupted run; the
                    # destination is the newer file, so drop the stray copy.
                    os.remove(src)
                else:
                    shutil.move(src, dst)
                moved_files += 1
            print("migrated %d player files -> %s" % (moved_files, PLAYER_DIR))

    # The duplicate region series. Keep only the region this runner actually
    # observes; archive the rest before dropping it.
    dropped = []
    if hist and isinstance(hist.get("regions"), dict):
        extra = [r for r in hist["regions"] if r != REGION]
        if extra:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            copy = os.path.join(ARCHIVE_DIR, "premigration-%s.json" % stamp)
            save(copy, hist)
            print("archived pre-migration history -> %s" % copy)
            for r in extra:
                hist["regions"].pop(r, None)
                dropped.append(r)
            print("dropped mislabelled region series: %s (kept %s)"
                  % (", ".join(dropped), REGION))
    return moved_files, dropped


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

    # One fetch. The region path is ignored upstream, so asking for both spellings
    # only ever retrieved the same board twice - pure waste against an API we want
    # to touch as little as possible.
    rows, total, served = board(season["id"], REGION_PATH)
    rows = shape(rows, smap)
    print("  %s: %d entries (X-Result-Total %s) upstream Date: %s"
          % (REGION, len(rows), total, served))

    hist = load(HISTORY)
    migrate_layout(hist)

    # Season rollover: archive the finished season, start a clean one.
    if hist and hist.get("seasonId") and hist["seasonId"] != season["id"]:
        old_id = hist["seasonId"]
        old = os.path.join(ARCHIVE_DIR, "%s.json" % old_id)
        save(old, hist)
        # Move the per-player files too. update_player_files() resets any file
        # whose seasonId does not match, so leaving them in place would delete a
        # whole season of rating history the first time each player reappeared.
        if os.path.isdir(PLAYER_DIR):
            dest = os.path.join(ARCHIVE_DIR, old_id, "players", REGION)
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
            # Sub-tier ladder ("Gold 2", not just "Gold"). Stored here so the page
            # needs no API call of its own just to label a rating - every request
            # a visitor makes to the official API is one we would rather not make.
            "tiers": tier_ladder(season),
            "regions": {},
        }

    # A second collector, running in the other region, writes into the same file.
    # Only ever touch our own region's slot so the two never clobber each other -
    # git merges the rest.
    hist.setdefault("regions", {})
    hist["regions"].setdefault(REGION, blank(season, REGION))

    # Do not record two snapshots minutes apart. The workflow runs a ~5.5 hour
    # loop and then dispatches its successor, so at every handover the old job's
    # final pass and the new job's first pass both fire - observed 13 seconds
    # apart. Each duplicate inflates the snapshot count and adds a phantom
    # stability=1.0 reading, which distorts "present in at least half the
    # snapshots". The board file is still refreshed below; only the history
    # series is protected.
    last_t = None
    prev_series = hist["regions"][REGION].get("series") or []
    if prev_series:
        last_t = prev_series[-1].get("t")
    too_soon = False
    if last_t:
        try:
            gap = (datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                   - datetime.strptime(last_t, "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
            too_soon = 0 <= gap < MIN_SNAPSHOT_GAP_S
        except (ValueError, TypeError):
            too_soon = False

    changed = False
    if too_soon and rows:
        print("  last snapshot was %.0fs ago (<%ds) - refreshing the board only,"
              " not recording another history point" % (gap, MIN_SNAPSHOT_GAP_S))
        save(BOARD, {
            "region": REGION,
            "seasonId": season["id"],
            "seasonName": season.get("name"),
            "collectedAt": iso,
            "count": len(rows),
            "rows": rows,
        })
        print("  wrote %s (%d rows)" % (BOARD, len(rows)))
        return 0
    if not rows:
        # Empty board is normal at season start, but it is also what a failed
        # fetch looks like. Only record it while the aggregate is still empty;
        # never let it clear an accumulated history.
        if hist["regions"][REGION].get("snapshots", 0) == 0:
            hist["regions"][REGION] = merge(hist["regions"][REGION], [], iso)
            changed = True
        else:
            print("  %s: empty board, existing history kept" % REGION)
    else:
        hist["regions"][REGION] = merge(hist["regions"][REGION], rows, iso)
        changed = True
        n = update_player_files(rows, iso, season["id"])
        print("  wrote %d player files" % n)
        # The board the page renders. Serving stored rows instead of letting each
        # visitor fetch the ladder themselves is the only way the board and the
        # history can agree: a visitor in Europe fetching live gets Europe's
        # roster, for which this collector holds no history at all.
        save(BOARD, {
            "region": REGION,
            "seasonId": season["id"],
            "seasonName": season.get("name"),
            "collectedAt": iso,
            "count": len(rows),
            "rows": rows,
        })
        print("  wrote %s (%d rows)" % (BOARD, len(rows)))

    hist["updated"] = iso
    hist["seasonName"] = season.get("name")
    hist["end"] = season.get("end")
    # Refreshed every pass, not just at creation, so an existing history file
    # picks these up without being rebuilt from scratch.
    hist["start"] = season.get("start")
    hist["tiers"] = tier_ladder(season)

    if not changed:
        print("nothing to write")
        return 0

    save(HISTORY, hist)
    for region, a in sorted(hist["regions"].items()):
        print("  %s: %d snapshots, %d players ever seen%s"
              % (region, a.get("snapshots", 0), len(a.get("players", {})),
                 "" if region == REGION else "  (other collector)"))
    print("wrote %s" % HISTORY)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                       # noqa: BLE001 - fail loudly in CI
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
