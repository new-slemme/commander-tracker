#!/usr/bin/env python3
"""One-time loader for the pre-wipe historical stat baseline.

Context: a block of games played before the April backup was lost in the DB
wipe and survives only as aggregate tallies in the Android offline cache
(``edh_offline.xml``). This script folds that history back in by setting
``player.historical_*`` and ``deck.historical_*`` to ``phone_total - recorded``,
so the leaderboard reflects true all-time totals without fabricating game rows.

The baseline is a FIXED constant once loaded — it is the count of games that
happened but are not present as rows. New games recorded later add to the
recorded count on top of it, so displayed totals keep growing correctly.

Run this ONCE, on the restored DB, before any new games are recorded.

DANGER — do not re-run after new games exist: the baseline is recomputed as
``phone_total - currently_recorded``, so if N new games are recorded and this is
re-run, the displayed total is pinned back to ``phone_total`` and those N new
games' contribution is silently ERASED from every winrate on the site. This is a
regression, not a harmless refresh. As a safety net the script refuses to APPLY
when any ``historical_*`` value is already non-zero unless ``FORCE=1`` is set.

Players are matched by name; decks by (name, owner). Mismatches and any
negative deltas are reported and skipped rather than written.

Usage (against a specific DB — NEVER the live file directly without a backup):
    COMMANDER_DB_FILE=/path/to/commander.db \
    HISTORICAL_CACHE_XML=/path/to/edh_offline.xml \
    python3 scripts/load_historical_stats.py            # dry run, reports only
    ... APPLY=1 python3 scripts/load_historical_stats.py # actually writes
"""

import json
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.getenv("COMMANDER_DB_FILE", os.path.join(REPO_ROOT, "data", "commander.db"))
CACHE_XML = os.getenv("HISTORICAL_CACHE_XML", os.path.join(REPO_ROOT, "edh_offline.xml"))
APPLY = os.getenv("APPLY") == "1"
FORCE = os.getenv("FORCE") == "1"


def load_phone_cache(path: str):
    root = ET.parse(path).getroot()
    data = {el.get("name"): (el.text if el.tag == "string" else el.get("value")) for el in root}
    players = json.loads(data["cached_players"])
    decks = json.loads(data["cached_setup_decks"])
    return players, decks


def recorded_player_stats(conn):
    rows = conn.execute(
        """
        SELECT p.name,
               SUM(CASE WHEN g.winner_id = gp.player_id THEN 1 ELSE 0 END) AS wins,
               COUNT(*) AS played
        FROM game_participant gp
        JOIN player p ON p.id = gp.player_id
        JOIN game g ON g.id = gp.game_id
        GROUP BY p.id
        """
    ).fetchall()
    return {name: (wins or 0, played or 0) for name, wins, played in rows}


def recorded_deck_stats(conn):
    rows = conn.execute(
        """
        SELECT gp.deck_id,
               SUM(CASE WHEN g.winner_id = gp.player_id THEN 1 ELSE 0 END) AS wins,
               COUNT(*) AS uses
        FROM game_participant gp
        JOIN game g ON g.id = gp.game_id
        GROUP BY gp.deck_id
        """
    ).fetchall()
    return {deck_id: (wins or 0, uses or 0) for deck_id, wins, uses in rows}


