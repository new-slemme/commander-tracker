from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests


# Base URL of the Card Conjurer (cc-auto) instance, e.g. "http://cardconjurer:4242".
# When set, URLs from that host are recognised as custom-set imports.
_CCAUTO_BASE_URL = os.getenv("CCAUTO_BASE_URL", "").rstrip("/")


class DeckParserError(ValueError):
    """User-safe error raised when import input cannot be parsed."""


@dataclass
class ParsedCardEntry:
    name: str
    quantity: int


@dataclass
class ParsedDeck:
    source: str
    commander: str | None
    commanders: list[str]
    sections: dict[str, list[ParsedCardEntry]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "commander": self.commander,
            "commanders": self.commanders,
            "sections": {
                section: [{"name": e.name, "quantity": e.quantity} for e in entries]
                for section, entries in self.sections.items()
            },
        }


_SECTION_CANONICAL = {
    "commander": "commander",
    "commanders": "commander",
    "main": "mainboard",
    "mainboard": "mainboard",
    "deck": "mainboard",
    "decklist": "mainboard",
    "sideboard": "sideboard",
    "side": "sideboard",
    "maybeboard": "maybeboard",
    "maybe": "maybeboard",
    "considering": "maybeboard",
}

_SUPPORTED_DOMAINS = {
    "moxfield.com": "moxfield",
    "www.moxfield.com": "moxfield",
    "archidekt.com": "archidekt",
    "www.archidekt.com": "archidekt",
}

_MOXFIELD_PATH_RE = re.compile(r"^/decks/(?P<deck_id>[A-Za-z0-9_-]{6,})/?")
_ARCHIDEKT_PATH_RE = re.compile(r"^/decks/(?P<deck_id>\d+)(?:/|$)")
_CCAUTO_SET_PATH_RE = re.compile(r"^/sets/(?P<set_name>[A-Za-z0-9_-]+?)(?:\.json)?/?$")


def parse_deck_input(raw_input: str, *, timeout: int = 15) -> dict[str, Any]:
    """
    Parse a user deck import value and return a unified structure.

    Returns:
        {
            "source": "moxfield"|"archidekt"|"text",
            "commander": str | None,
            "commanders": [str, ...],
            "sections": {
                "mainboard": [{"name": str, "quantity": int}, ...],
                "commander": [...],
                "sideboard": [...],
                "maybeboard": [...],
            },
        }
    """
    text = (raw_input or "").strip()
    if not text:
        raise DeckParserError("Please paste a deck URL or plain-text deck list.")

    parsed_url = _safe_parse_url(text)
    if parsed_url:
        # Check Card Conjurer custom-set URLs before the standard domain table.
        if _CCAUTO_BASE_URL:
            ccauto_parsed = _safe_parse_url(_CCAUTO_BASE_URL)
            if (
                ccauto_parsed
                and parsed_url.netloc.lower() == ccauto_parsed.netloc.lower()
                and _CCAUTO_SET_PATH_RE.match(parsed_url.path)
            ):
                return parse_ccauto_set_url(text, timeout=timeout).as_dict()

        domain_type = _SUPPORTED_DOMAINS.get(parsed_url.netloc.lower())
        if not domain_type:
            raise DeckParserError(
                "Unsupported deck URL. Please use moxfield.com or archidekt.com, "
                "or paste plain-text cards."
            )
        if domain_type == "moxfield":
            return parse_moxfield_url(text, timeout=timeout).as_dict()
        return parse_archidekt_url(text, timeout=timeout).as_dict()

    return parse_plaintext_decklist(text).as_dict()


