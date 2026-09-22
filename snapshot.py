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
from datetime import datetime, timedelta, timezone

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
WEEKLY_MATCH_STEP = 15       # the match minimum rises by this every week
BUMP_WINDOW_H = 48           # how long a requirement rise is measured for

# Around a requirement rise the hourly grid is too coarse. The rise is scheduled
# on the hour, but the API recomputes on its own clock, so an on-the-hour pass
# can land a second after the boundary and see nothing. These are extra passes,
# minutes after a scheduled rise, that keep looking until the minimum actually
# moves. They do not replace the hourly pass - that one is what captures the
# pre-rise roster, which cannot be recovered afterwards.
#
# A probe only writes a history point if the observed minimum has CHANGED, so
# probes that see nothing cost an API call and nothing else. The offsets are
# front-loaded because the recompute is more likely to be prompt than late; once
# a few rises have been measured the real latency will be visible in the bump
# records and this list can shrink to one well-placed pass.
BUMP_PROBE_MIN = [6, 13, 25, 40, 55]
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


def active_season(known_start=None):
    """The season currently running, found by asking EVERY season, not the tail.

    /v2/pvp/seasons returns ids in NO GUARANTEED ORDER. This function used to
    fetch only the last 20 and look for active:true among them, which worked
    until ArenaNet reshuffled the list - on 2026-09-15 the running season moved
    to index 37 of 78, forty places from the end, and dropped out of that window.
    The old code then fell through to "newest by start date among those twenty"
    and picked PvP League 3v3 Season Twelve, which ended in March 2025. Its
    ladder is empty, an empty board is a legitimate state the collector refuses
    to record over good history, so every pass wrote nothing and every run
    reported success. Twelve hours of the season's most interesting event were
    lost to a silent fallback.

    So: no slicing, and no quiet fallback. If nothing is active, or the season
    found is OLDER than the one already being recorded, this raises. A failed
    pass is visible in the workflow log; a wrong season is not.
    """
    ids, _ = fetch("/pvp/seasons")
    if not ids:
        raise RuntimeError("/pvp/seasons returned no ids")
    objs = []
    for i in range(0, len(ids), 20):            # the endpoint caps ids per call
        chunk, _ = fetch("/pvp/seasons?lang=en&ids=" + ",".join(ids[i:i + 20]))
        if isinstance(chunk, list):
            objs.extend(chunk)
    active = [s for s in objs if s and s.get("active")]
    if not active:
        raise RuntimeError(
            "no active season among %d seasons - refusing to guess. The last "
            "time this was guessed it silently collected a season that ended in "
            "2025." % len(objs))
    if len(active) > 1:
        # More than one can be flagged active around a rollover; take the one
        # that started most recently rather than whichever came back first.
        active.sort(key=lambda s: s.get("start") or "", reverse=True)
        print("  %d seasons flagged active; taking the newest (%s)"
              % (len(active), active[0].get("name")))
    season = active[0]
    # Never walk backwards. A new season starting is normal; the "current"
    # season suddenly being an older one is always a bug or an API glitch.
    if known_start and season.get("start") and season["start"][:19] < known_start[:19]:
        raise RuntimeError(
            "active season %r starts %s, BEFORE the season already being "
            "recorded (%s) - refusing to overwrite newer history with older."
            % (season.get("name"), season.get("start"), known_start))
    return season


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
        "bumps": {},        # requirement rise -> what the board did around it
    }