def main() -> int:
    if not os.path.exists(DB_FILE):
        print(f"[hist] DB not found: {DB_FILE}", file=sys.stderr)
        return 1
    if not os.path.exists(CACHE_XML):
        print(f"[hist] cache XML not found: {CACHE_XML}", file=sys.stderr)
        return 1

    phone_players, phone_decks = load_phone_cache(CACHE_XML)
    conn = sqlite3.connect(DB_FILE)
    try:
        # Player.name is unique in the schema, but guard anyway so a surprise
        # collision is reported rather than silently overwriting a mapping.
        db_players: dict[str, int] = {}
        ambiguous_players: set[str] = set()
        for pid, name in conn.execute("SELECT id, name FROM player"):
            if name in db_players:
                ambiguous_players.add(name)
            db_players[name] = pid
        # (name, owner) is NOT unique in the schema — track collisions so an
        # ambiguous deck is reported and skipped rather than silently mis-bound.
        db_decks: dict[tuple[str, str], int] = {}
        ambiguous_decks: set[tuple[str, str]] = set()
        for did, dname, pname in conn.execute(
            "SELECT d.id, d.name, p.name FROM deck d JOIN player p ON p.id = d.player_id"
        ):
            key = (dname.strip().lower(), pname.strip().lower())
            if key in db_decks:
                ambiguous_decks.add(key)
            db_decks[key] = did
        rec_p = recorded_player_stats(conn)
        rec_d = recorded_deck_stats(conn)

        already = conn.execute(
            "SELECT COUNT(*) FROM player WHERE historical_wins != 0 OR historical_games != 0"
        ).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM deck WHERE historical_wins != 0 OR historical_games != 0"
        ).fetchone()[0]

        player_updates = []
        skipped = []
        for pp in phone_players:
            name = pp["name"]
            if name in ambiguous_players:
                skipped.append(f"player '{name}' is ambiguous (duplicate DB rows)")
                continue
            pid = db_players.get(name)
            if pid is None:
                skipped.append(f"player '{name}' has no DB row")
                continue
            if pp["wins"] > pp["played"]:
                skipped.append(f"player '{name}' corrupt cache: wins {pp['wins']} > played {pp['played']}")
                continue
            rec_w, rec_pl = rec_p.get(name, (0, 0))
            hist_w = pp["wins"] - rec_w
            hist_g = pp["played"] - rec_pl
            if hist_w < 0 or hist_g < 0 or hist_w > hist_g:
                skipped.append(f"player '{name}' bad delta w={hist_w} g={hist_g}")
                continue
            player_updates.append((pid, name, hist_w, hist_g))

        deck_updates = []
        for pd in phone_decks:
            key = (pd["name"].strip().lower(), (pd.get("player_name") or "").strip().lower())
            if key in ambiguous_decks:
                skipped.append(f"deck '{pd['name']}' / '{pd.get('player_name')}' is ambiguous (duplicate DB rows)")
                continue
            did = db_decks.get(key)
            if did is None:
                skipped.append(f"deck '{pd['name']}' / '{pd.get('player_name')}' has no DB row")
                continue
            if pd["wins"] > pd["uses"]:
                skipped.append(f"deck '{pd['name']}' corrupt cache: wins {pd['wins']} > uses {pd['uses']}")
                continue
            rec_w, rec_u = rec_d.get(did, (0, 0))
            hist_w = pd["wins"] - rec_w
            hist_g = pd["uses"] - rec_u
            if hist_w < 0 or hist_g < 0 or hist_w > hist_g:
                skipped.append(f"deck '{pd['name']}' bad delta w={hist_w} g={hist_g}")
                continue
            deck_updates.append((did, pd["name"], hist_w, hist_g))

        print(f"[hist] DB: {DB_FILE}")
        print(f"[hist] players to update: {len(player_updates)}  decks: {len(deck_updates)}  skipped: {len(skipped)}")
        for pid, name, w, g in player_updates:
            print(f"    player {name:12} historical_wins={w:3} historical_games={g:3}")
        total_hist_wins = sum(w for _, _, w, _ in player_updates)
        print(f"[hist] total historical wins (= distinct pre-wipe games): {total_hist_wins}")
        for s in skipped:
            print(f"    SKIP: {s}")

        if not APPLY:
            print("[hist] dry run (set APPLY=1 to write).")
            return 0

        if already and not FORCE:
            print(
                f"[hist] REFUSING: {already} row(s) already carry a historical baseline. "
                "Re-running recomputes phone_total - recorded, which silently erases any "
                "games recorded since the first load. Set FORCE=1 only if you are certain "
                "no new games have been recorded.",
                file=sys.stderr,
            )
            return 3

        for pid, _name, w, g in player_updates:
            conn.execute(
                "UPDATE player SET historical_wins = ?, historical_games = ? WHERE id = ?",
                (w, g, pid),
            )
        for did, _name, w, g in deck_updates:
            conn.execute(
                "UPDATE deck SET historical_wins = ?, historical_games = ? WHERE id = ?",
                (w, g, did),
            )
        conn.commit()
        print("[hist] applied.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