def parse_moxfield_url(url: str, *, timeout: int = 15) -> ParsedDeck:
    deck_id = _extract_moxfield_deck_id(url)
    api_urls = [
        f"https://api2.moxfield.com/v3/decks/all/{deck_id}",
        f"https://api.moxfield.com/v2/decks/all/{deck_id}",
        f"https://api2.moxfield.com/v2/decks/all/{deck_id}",
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.moxfield.com",
        "Referer": "https://www.moxfield.com/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    response: requests.Response | None = None
    last_error: requests.RequestException | None = None

    for api_url in api_urls:
        try:
            candidate = requests.get(api_url, timeout=timeout, headers=headers)
        except requests.RequestException as exc:
            last_error = exc
            continue

        if candidate.status_code in {200, 404}:
            response = candidate
            break

        if candidate.status_code == 403:
            continue

        response = candidate
        break

    if response is None:
        if last_error:
            raise DeckParserError(f"Could not reach Moxfield: {last_error}") from last_error
        raise DeckParserError(
            "Moxfield import failed (HTTP 403). This deck may be private or blocked by Moxfield."
        )

    if response.status_code == 404:
        raise DeckParserError("Moxfield deck not found. Please verify the URL is correct and public.")
    if response.status_code != 200:
        raise DeckParserError(f"Moxfield import failed (HTTP {response.status_code}).")

    payload = _parse_json_response(response, "Moxfield")

    section_buckets: dict[str, dict[str, ParsedCardEntry]] = {}

    _merge_mapping_cards(section_buckets, "mainboard", _extract_moxfield_section(payload, "mainboard"))
    _merge_mapping_cards(section_buckets, "sideboard", _extract_moxfield_section(payload, "sideboard"))
    _merge_mapping_cards(section_buckets, "maybeboard", _extract_moxfield_section(payload, "maybeboard"))

    commanders_payload = (
        _extract_moxfield_section(payload, "commanders")
        or _extract_moxfield_section(payload, "commander")
        or {}
    )
    _merge_mapping_cards(section_buckets, "commander", commanders_payload)

    sections = _finalize_sections(section_buckets)
    commanders = _detect_commanders(sections)
    commander = " + ".join(commanders) if commanders else None

    if not sections:
        raise DeckParserError("Moxfield deck did not contain any card entries.")

    return ParsedDeck(source="moxfield", commander=commander, commanders=commanders, sections=sections)


def parse_archidekt_url(url: str, *, timeout: int = 15) -> ParsedDeck:
    deck_id = _extract_archidekt_deck_id(url)
    api_url = f"https://archidekt.com/api/decks/{deck_id}/"

    try:
        response = requests.get(api_url, timeout=timeout)
    except requests.RequestException as exc:
        raise DeckParserError(f"Could not reach Archidekt: {exc}") from exc

    if response.status_code == 404:
        raise DeckParserError("Archidekt deck not found. Please verify the URL is correct and public.")
    if response.status_code != 200:
        raise DeckParserError(f"Archidekt import failed (HTTP {response.status_code}).")

    payload = _parse_json_response(response, "Archidekt")
    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise DeckParserError("Archidekt payload is missing card entries.")

    section_buckets: dict[str, dict[str, ParsedCardEntry]] = {}

    for card_entry in cards:
        if not isinstance(card_entry, dict):
            continue
        quantity = card_entry.get("quantity", 1)
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue

        card_obj = card_entry.get("card") or {}
        name = card_obj.get("oracleCard", {}).get("name") or card_obj.get("name")
        if not name:
            continue

        categories = card_entry.get("categories") or []
        section = _categorize_archidekt(categories)
        _add_card(section_buckets, section, name, quantity)

    sections = _finalize_sections(section_buckets)
    commanders = _detect_commanders(sections)
    commander = " + ".join(commanders) if commanders else None

    if not sections:
        raise DeckParserError("Archidekt deck did not contain any card entries.")

    return ParsedDeck(source="archidekt", commander=commander, commanders=commanders, sections=sections)


def parse_ccauto_set_url(url: str, *, timeout: int = 15) -> ParsedDeck:
    """Import all cards in a Card Conjurer custom set as a mainboard list (1x each)."""
    parsed = _safe_parse_url(url)
    if not parsed:
        raise DeckParserError("Invalid Card Conjurer URL.")

    match = _CCAUTO_SET_PATH_RE.match(parsed.path)
    if not match:
        raise DeckParserError("Card Conjurer URL must look like /sets/<set-name>.")

    set_name = match.group("set_name")
    base = f"{parsed.scheme}://{parsed.netloc}"
    api_url = f"{base}/sets/{set_name}.json"

    try:
        response = requests.get(api_url, timeout=timeout)
    except requests.RequestException as exc:
        raise DeckParserError(f"Could not reach Card Conjurer: {exc}") from exc

    if response.status_code == 404:
        raise DeckParserError(
            f"Card Conjurer set '{set_name}' not found. Check the set name and try again."
        )
    if response.status_code != 200:
        raise DeckParserError(f"Card Conjurer import failed (HTTP {response.status_code}).")

    try:
        cards = response.json()
    except ValueError as exc:
        raise DeckParserError("Card Conjurer returned invalid JSON.") from exc

    if not isinstance(cards, list):
        raise DeckParserError("Card Conjurer set response is not a card list.")

    section_buckets: dict[str, dict[str, ParsedCardEntry]] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = card.get("name")
        if not name:
            continue
        _add_card(section_buckets, "mainboard", str(name), 1)

    sections = _finalize_sections(section_buckets)
    if not sections:
        raise DeckParserError("Card Conjurer set did not contain any cards.")

    return ParsedDeck(source="ccauto", commander=None, commanders=[], sections=sections)


def ccauto_named_exact(name: str, *, timeout: int = 5) -> dict | None:
    """Look up a card by name across all Card Conjurer sets.

    Returns the raw card dict (with 'name', 'mana', 'type', 'rules', etc.) or None.
    Requires CCAUTO_BASE_URL to be set.
    """
    if not _CCAUTO_BASE_URL or not name:
        return None

    name_lower = name.lower().strip()

    try:
        index_r = requests.get(f"{_CCAUTO_BASE_URL}/sets/index.json", timeout=timeout)
        if index_r.status_code != 200:
            return None
        sets = index_r.json()
        if not isinstance(sets, list):
            return None
    except (requests.RequestException, ValueError):
        return None

    for set_info in sets:
        if not isinstance(set_info, dict):
            continue
        set_name = set_info.get("name")
        if not set_name:
            continue
        try:
            cards_r = requests.get(
                f"{_CCAUTO_BASE_URL}/sets/{set_name}.json", timeout=timeout
            )
            if cards_r.status_code != 200:
                continue
            cards = cards_r.json()
            if not isinstance(cards, list):
                continue
            for card in cards:
                if (
                    isinstance(card, dict)
                    and (card.get("name") or "").lower().strip() == name_lower
                ):
                    return card
        except (requests.RequestException, ValueError):
            continue

    return None


def parse_plaintext_decklist(text: str) -> ParsedDeck:
    lines = text.splitlines()
    section = "mainboard"
    buckets: dict[str, dict[str, ParsedCardEntry]] = {}

    for i, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line:
            continue

        canonical_section = _parse_section_header(line)
        if canonical_section:
            section = canonical_section
            continue

        quantity, name = _parse_plaintext_card_line(line)
        if quantity is None or name is None:
            raise DeckParserError(
                f"Could not parse line {i}: '{original_line}'. Use formats like "
                "'1 Sol Ring' or 'Sol Ring x1'."
            )

        _add_card(buckets, section, name, quantity)

    sections = _finalize_sections(buckets)
    if not sections:
        raise DeckParserError("No valid card entries found in pasted deck list.")

    commanders = _detect_commanders(sections)
    commander = " + ".join(commanders) if commanders else None
    return ParsedDeck(source="text", commander=commander, commanders=commanders, sections=sections)


def _safe_parse_url(value: str):
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return parsed
    return None


def _extract_moxfield_deck_id(url: str) -> str:
    parsed = _safe_parse_url(url)
    if not parsed or parsed.netloc.lower() not in {"moxfield.com", "www.moxfield.com"}:
        raise DeckParserError("Invalid Moxfield URL. Example: https://www.moxfield.com/decks/<deck-id>")

    match = _MOXFIELD_PATH_RE.match(parsed.path)
    if not match:
        raise DeckParserError("Moxfield URL must look like /decks/<deck-id>.")

    return match.group("deck_id")


def _extract_archidekt_deck_id(url: str) -> str:
    parsed = _safe_parse_url(url)
    if not parsed or parsed.netloc.lower() not in {"archidekt.com", "www.archidekt.com"}:
        raise DeckParserError("Invalid Archidekt URL. Example: https://archidekt.com/decks/<deck-id>")

    match = _ARCHIDEKT_PATH_RE.match(parsed.path)
    if not match:
        raise DeckParserError("Archidekt URL must look like /decks/<deck-id>.")

    return match.group("deck_id")


def _parse_json_response(response: requests.Response, source_label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise DeckParserError(f"{source_label} returned invalid JSON.") from exc

    if not isinstance(data, dict):
        raise DeckParserError(f"{source_label} response shape was not a JSON object.")

    return data


def _parse_section_header(line: str) -> str | None:
    normalized = line.strip().rstrip(":").lower()
    return _SECTION_CANONICAL.get(normalized)


def _parse_plaintext_card_line(line: str) -> tuple[int | None, str | None]:
    cleaned = line.strip()
    cleaned = re.sub(r"^[*\-]\s+", "", cleaned)

    match = re.match(r"^(?P<qty>\d+)\s*[xX]?\s+(?P<name>.+)$", cleaned)
    if match:
        qty = int(match.group("qty"))
        name = _normalize_card_name(match.group("name"))
        return (qty, name) if qty > 0 and name else (None, None)

    match = re.match(r"^(?P<name>.+?)\s+[xX](?P<qty>\d+)$", cleaned)
    if match:
        qty = int(match.group("qty"))
        name = _normalize_card_name(match.group("name"))
        return (qty, name) if qty > 0 and name else (None, None)

    return None, None


def _categorize_archidekt(categories: list[str]) -> str:
    for item in categories:
        norm = str(item).strip().lower()
        if norm in _SECTION_CANONICAL:
            return _SECTION_CANONICAL[norm]
        if "commander" in norm:
            return "commander"
        if "side" in norm:
            return "sideboard"
        if "maybe" in norm or "consider" in norm:
            return "maybeboard"
    return "mainboard"


def _merge_mapping_cards(
    buckets: dict[str, dict[str, ParsedCardEntry]], section: str, payload: dict[str, Any] | list[Any]
) -> None:
    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            quantity = entry.get("quantity", 1)
            card_name = (
                (entry.get("card") or {}).get("name")
                or (entry.get("card") or {}).get("oracleCard", {}).get("name")
                or entry.get("name")
            )
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                continue
            if quantity <= 0 or not card_name:
                continue
            _add_card(buckets, section, str(card_name), quantity)
        return

    if not isinstance(payload, dict):
        return

    if isinstance(payload.get("cards"), (dict, list)):
        _merge_mapping_cards(buckets, section, payload.get("cards") or {})
        return

    for key, entry in payload.items():
        if isinstance(entry, dict):
            quantity = entry.get("quantity", 1)
            card_name = (entry.get("card") or {}).get("name") or entry.get("name") or key
        else:
            quantity = 1
            card_name = str(key)

        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue

        if quantity <= 0:
            continue

        _add_card(buckets, section, card_name, quantity)


def _extract_moxfield_section(payload: dict[str, Any], section: str) -> dict[str, Any] | list[Any]:
    direct = payload.get(section)
    if isinstance(direct, (dict, list)):
        return direct

    boards = payload.get("boards")
    if isinstance(boards, dict):
        board = boards.get(section)
        if isinstance(board, (dict, list)):
            return board

        # Some payloads use singular commander board names.
        if section in {"commander", "commanders"}:
            for alias in ("commanders", "commander"):
                board = boards.get(alias)
                if isinstance(board, (dict, list)):
                    return board

    return {}


def _add_card(
    buckets: dict[str, dict[str, ParsedCardEntry]], section: str, name: str, quantity: int
) -> None:
    if quantity <= 0:
        return

    canonical_name = _normalize_card_name(name)
    if not canonical_name:
        return

    norm_key = canonical_name.lower()
    section_bucket = buckets.setdefault(section, {})
    if norm_key in section_bucket:
        section_bucket[norm_key].quantity += quantity
    else:
        section_bucket[norm_key] = ParsedCardEntry(name=canonical_name, quantity=quantity)


def _normalize_card_name(name: str) -> str:
    normalized = str(name).replace("’", "'").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _finalize_sections(
    buckets: dict[str, dict[str, ParsedCardEntry]]
) -> dict[str, list[ParsedCardEntry]]:
    out: dict[str, list[ParsedCardEntry]] = {}
    for section, entries in buckets.items():
        if not entries:
            continue
        out[section] = sorted(entries.values(), key=lambda e: e.name.lower())
    return out


def _detect_commanders(sections: dict[str, list[ParsedCardEntry]]) -> list[str]:
    commander_entries = sections.get("commander") or []
    return [entry.name for entry in commander_entries if entry.name]
