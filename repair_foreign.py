#!/usr/bin/env python3
"""One-off repair: remove a foreign region's board from the NA history.

On 2026-09-22T06:00:06Z the collector was served the EU ladder and recorded it
as NA. The ladder API returns the board for the CALLER's location and never says
which one it gave you, and GitHub had placed the runner in Europe for that one
request. The result: 251 accounts that exist in exactly one snapshot, a bogus
churn reading, and every cumulative statistic in the file inflated.

The signature, which this script uses to find the pass rather than trusting a
hardcoded timestamp: a snapshot that replaced almost the whole board while
recording ZERO games played. Real churn requires somebody to finish a match, so
that combination cannot occur naturally.

What it does:
  - finds contaminated passes by signature
  - drops those series points
  - deletes accounts whose ENTIRE recorded existence is a contaminated pass
    (an account seen elsewhere too is kept; only its sighting count is adjusted)
  - blanks the churn fields on the FOLLOWING pass, whose new/gone/stab were
    computed against the foreign board and are therefore meaningless. They are
    set to null rather than interpolated: a missing measurement is honest, an
    invented one is not.
  - corrects the cumulative "ever" counts and the snapshot total
  - removes the orphaned per-player files

Run from the repository root. Writes nothing unless --apply is passed.
"""

import json
import os
import shutil
import sys
import urllib.parse
from datetime import datetime

HISTORY = os.path.join("data", "history.json")
PLAYER_ROOT = os.path.join("data", "players")

# A pass is contaminated when it replaced at least this share of the board while
# recording no games at all.
MIN_REPLACED = 0.80


# A contaminated pass introduces a crowd of accounts that are never seen again.
# Below this many, a signature match is a recovery pass, not a foreign board.
MIN_PHANTOMS = 50


def find_contaminated(series, players):
    """Split signature-matching passes into foreign boards and recovery passes.

    Both look identical at first glance: a foreign board replaces the roster and
    records zero games, and so does the pass that switches BACK, because it too
    is diffed against a roster it shares nobody with. Deleting both would throw
    away a perfectly good NA snapshot.

    What separates them is who arrived. A foreign pass brings hundreds of
    accounts that are born and die in that single timestamp. The recovery pass
    brings back accounts that were already known, so almost nothing is new.

    Returns (foreign_indices, recovery_indices).
    """
    born_and_died = {}
    for p in players.values():
        f, l = p.get("f"), p.get("l")
        if f and f == l:
            born_and_died[f] = born_and_died.get(f, 0) + 1

    foreign, recovery = [], []
    for i, p in enumerate(series):
        c = p.get("c") or 0
        gone, played = p.get("gone"), p.get("g")
        if not c or gone is None or played is None:
            continue
        if played == 0 and gone >= MIN_REPLACED * c:
            if born_and_died.get(p["t"], 0) >= MIN_PHANTOMS:
                foreign.append(i)
            else:
                recovery.append(i)
    return foreign, recovery


def main(apply):
    if not os.path.exists(HISTORY):
        sys.exit("no %s - run this from the repository root" % HISTORY)
    with open(HISTORY, encoding="utf-8") as f:
        hist = json.load(f)

    total_removed = 0
    for region, agg in sorted((hist.get("regions") or {}).items()):
        series = agg.get("series") or []
        players = agg.get("players") or {}
        bad, recovery = find_contaminated(series, players)
        if not bad and not recovery:
            print("%s: nothing contaminated" % region)
            continue

        bad_times = set(series[i]["t"] for i in bad)
        print("%s: %d foreign pass(es), %d recovery pass(es)"
              % (region, len(bad), len(recovery)))
        for i in bad:
            p = series[i]
            print("    FOREIGN  %s  replaced %s of %s with g=0 - dropping"
                  % (p["t"], p.get("gone"), p.get("c")))
        for i in recovery:
            print("    recovery %s  roster is ours, churn figures are not -"
                  " keeping the point, blanking new/gone/stab" % series[i]["t"])

        # Accounts whose whole existence is a contaminated pass. Checking first
        # AND last sighting matters: plenty of real players legitimately appear
        # in a single snapshot at some other hour, and those must be kept.
        doomed = [n for n, pl in players.items()
                  if pl.get("f") in bad_times and pl.get("l") in bad_times]
        print("    %d accounts exist only in those pass(es)" % len(doomed))

        # An account seen both inside and outside a bad pass keeps its record,
        # but its sighting count included the bad pass.
        touched = [n for n, pl in players.items()
                   if n not in set(doomed)
                   and (pl.get("f") in bad_times or pl.get("l") in bad_times)]
        if touched:
            print("    %d accounts were also seen outside - keeping, "
                  "adjusting counts" % len(touched))

        if not apply:
            print("    (dry run - nothing written)")
            total_removed += len(doomed)
            continue

        for n in doomed:
            players.pop(n, None)
        for n in touched:
            pl = players[n]
            if pl.get("n"):
                pl["n"] = max(1, pl["n"] - 1)

        # A recovery pass compared itself against the foreign board, so its churn
        # numbers describe a comparison that never happened. Null, not
        # interpolated: a missing measurement is honest, an invented one is not.
        for i in recovery:
            for k in ("new", "gone", "stab"):
                series[i][k] = None
            series[i]["recovered"] = True

        # Cumulative "ever" counts after the contamination are inflated by
        # exactly the accounts we just deleted, since none of them existed before.
        if bad:
            for p in series[min(bad):]:
                if p.get("ever") is not None:
                    p["ever"] = max(0, p["ever"] - len(doomed))

        agg["series"] = [p for i, p in enumerate(series) if i not in set(bad)]
        agg["snapshots"] = max(0, (agg.get("snapshots") or 0) - len(bad))
        agg["players"] = players

        # Orphaned per-player files. The collector writes them under a
        # double-encoded name, so encode the same way to find them.
        pdir = os.path.join(PLAYER_ROOT, region)
        gone_files = 0
        if os.path.isdir(pdir):
            for n in doomed:
                path = os.path.join(pdir, urllib.parse.quote(n, safe="") + ".json")
                if os.path.exists(path):
                    os.remove(path)
                    gone_files += 1
        print("    removed %d player files" % gone_files)
        print("    accounts now: %d   snapshots now: %d"
              % (len(players), agg["snapshots"]))
        total_removed += len(doomed)

    if not apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return

    backup = HISTORY + ".before-repair"
    shutil.copy2(HISTORY, backup)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, HISTORY)
    print("\nWrote %s (%d phantom accounts removed)." % (HISTORY, total_removed))
    print("Previous version saved as %s - delete it once you are happy."
          % backup)


if __name__ == "__main__":
    main("--apply" in sys.argv)