def bump_times(season):
    """When the match minimum rises, as ISO strings.

    The minimum goes up by WEEKLY_MATCH_STEP at the end of every seven days of
    the season. Computed the same way the page computes it, so the two never
    disagree about which rise a measurement belongs to.
    """
    start, end = season.get("start"), season.get("end")
    if not start:
        return []
    try:
        t0 = datetime.strptime(start[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return []
    try:
        t1 = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        t1 = t0 + timedelta(days=200)
    out, w = [], 1
    while w <= 40:
        t = t0 + timedelta(days=7 * w)
        if t > t1:
            break
        out.append((t, WEEKLY_MATCH_STEP * (w + 1)))
        w += 1
    return out


def probe_window(season, now):
    """The scheduled rise this moment is probing, as (boundary, expected_min).

    Returns None outside the probing period, when the collector runs on its
    ordinary hourly grid.
    """
    if not BUMP_PROBE_MIN:
        return None
    span = timedelta(minutes=max(BUMP_PROBE_MIN) + 5)
    for t, required in bump_times(season):
        if t <= now < t + span:
            return t, required
    return None


def still_probing(season, now, min_games_now):
    """Are we inside a rise window that has not yet been observed to happen?

    The stopping condition is the OBSERVED minimum reaching what the calendar
    says it should be - not a difference between the last two history points,
    which stops being true again as soon as a third point lands and would leave
    the collector probing for the rest of the window.

    An observed rise that falls short of the calendar (say 15 -> 25 when 30 was
    expected) keeps probing until the window closes. That costs a few API calls
    and is the behaviour we want: the schedule is a guess, the board is not.
    """
    w = probe_window(season, now)
    if w is None:
        return None
    t, required = w
    if min_games_now is not None and required is not None and min_games_now >= required:
        return None
    return t


def next_wake_seconds(season, now, min_games_now):
    """How long to sleep before the next pass.

    Normally: to the top of the next hour, so snapshots land on a tidy grid and
    line up with the API's own hourly recompute. Approaching a scheduled rise:
    to the boundary itself, so the pass that captures the irrecoverable pre-rise
    roster happens as late as possible. Just after one, until it is observed:
    to the next probe offset.
    """
    def to_next_hour():
        nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return max(60, int((nxt - now).total_seconds()))

    t = still_probing(season, now, min_games_now)
    if t is not None:
        for mins in sorted(BUMP_PROBE_MIN):
            at = t + timedelta(minutes=mins)
            if at > now:
                return max(60, int((at - now).total_seconds()))
        return to_next_hour()

    # Not probing. If a rise is scheduled inside the next hour, land on it.
    for bt, _ in bump_times(season):
        if now < bt <= now + timedelta(hours=1):
            return max(60, int((bt - now).total_seconds()))
    return to_next_hour()


def emit_wake(season, hist):
    """Tell the workflow how long to sleep, on every exit path.

    Printed as a single parseable line rather than returned, because the loop
    that does the sleeping is the shell script, not this process. If the line is
    missing or unparseable the workflow falls back to the top of the next hour,
    so this is a hint and never a dependency.
    """
    try:
        ser = (hist.get("regions", {}).get(REGION, {}) or {}).get("series") or []
        mg_now = ser[-1].get("mg") if ser else None
        now = datetime.now(timezone.utc)
        s = next_wake_seconds(season, now, mg_now)
        why = ("probing for a requirement rise (minimum still %s)" % mg_now
               if still_probing(season, now, mg_now) else "hourly grid")
        print("NEXT_WAKE_S=%d  (%s)" % (s, why))
    except Exception as e:                       # noqa: BLE001
        print("  could not compute next wake (%s); workflow will use the hour" % e)


def record_bump(agg, season, rows, iso, pre_roster, cut_before, prev_count,
                min_games_now=None, min_games_prev=None):
    """Measure what a requirement rise does to the board.

    A rise is the only moment the ladder shows players it normally hides: nobody's
    rating changes, the eligibility rule does, and accounts that were always below
    the cut become briefly visible. Refill time alone cannot tell that apart from
    board regulars grinding their way back, so the board is split three ways:

      held    - was on the board immediately before the rise (whether it kept its
                slot throughout or fell off and played its way back - either way
                the ladder was already showing this account)
      back    - recorded earlier this season but NOT on the board just before the
                rise: someone the cut had pushed out, now visible again because
                the cut dropped
      fresh   - never recorded at any point this season

    Only `fresh` is evidence about the population below the cut. `held` and `back`
    are the same people taking a lap. The pre-rise roster cannot be rebuilt after
    the fact (the history keeps only the latest board), so it is captured here on
    the first pass at or after the rise and kept with the record.
    """
    now = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    active = None
    for t, req in bump_times(season):
        if t <= now < t + timedelta(hours=BUMP_WINDOW_H):
            active = (t, req)

    # The schedule above is arithmetic from the season start - seven days, fifteen
    # matches - and nothing verifies it. If ArenaNet's real cadence differs by so
    # much as a day, the window opens at the wrong time and the pre-rise roster,
    # the one thing that cannot be recovered afterwards, is captured too late to
    # be the pre-rise roster at all.
    #
    # So also watch the board itself. Nobody holds a slot below the minimum, so
    # the smallest game count on the board IS the minimum. When that number jumps,
    # the requirement rose, whatever the calendar says. An observed jump opens a
    # window of its own, keyed to this hour.
    observed = None
    if min_games_now is not None and min_games_prev is not None \
            and min_games_now - min_games_prev >= WEEKLY_MATCH_STEP:
        observed = (now, min_games_now)
        print("  observed the match minimum jump from %d to %d"
              % (min_games_prev, min_games_now))

    if observed and (not active or abs((observed[0] - active[0]).total_seconds()) > 3 * 3600):
        # More than three hours from where the schedule expected it: trust the
        # board, not the arithmetic, and note that they disagreed.
        if active:
            print("  NOTE: schedule expected the rise at %s, the board says %s"
                  % (active[0].isoformat(), observed[0].isoformat()))
        active = observed
    if not active:
        return
    t, req = active
    key = t.strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = agg.setdefault("bumps", {}).setdefault(key, {})
    if "req" not in rec:
        # First pass after this rise. agg["current"] has not been merged yet, so
        # it still holds the board as it stood before the rule changed.
        rec.update({"req": req, "at": key, "preRoster": list(pre_roster),
                    "preCount": prev_count, "preCut": cut_before, "series": []})
        print("  requirement rose to %d; captured pre-rise roster of %d"
              % (req, len(pre_roster)))

    pre = set(rec.get("preRoster") or [])
    # "Seen before the rise" is read from each account's own first-seen stamp, so
    # an account first recorded in this very pass is never counted as a returner.
    seen_before = {n for n, p in agg.get("players", {}).items()
                   if p.get("f") and p["f"] < key}
    names = [r.get("name") for r in rows if r.get("name")]
    held = sum(1 for n in names if n in pre)
    back = sum(1 for n in names if n not in pre and n in seen_before)
    fresh = len(names) - held - back
    ratings = sorted([r["rating"] for r in rows if r.get("rating") is not None])
    rec["series"].append({
        "t": iso, "c": len(names), "cut": ratings[0] if ratings else None,
        "held": held, "back": back, "fresh": fresh,
        "mg": min_games_now,
        "h": round((now - t).total_seconds() / 3600.0, 2),
    })
    if len(rec["series"]) > 3 * BUMP_WINDOW_H:
        rec["series"] = rec["series"][-3 * BUMP_WINDOW_H:]
    print("  rise+%.0fh: board %d = %d already shown / %d re-surfaced / %d never seen before"
          % (rec["series"][-1]["h"], len(names), held, back, fresh))


FOREIGN_WINDOW_H = 12        # how far back "who was recently here" reaches
FOREIGN_MIN_OVERLAP = 0.20   # below this share of familiar names, refuse the board


def foreign_board(agg, rows, iso):
    """Is this somebody else's regional board? Returns the overlap, or None.

    The ladder API serves whichever region the CALLER sits in and never says
    which one it gave you. GitHub reassigns runner IPs constantly, so a runner
    that normally geolocates to the US can be placed in Europe for a single
    request. That happened at 2026-09-22T06:00:06Z: one pass recorded the entire
    EU board as NA, adding 251 accounts that were never seen again and inflating
    every cumulative statistic in the file.

    The signature is unmistakable once you look for it - that pass replaced 249
    of 250 players while recording ZERO games played, which cannot happen. Real
    churn requires somebody to finish a match.

    Comparing against the previous roster alone is not enough: if a foreign board
    were served twice in a row, the second pass would look perfectly stable
    against the first. So this compares against everyone seen in the last
    FOREIGN_WINDOW_H hours, which a foreign board misses almost entirely.

    For scale on the threshold: the largest legitimate upheaval this collector
    has recorded is the 15 Sept requirement rise, which replaced 47% of the board
    in one pass and still kept 53% overlap. Twenty percent is far below anything
    the ladder does on its own.
    """
    names = set(r.get("name") for r in rows if r.get("name"))
    if not names:
        return None
    prev = agg.get("current") or []
    if len(prev) < 50:
        return None                      # early season: nothing to compare against
    try:
        now = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    cutoff = now - timedelta(hours=FOREIGN_WINDOW_H)
    recent = set()
    for name, p in (agg.get("players") or {}).items():
        last = p.get("l")
        if not last:
            continue
        try:
            t = datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if t >= cutoff:
            recent.add(name)
    if len(recent) < 50:
        return None                      # not enough recent history to judge
    overlap = len(names & recent) / float(len(names))
    return overlap if overlap < FOREIGN_MIN_OVERLAP else None


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
                # lr/lk are the LAST rating and rank, as distinct from the best
                # ones. Without them a player who has left the board can only be
                # described by their peak, which says nothing about why they fell
                # off - the interesting question is where they were standing when
                # they went, not how high they once got.
                "lr": r["rating"], "lk": r["rank"],
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
            p["lr"] = r["rating"]
            p["lk"] = r["rank"]

    ratings = sorted([r["rating"] for r in rows if r["rating"] is not None])
    med = ratings[len(ratings) // 2] if ratings else None

    def pct(sorted_vals, p):
        if not sorted_vals:
            return None
        return sorted_vals[min(len(sorted_vals) - 1,
                               int(round((len(sorted_vals) - 1) * p)))]

    # Roster churn against the previous snapshot. This can only be measured here,
    # where both rosters are in hand - the page would otherwise have to download
    # every player file to reconstruct it.
    prev = {r.get("name"): r for r in (agg.get("current") or []) if r.get("name")}
    prev_names = set(prev)
    now_names = {r["name"] for r in rows if r.get("name")}
    entered = len(now_names - prev_names) if prev_names else len(now_names)
    left = len(prev_names - now_names)
    held = len(now_names & prev_names)
    stab = round(held / len(prev_names), 4) if prev_names else None

    # Games actually played since the previous snapshot, summed over everyone who
    # was on the board both times. Accounts that arrived this hour are skipped -
    # their earlier game count was never observed, so counting their whole season
    # total as "this hour" would invent activity. Derivable from the player files
    # after the fact, but only by downloading all of them; one integer here makes
    # the activity curve free to draw.
    played = 0
    for r in rows:
        p = prev.get(r.get("name"))
        if not p:
            continue
        d = ((r.get("games") or 0) - (p.get("games") or 0))
        if d > 0:
            played += d

    # The eligibility threshold, OBSERVED rather than assumed. Nobody can hold a
    # slot without the minimum number of matches, so the smallest game count on
    # the board is that minimum (or just above it). This is the only way to check
    # the weekly schedule against reality instead of trusting arithmetic from the
    # season start - and the moment it jumps, a requirement rise has happened.
    games_on_board = sorted([r["games"] for r in rows if r.get("games") is not None])
    min_games = games_on_board[0] if games_on_board else None

    agg["series"].append({
        "t": iso,
        "c": len(rows),
        "r1": rows[0]["rating"] if rows else None,
        "r250": ratings[0] if ratings else None,
        "med": med,
        "q1": pct(ratings, 0.25),             # rating quartiles: reconstructing these
        "q3": pct(ratings, 0.75),             # later would need every player file
        "new": entered,                       # first appearance since last snapshot
        "gone": left,                         # on the board last time, not now
        "stab": stab,                         # share of the previous board still there
        "ever": len(agg["players"]),          # cumulative distinct accounts this season
        "g": played,                          # games played across the board this hour
        "mg": min_games,                      # observed match requirement
    })
    if len(agg["series"]) > MAX_SERIES:
        agg["series"] = agg["series"][-MAX_SERIES:]

    agg["snapshots"] = agg.get("snapshots", 0) + 1
    agg["firstAt"] = agg.get("firstAt") or iso
    agg["lastAt"] = iso
    agg["current"] = rows
    return agg


def epoch(iso_str):
    """ISO timestamp -> epoch seconds, or None. Stored as an integer because a
    full ISO string per point roughly doubles the size of every player file."""
    if not iso_str:
        return None
    try:
        return int(datetime.strptime(str(iso_str)[:19], "%Y-%m-%dT%H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


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
        # Index 5 is the API's own `date` for this entry - when the account last
        # played - as epoch seconds. It is the single most useful field we were
        # discarding: the board file that carries it is overwritten every hour, so
        # every past hour's version is gone for good. Win/loss deltas can only say
        # "sometime in the last hour or six"; this says exactly when, which is what
        # a real activity curve and any session analysis need.
        point = [iso, r.get("rank"), r.get("rating"), r.get("wins"), r.get("losses"),
                 epoch(r.get("date"))]
        if pts:
            last = pts[-1]
            # Compare rank/rating/wins/losses only. The last-match stamp is
            # deliberately excluded: including it would make every point "changed"
            # the moment a player queues, defeating the change detection that keeps
            # these files and their git diffs small.
            unchanged = last[1:5] == point[1:5]
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
    # Read the season we are already recording BEFORE asking the API, so a
    # season that goes backwards can be rejected instead of silently adopted.
    known_start = None
    try:
        prior = load_optional(HISTORY) or {}
        known_start = prior.get("start")
    except Exception:                            # noqa: BLE001 - never block on this
        known_start = None

    season = active_season(known_start)
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

    # The one case worth breaking the gap guard for: a probe pass, minutes after a
    # scheduled requirement rise, that can SEE the rise. The guard exists to drop
    # duplicate snapshots at workflow handover, where nothing has changed; here
    # something has, and it is the single most informative point in the season.
    # A probe that observes no change still writes nothing, so the series does not
    # fill up with near-identical points on bump days.
    if too_soon and rows:
        try:
            gob = sorted([r["games"] for r in rows if r.get("games") is not None])
            mg_seen = gob[0] if gob else None
            mg_last = prev_series[-1].get("mg") if prev_series else None
            now_dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (mg_seen is not None and mg_last is not None and mg_seen > mg_last
                    and probe_window(season, now_dt) is not None):
                print("  probe pass sees the minimum move %s -> %s only %.0fs after"
                      " the last point - recording it anyway"
                      % (mg_last, mg_seen, gap))
                too_soon = False
        except (ValueError, TypeError, KeyError, IndexError):
            pass

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
        emit_wake(season, hist)
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
        # Captured BEFORE the merge: once merge() runs, agg["current"] is this
        # pass's board and the pre-rise roster is gone for good. The history keeps
        # only the latest board, so there is no second chance to read it.
        agg_now = hist["regions"][REGION]

        # Refuse a board that is not ours. Skipping an hour costs one data point;
        # accepting a foreign board permanently corrupts every cumulative count in
        # the file, and no later pass can undo it.
        ov = foreign_board(agg_now, rows, iso)
        if ov is not None:
            print("  REFUSED: only %.1f%% of this board has been seen here in the"
                  " last %dh. This is almost certainly another region's ladder -"
                  " the API serves the board for the caller's location and this"
                  " runner has been geolocated elsewhere. Keeping the previous"
                  " board and history untouched." % (ov * 100, FOREIGN_WINDOW_H))
            print("  (first few unfamiliar names: %s)"
                  % ", ".join(sorted(set(r.get("name") for r in rows
                                         if r.get("name")))[:5]))
            emit_wake(season, hist)
            return 0

        pre_rows = agg_now.get("current") or []
        pre_names = [r.get("name") for r in pre_rows if r.get("name")]
        pre_ratings = sorted([r["rating"] for r in pre_rows if r.get("rating") is not None])
        pre_cut = pre_ratings[0] if pre_ratings else None

        hist["regions"][REGION] = merge(hist["regions"][REGION], rows, iso)
        changed = True
        try:
            ser = hist["regions"][REGION].get("series") or []
            mg_now = ser[-1].get("mg") if ser else None
            mg_prev = ser[-2].get("mg") if len(ser) > 1 else None
            record_bump(hist["regions"][REGION], season, rows, iso,
                        pre_names, pre_cut, len(pre_names), mg_now, mg_prev)
        except Exception as e:                   # noqa: BLE001
            # A measurement is not worth losing an hour of history over.
            print("  bump bookkeeping failed (%s); snapshot kept" % e)
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
        emit_wake(season, hist)
        return 0

    save(HISTORY, hist)
    for region, a in sorted(hist["regions"].items()):
        print("  %s: %d snapshots, %d players ever seen%s"
              % (region, a.get("snapshots", 0), len(a.get("players", {})),
                 "" if region == REGION else "  (other collector)"))
    print("wrote %s" % HISTORY)
    emit_wake(season, hist)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                       # noqa: BLE001 - fail loudly in CI
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
