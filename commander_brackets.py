"""Explainable Commander bracket floors, with Commander Spellbook card/combo data.

Rules: Wizards' October 2025 framework, February 2026 Game Changer update.
Spellbook contract: https://backend.commanderspellbook.com/schema/
Its speed is a mana/prerequisite heuristic (higher is faster), NOT a turn number.
We use classified evidence, not its bracketTag or a power-points score.
"""
from collections import Counter, OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time

import requests

SNAPSHOT = json.loads((Path(__file__).parent / "data_sources/commander_game_changers.json").read_text())
GAME_CHANGERS = {name.casefold(): name for name in SNAPSHOT["cards"]}
SPELLBOOK_URL = "https://backend.commanderspellbook.com/estimate-bracket"
_cache = OrderedDict()
_lock = threading.Lock()
_failure_until = 0.0


def _names(names):
    return Counter(name.strip().casefold() for name in names if name.strip())


def _classifications(main, commanders):
    """Bounded per-process TTL cache; short outage backoff avoids repeated timeouts."""
    global _failure_until
    key = (tuple(sorted(main.items())), tuple(sorted(commanders.items())))
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            _cache.move_to_end(key)
            return deepcopy(cached[1])
        if _failure_until > now:
            raise ValueError("Spellbook temporarily unavailable")
    try:
        response = requests.post(
            SPELLBOOK_URL,
            json={"main": [{"card": n, "quantity": q} for n, q in main.items()],
                  "commanders": [{"card": n, "quantity": q} for n, q in commanders.items()]},
            headers={"User-Agent": "CommanderTracker/1.0", "Accept": "application/json"},
            timeout=(2, 6),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("cards"), list) or not isinstance(data.get("combos"), list):
            raise ValueError("Invalid Spellbook response")
        for item in data["cards"]:
            if not isinstance(item.get("card", {}).get("name"), str) or any(
                type(item.get(flag)) is not bool for flag in ("gameChanger", "banned", "massLandDenial", "extraTurn")
            ):
                raise ValueError("Invalid card classification")
        for item in data["combos"]:
            if not isinstance(item.get("combo", {}).get("uses"), list) or any(
                type(item.get(flag)) is not bool for flag in ("relevant", "definitelyTwoCard", "massLandDenial", "extraTurn")
            ) or type(item.get("speed")) is not int:
                raise ValueError("Invalid combo classification")
            combo = item["combo"]
            if not isinstance(combo.get("id"), str) or not isinstance(combo.get("requires"), list):
                raise ValueError("Invalid combo identity or prerequisites")
            for use in combo["uses"]:
                if not isinstance(use.get("card", {}).get("name"), str) or type(use.get("quantity")) is not int or use["quantity"] < 1:
                    raise ValueError("Invalid combo ingredient")
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        with _lock:
            _failure_until = time.monotonic() + 30
        raise ValueError("Spellbook unavailable") from exc
    with _lock:
        _cache[key] = (time.monotonic() + 3600, deepcopy(data))
        _cache.move_to_end(key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return data


def estimate_bracket(cards, *, commanders=(), declared_bracket=None):
    main, command = _names(cards), _names(commanders)
    for name in command:
        main.pop(name, None)  # Some imports repeat the commander in the mainboard.
    all_cards = main + command
    report = {
        "bracket": None, "estimated_bracket": None, "declared_bracket": declared_bracket,
        "status": "unknown", "rules_version": SNAPSHOT["rules_version"],
        "game_changer_list_date": SNAPSHOT["updated_at"],
        "source": "local_snapshot", "checked_at": datetime.now(timezone.utc).isoformat(),
        "reasons": [], "warnings": [], "combos": [], "unresolved_cards": [],
        "matched_cards": {"game_changers": [], "mass_land_denial": [], "extra_turns": [], "banned": []},
        "signals": {},
    }
    if not all_cards:
        report["warnings"].append("Add a decklist to estimate its bracket.")
        return report
    if sum(all_cards.values()) > 600 or sum(command.values()) > 12:
        report["warnings"].append("Bracket checks support at most 600 cards and 12 commanders.")
        return report

    gc = {n: GAME_CHANGERS[n] for n in all_cards if n in GAME_CHANGERS}
    floor = 2
    report["status"] = "partial"
    try:
        data = _classifications(main, command)
        report["source"] = "commander_spellbook"
        # The live classifications supersede the dated fallback, including removals.
        gc = {}
        resolved = set()
        for item in data["cards"]:
            name = item["card"]["name"]
            key = name.casefold()
            # Split-card front names are accepted by Spellbook's resolver.
            aliases = {key, key.split(" // ")[0]}
            input_key = next((n for n in aliases if n in all_cards), None)
            if input_key is None:
                continue
            resolved.add(input_key)
            if item["gameChanger"]:
                gc[input_key] = name
            for flag, bucket in (("massLandDenial", "mass_land_denial"), ("extraTurn", "extra_turns"), ("banned", "banned")):
                if item[flag]:
                    report["matched_cards"][bucket].append(name)
        report["unresolved_cards"] = sorted(set(all_cards) - resolved)
        # Still recognize official fallback names that the service could not resolve.
        gc.update({n: GAME_CHANGERS[n] for n in report["unresolved_cards"] if n in GAME_CHANGERS})
        report["status"] = "partial" if report["unresolved_cards"] else "estimated"
        for item in data["combos"]:
            combo = item["combo"]
            required = Counter()
            valid = True
            names = []
            for use in combo["uses"]:
                name = use["card"]["name"]
                key = name.casefold()
                if key not in all_cards:
                    key = key.split(" // ")[0]
                required[key] += use.get("quantity", 1)
                names.append(name)
                if use.get("mustBeCommander") and key not in command:
                    valid = False
            # Never report near-misses, missing quantities or unresolved templates as combos.
            if not required or not valid or required - all_cards or combo.get("requires"):
                continue
            combo_floor = None
            explanation = "Known combo; review its prerequisites and intended use."
            if item["massLandDenial"] or item["extraTurn"]:
                combo_floor = 4
                explanation = "Mass land denial or chained extra turns."
            elif item["relevant"] and item["definitelyTwoCard"]:
                combo_floor = 4 if item["speed"] >= 4 else 3
                explanation = ("Potential early two-card combo; review setup and consistency." if combo_floor == 4
                               else "Two-card combo; Bracket 3 requires it to be a late-game plan.")
            if combo_floor:
                floor = max(floor, combo_floor)
                report["reasons"].append(" + ".join(names) + ": " + explanation)
            report["combos"].append({"id": str(combo["id"]), "cards": names,
                                     "bracket_floor": combo_floor, "explanation": explanation,
                                     "url": "https://commanderspellbook.com/combo/" + str(combo["id"]) + "/"})
    except (ValueError, TypeError, KeyError, AttributeError):
        # Discard any partially interpreted response rather than claiming complete checks.
        floor = 2
        gc = {n: GAME_CHANGERS[n] for n in all_cards if n in GAME_CHANGERS}
        report.update(status="partial", source="local_snapshot", reasons=[], combos=[], unresolved_cards=[])
        report["matched_cards"] = {"game_changers": [], "mass_land_denial": [], "extra_turns": [], "banned": []}
        report["warnings"].append("Live card and combo checks are unavailable. This partial estimate only checks the dated Game Changer list; land denial, extra turns, combos and bans are unchecked.")

    gc_count = sum(all_cards[n] for n in gc)
    report["matched_cards"]["game_changers"] = sorted(gc.values())
    if gc_count:
        floor = max(floor, 4 if gc_count > 3 else 3)
        report["reasons"].append(f"{gc_count} Game Changer(s): " + ", ".join(sorted(gc.values())) + (". More than three requires Bracket 4+." if gc_count > 3 else ". Requires Bracket 3+."))
    if report["matched_cards"]["mass_land_denial"]:
        floor = 4
        report["reasons"].append("Mass land denial: " + ", ".join(report["matched_cards"]["mass_land_denial"]) + ". Requires Bracket 4+.")
    if report["matched_cards"]["extra_turns"]:
        report["warnings"].append("Extra-turn cards: " + ", ".join(report["matched_cards"]["extra_turns"]) + ". Chaining extra turns requires Bracket 4; card count alone does not prove a chain.")
    if report["unresolved_cards"]:
        report["warnings"].append("Unrecognized cards were not fully checked: " + ", ".join(report["unresolved_cards"]))
    if not report["reasons"]:
        report["reasons"].append("No higher-bracket restriction detected in the available checks. Bracket 2 is a starting point, not a power rating.")
    if report["matched_cards"]["banned"]:
        floor = None
        report["status"] = "invalid"
        report["warnings"].append("Cards banned in Commander: " + ", ".join(report["matched_cards"]["banned"]) + ". No normal bracket assigned.")
    report["warnings"].append("Speed, consistency and intent need player judgment. Bracket 1 (Exhibition) and Bracket 5 (cEDH) are owner declarations. This is not a full deck-legality check.")
    if declared_bracket and floor and declared_bracket < floor:
        report["warnings"].append(f"Declared Bracket {declared_bracket} is below the detected floor of {floor}; discuss exceptions with your group.")
    report["bracket"] = report["estimated_bracket"] = floor
    report["signals"] = {"game_changer_count": gc_count, "combo_count": len(report["combos"])}
    return report
