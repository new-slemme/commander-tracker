from __future__ import annotations
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    send_from_directory,
    abort,
)
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path
from datetime import datetime
import json
import os
import re
import random
import requests
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, text, case
from sqlalchemy.orm import aliased
from functools import wraps
from uuid import uuid4

from deck_import import DeckParserError, parse_deck_input, parse_plaintext_decklist, ccauto_named_exact

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-default-change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/commander.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

DEFAULT_POD_NAME = "Der Keller â€“ Die Salzmine"
DEFAULT_POD_SLUG = "der-keller-die-salzmine"

ART_DIR = Path("/data/art")
ART_DIR.mkdir(parents=True, exist_ok=True)
COMMANDER_ART_DIR = ART_DIR / "commander_art"
COMMANDER_ART_DIR.mkdir(parents=True, exist_ok=True)
CARD_ART_DIR = ART_DIR / "card_art"
CARD_ART_DIR.mkdir(parents=True, exist_ok=True)
CARD_ART_INDEX_FILE = CARD_ART_DIR / "name_index.json"
CARD_ART_FAILURE_FILE = CARD_ART_DIR / "failure_index.json"
APK_DIR = Path(__file__).resolve().parent / "apk"
APK_DIR.mkdir(parents=True, exist_ok=True)
ANDROID_LATEST_RELEASE_MANIFEST = APK_DIR / "android-latest.json"
APK_VERSION_FILENAME_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<version_name>v?\d+(?:\.\d+)*)(?:\+(?P<version_code>\d+))?\.apk$",
    re.IGNORECASE,
)

# --- Dev/test bootstrap user (env-gated) ---
BOOTSTRAP_TEST_USER = os.getenv("BOOTSTRAP_TEST_USER", "0") == "1"
AUTO_LOGIN_TEST_USER = os.getenv("AUTO_LOGIN_TEST_USER", "0") == "1"

TEST_USERNAME = os.getenv("TEST_USERNAME", "test")
TEST_DISPLAY_NAME = os.getenv("TEST_DISPLAY_NAME", "Test User")
TEST_PASSWORD = os.getenv("TEST_PASSWORD", "test")  # dev only
TEST_IS_ADMIN = os.getenv("TEST_IS_ADMIN", "1") == "1"

COMMON_WEAK_PASSWORDS = {
    "password",
    "password123",
    "12345678",
    "123456789",
    "qwerty123",
    "letmein",
    "admin123",
}

MAX_PARTICIPANT_FLAGS_PAYLOAD_BYTES = 204800  # 200KB — supports up to 4 players × 500 turns of stats
ALLOWED_PARTICIPANT_FLAG_KEYS = {
    "mana_fucked",
    "misplayed",
    "monarch",
    "poison",
    "salt_count",
    "card_state",
    "turn_stats",
}

MAX_PER_PLAYER_TURN_STATS = 500

DECK_TAGS_VERSION = 2
KNOWN_DECK_TAG_KEYS = (
    "monarch",
    "initiative",
    "citys_blessing",
    "poison",
    "proliferate",
    "energy",
    "experience",
    "mana_fucked",
    "misplayed",
)
TRUST_LEGACY_DECK_TAGS = False

CCAUTO_BASE_URL = os.getenv("CCAUTO_BASE_URL", "").rstrip("/")

CARD_ART_CACHE_LOCK = threading.Lock()
CARD_ART_NAME_INDEX: dict[str, str] = {}
CARD_ART_FAILURE_INDEX: dict[str, dict[str, float | int | str]] = {}

COMMANDER_BRACKET_FAST_MANA = {
    "mana crypt",
    "jeweled lotus",
    "mox diamond",
    "chrome mox",
    "mox opal",
    "lion's eye diamond",
    "grim monolith",
}

COMMANDER_BRACKET_TUTORS = {
    "demonic tutor",
    "vampiric tutor",
    "imperial seal",
    "mystical tutor",
    "enlightened tutor",
    "worldly tutor",
    "gamble",
    "diabolic intent",
    "wishclaw talisman",
}

COMMANDER_BRACKET_CEDH_COMBOS = {
    "thassa's oracle",
    "demonic consultation",
    "tainted pact",
    "underworld breach",
    "ad nauseam",
    "necrotic ooze",
    "protean hulk",
}

CANONICAL_WIN_TYPES = {
    "combat",
    "combo",
    "alt_win",
    "concede",
    "time",
    "lock",
    "other",
}
LEGACY_WIN_TYPE_MAP = {
    "scoop": "concede",
    "infinite_turns": "combo",
    "mill": "other",
    "alternate_win": "alt_win",
    "altwin": "alt_win",
}

CANONICAL_TIMED_MODES = {
    "off",
    "chess_clock",
    "turn_timer",
}
LEGACY_TIMED_MODE_MAP = {
    "none": "off",
    "disabled": "off",
    "chess": "chess_clock",
    "clock": "chess_clock",
    "turn": "turn_timer",
}


def _check_card_art_storage_health() -> None:
    if not CARD_ART_DIR.exists():
        app.logger.error(
            "startup card art storage check failed: directory does not exist path=%s",
            CARD_ART_DIR,
        )
        return
    if not CARD_ART_DIR.is_dir():
        app.logger.error(
            "startup card art storage check failed: path is not a directory path=%s",
            CARD_ART_DIR,
        )
        return
    if not os.access(CARD_ART_DIR, os.W_OK):
        app.logger.warning(
            "startup card art storage check warning: directory is not writable path=%s",
            CARD_ART_DIR,
        )


_check_card_art_storage_health()


def canonicalize_win_type(value: str | None, *, unknown_default: str | None = None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    normalized = LEGACY_WIN_TYPE_MAP.get(raw, raw)
    if normalized in CANONICAL_WIN_TYPES:
        return normalized
    return unknown_default


def canonicalize_timed_mode(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    normalized = LEGACY_TIMED_MODE_MAP.get(raw, raw)
    if normalized in CANONICAL_TIMED_MODES:
        return normalized
    return None


def parse_participant_flags(raw_flags: str | None) -> dict[str, bool | int]:
    parsed_flags = {}
    payload = (raw_flags or "").strip()
    if not payload:
        return parsed_flags

    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return parsed_flags

    if not isinstance(loaded, dict):
        return parsed_flags

    mana_flag = loaded.get("mana_fucked")
    if isinstance(mana_flag, bool):
        parsed_flags["mana_fucked"] = mana_flag

    misplayed_flag = loaded.get("misplayed")
    if isinstance(misplayed_flag, bool):
        parsed_flags["misplayed"] = misplayed_flag

    monarch_flag = loaded.get("monarch")
    if isinstance(monarch_flag, bool):
        parsed_flags["monarch"] = monarch_flag

    poison_raw = loaded.get("poison")
    if isinstance(poison_raw, int) and not isinstance(poison_raw, bool) and poison_raw >= 0:
        parsed_flags["poison"] = min(poison_raw, 10)

    salt_count_raw = loaded.get("salt_count")
    if isinstance(salt_count_raw, int) and not isinstance(salt_count_raw, bool) and salt_count_raw >= 0:
        parsed_flags["salt_count"] = salt_count_raw
    elif isinstance(loaded.get("salted"), bool):
        # Backward compatibility for older records that used a boolean salted flag.
        parsed_flags["salt_count"] = 1 if loaded["salted"] else 0

    return parsed_flags


def sanitize_card_state_payload(raw_card_state, valid_player_ids: set[int]) -> dict | None:
    if not isinstance(raw_card_state, dict):
        return None

    raw_counters = raw_card_state.get("counters")
    sanitized_counters = {}
    if isinstance(raw_counters, dict):
        for key, value in raw_counters.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.strip().lower()
            if not normalized_key:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if value < 0:
                continue
            sanitized_counters[normalized_key] = min(value, 999)

    raw_commander_damage = raw_card_state.get("commander_damage")
    sanitized_commander_damage = {}
    if isinstance(raw_commander_damage, dict):
        for source_player_id_raw, damage in raw_commander_damage.items():
            try:
                source_player_id = int(source_player_id_raw)
            except (TypeError, ValueError):
                continue

            if source_player_id not in valid_player_ids:
                continue
            if not isinstance(damage, int) or isinstance(damage, bool):
                continue
            if damage < 0:
                continue
            sanitized_commander_damage[str(source_player_id)] = min(damage, 99)

    raw_statuses = raw_card_state.get("statuses")
    sanitized_statuses = {}
    if isinstance(raw_statuses, dict):
        for status_name, status_value in raw_statuses.items():
            if not isinstance(status_name, str):
                continue
            normalized_status_name = status_name.strip().lower()
            if not normalized_status_name:
                continue
            if isinstance(status_value, bool):
                sanitized_statuses[normalized_status_name] = status_value

    if not sanitized_counters and not sanitized_commander_damage and not sanitized_statuses:
        return None

    return {
        "counters": sanitized_counters,
        "commander_damage": sanitized_commander_damage,
        "statuses": sanitized_statuses,
    }


def participant_salt_count(parsed_flags: dict[str, bool | int] | None) -> int:
    if not parsed_flags:
        return 0
    salt_count_raw = parsed_flags.get("salt_count", 0)
    if isinstance(salt_count_raw, int) and not isinstance(salt_count_raw, bool):
        return max(0, salt_count_raw)
    return 0


def parse_participant_turn_stats(raw_flags: str | None) -> list[dict[str, int | bool]]:
    payload = (raw_flags or "").strip()
    if not payload:
        return []

    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return []

    turn_stats = loaded.get("turn_stats")
    if not isinstance(turn_stats, list):
        return []

    parsed_stats = []
    for entry in turn_stats:
        if not isinstance(entry, dict):
            continue

        turn = entry.get("turn")
        life_delta = entry.get("life_delta")
        mana_fucked = entry.get("mana_fucked")
        misplayed = entry.get("misplayed")
        turn_seconds = entry.get("turn_seconds", 0)

        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            continue
        if not isinstance(life_delta, int) or isinstance(life_delta, bool):
            continue
        if not isinstance(mana_fucked, bool):
            continue
        if not isinstance(misplayed, bool):
            continue
        if not isinstance(turn_seconds, int) or isinstance(turn_seconds, bool) or turn_seconds < 0:
            continue

        parsed_stats.append(
            {
                "turn": turn,
                "life_delta": life_delta,
                "mana_fucked": mana_fucked,
                "misplayed": misplayed,
                "turn_seconds": turn_seconds,
            }
        )

    parsed_stats.sort(key=lambda item: item["turn"])
    return parsed_stats


def validate_participant_seat_positions(participants):
    seat_positions = []
    for participant in participants:
        seat_position = participant.get("seat_position")
        if seat_position is None:
            return "Seat position is required", None
        if not isinstance(seat_position, int) or isinstance(seat_position, bool):
            return "Seat position must be an integer", None
        if seat_position < 1:
            return "Seat position must be at least 1", None
        seat_positions.append(seat_position)

    if len(set(seat_positions)) != len(seat_positions):
        return "Duplicate seat positions are not allowed within the same game", None

    expected = list(range(1, len(participants) + 1))
    if sorted(seat_positions) != expected:
        return f"Seat positions must be contiguous from 1 to {len(participants)}", None

    return None, seat_positions
def participant_hot_fields_from_flags(raw_flags: str | None) -> dict[str, int | bool]:
    parsed_flags = parse_participant_flags(raw_flags)
    turn_stats = parse_participant_turn_stats(raw_flags)

    life_delta_total = 0
    for entry in turn_stats:
        life_delta = entry.get("life_delta")
        if isinstance(life_delta, int) and not isinstance(life_delta, bool):
            life_delta_total += life_delta

    mana_fucked = parsed_flags.get("mana_fucked")
    misplayed = parsed_flags.get("misplayed")

    return {
        "salt_count": participant_salt_count(parsed_flags),
        "mana_fucked": mana_fucked if isinstance(mana_fucked, bool) else False,
        "misplayed": misplayed if isinstance(misplayed, bool) else False,
        "life_delta_total": life_delta_total,
    }


def participant_flags_snapshot(gp: "GameParticipant") -> dict[str, bool | int]:
    parsed_flags = parse_participant_flags(gp.flags_json)

    salt_count_raw = getattr(gp, "salt_count", None)
    if isinstance(salt_count_raw, int) and not isinstance(salt_count_raw, bool):
        salt_count = max(0, salt_count_raw)
    else:
        salt_count = participant_salt_count(parsed_flags)

    mana_fucked_raw = getattr(gp, "mana_fucked", None)
    if isinstance(mana_fucked_raw, bool):
        mana_fucked = mana_fucked_raw
    else:
        mana_fucked = bool(parsed_flags.get("mana_fucked", False))

    misplayed_raw = getattr(gp, "misplayed", None)
    if isinstance(misplayed_raw, bool):
        misplayed = misplayed_raw
    else:
        misplayed = bool(parsed_flags.get("misplayed", False))

    monarch = bool(parsed_flags.get("monarch", False))

    poison_raw = parsed_flags.get("poison", 0)
    if isinstance(poison_raw, int) and not isinstance(poison_raw, bool):
        poison = max(0, min(poison_raw, 10))
    else:
        poison = 0

    return {
        "mana_fucked": mana_fucked,
        "misplayed": misplayed,
        "monarch": monarch,
        "poison": poison,
        "salt_count": salt_count,
    }


def compute_game_mechanic_activation(parts: list["GameParticipant"]) -> dict[str, bool]:
    """Compute per-game mechanic activation/capability for index analytics."""
    monarch_activated = False
    poison_activated = False
    monarch_capable_present = False
    poison_capable_present = False

    for participant in parts:
        flags = parse_participant_flags(getattr(participant, "flags_json", None))

        if flags.get("monarch") is True:
            monarch_activated = True

        poison_raw = flags.get("poison", 0)
        poison_count = poison_raw if isinstance(poison_raw, int) and not isinstance(poison_raw, bool) else 0
        if poison_count > 0:
            poison_activated = True

        mechanics = getattr(participant, "deck_mechanics", None)
        if not isinstance(mechanics, dict):
            mechanics = derive_deck_mechanics({})

        if mechanics.get("monarch"):
            monarch_capable_present = True
        if mechanics.get("poison"):
            poison_capable_present = True

    return {
        "monarch_activated": monarch_activated,
        "poison_activated": poison_activated,
        "monarch_capable_present": monarch_capable_present,
        "poison_capable_present": poison_capable_present,
    }


def validate_password_rules(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters long."

    if password.lower() in COMMON_WEAK_PASSWORDS:
        return "That password is too common. Please choose a stronger one."

    has_letter = any(char.isalpha() for char in password)
    has_number = any(char.isdigit() for char in password)
    if not has_letter or not has_number:
        return "Password must include at least one letter and one number."

    return None


# -------------------------
# Models
# -------------------------


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(100), unique=True, nullable=False)

    password_hash = db.Column(db.String(128), nullable=False)

    is_active = db.Column(db.Boolean, default=False, nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    use_sigtaara = db.Column(db.Boolean, default=False, nullable=False)
    use_light_theme = db.Column(db.Boolean, default=False, nullable=False)
    mana_fucked_salt_value = db.Column(db.Integer, nullable=False, default=1)
    misplayed_salt_value = db.Column(db.Integer, nullable=False, default=1)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)

    player = db.relationship("Player", backref="user", uselist=False)


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Stats display name (mirrors user.display_name for accounts)
    name = db.Column(db.String(100), unique=True, nullable=False)

    # Nullable to allow guest/manual players
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=True)

    decks = db.relationship("Deck", backref="owner", lazy=True)


class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    retired = db.Column(db.Boolean, nullable=False, default=False)
    planned = db.Column(db.Boolean, nullable=False, default=False)

    # legacy / user-entered fallback
    commander = db.Column(db.String(100), nullable=False)

    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False, index=True)

    # Robust commander support (best-effort filled via Scryfall)
    commander_name = db.Column(db.String(120))
    commander_scryfall_id = db.Column(db.String(40), index=True)
    commander_art_crop_url = db.Column(db.String(300))
    commander_local_art_crop = db.Column(db.String(300))
    commander_local_art_custom = db.Column(db.String(300))
    custom_commander_art_url = db.Column(db.String(500))
    custom_card_art_url = db.Column(db.String(500))
    custom_card_art_local = db.Column(db.String(300))
    color_identity = db.Column(db.String(10))  # e.g. "WUBRG"
    decklist_text = db.Column(db.Text)
    tags_json = db.Column(db.Text, nullable=False, default="{}")
    tags_version = db.Column(db.Integer, nullable=True)
    tags_computed_at = db.Column(db.DateTime, nullable=True)

    @property
    def commander_art_url(self):
        return (
            self.commander_local_art_custom
            or self.custom_commander_art_url
            or self.commander_local_art_crop
            or self.commander_art_crop_url
        )

    @property
    def card_art_url(self):
        return self.custom_card_art_local or self.custom_card_art_url

    @property
    def card_art(self):
        return self.card_art_url

    @property
    def commander_art_scale(self):
        # Uploaded/linked custom art is usually full-card framing. Slightly zoom it so
        # life-counter and deck tiles keep a useful crop without letterboxing.
        if self.commander_local_art_custom or self.custom_commander_art_url or self.custom_card_art_local or self.custom_card_art_url:
            return "118%"
        return "cover"


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)

    winner_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False, index=True)
    winner = db.relationship(
        "Player",
        foreign_keys="Game.winner_id",
        backref="won_games",
        lazy=True,
    )

    starting_player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=True)
    starting_player = db.relationship(
        "Player",
        foreign_keys="Game.starting_player_id",
        lazy=True,
    )

    salt_rating = db.Column(db.Integer, nullable=True)  # 1..5
    win_type = db.Column(db.String(32), nullable=True)
    timed_mode = db.Column(db.String(32), nullable=True)
    time_control = db.Column(db.Text, nullable=True)
    ended_on_time = db.Column(db.Boolean, nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)
    ending_turn = db.Column(db.Integer, nullable=True)

    note = db.Column(db.Text, nullable=True)

    pod_id = db.Column(db.Integer, db.ForeignKey("pod.id"), nullable=True, index=True)
    pod = db.relationship("Pod", backref="games", lazy=True)


class Pod(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    slug = db.Column(db.String(120), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PodMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pod_id = db.Column(db.Integer, db.ForeignKey("pod.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default="member")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    pod = db.relationship("Pod", backref="memberships", lazy=True)
    player = db.relationship("Player", backref="pod_memberships", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("pod_id", "player_id", name="uq_pod_membership_pod_player"),
    )


class RegistrationRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    requested_pod_id = db.Column(db.Integer, db.ForeignKey("pod.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("registration_request", uselist=False))
    requested_pod = db.relationship("Pod", foreign_keys=[requested_pod_id], lazy=True)
    reviewed_by_user = db.relationship("User", foreign_keys=[reviewed_by_user_id], lazy=True)


class GameParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False, index=True)
    deck_id = db.Column(db.Integer, db.ForeignKey("deck.id"), nullable=False, index=True)
    seat_position = db.Column(db.Integer, nullable=True)
    flags_json = db.Column(db.Text, nullable=True)
    salt_count = db.Column(db.Integer, nullable=False, default=0)
    mana_fucked = db.Column(db.Boolean, nullable=False, default=False)
    misplayed = db.Column(db.Boolean, nullable=False, default=False)
    life_delta_total = db.Column(db.Integer, nullable=True, default=0)

    player = db.relationship("Player", backref="participations", lazy=True)
    deck = db.relationship("Deck", backref="deck_participations", lazy=True)
    game = db.relationship("Game", backref="participants", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("game_id", "player_id", name="unique_player_per_game"),
        db.UniqueConstraint("game_id", "seat_position", name="unique_seat_per_game"),
    )


class ActiveGame(db.Model):
    __tablename__ = "active_game"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(32), unique=True, nullable=False, index=True)
    host_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    pod_id = db.Column(db.Integer, db.ForeignKey("pod.id"), nullable=True)
    participants_json = db.Column(db.Text, nullable=False)
    state_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)


# -------------------------
# Dev bootstrap helpers
# -------------------------


def bootstrap_test_user():
    """
    Dev-only (env-gated):
      - Ensure a test user exists and is active.
      - Ensure linked Player exists.
      - Ensure membership in default pod (role podmaster if TEST_IS_ADMIN).
    Returns the User or None.
    """
    if not BOOTSTRAP_TEST_USER:
        return None

    u = User.query.filter_by(username=TEST_USERNAME).first()
    role = "podmaster" if TEST_IS_ADMIN else "member"

    if u:
        changed = False

        if not u.is_active:
            u.is_active = True
            u.approved_at = datetime.utcnow()
            changed = True

        if TEST_IS_ADMIN and not u.is_admin:
            u.is_admin = True
            changed = True

        if not u.player:
            u.player = Player(name=u.display_name)
            changed = True

        default_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG).first()
        if default_pod and u.player:
            m = PodMembership.query.filter_by(pod_id=default_pod.id, player_id=u.player.id).first()
            if not m:
                db.session.add(PodMembership(pod_id=default_pod.id, player_id=u.player.id, role=role))
                changed = True
            elif role == "podmaster" and m.role != "podmaster":
                m.role = "podmaster"
                changed = True

        if changed:
            db.session.commit()
        return u

    # Create new user (avoid display_name collisions)
    display = (TEST_DISPLAY_NAME or "").strip() or TEST_USERNAME
    if User.query.filter_by(display_name=display).first() or Player.query.filter_by(name=display).first():
        display = f"{display} ({TEST_USERNAME})"

    u = User(
        username=TEST_USERNAME,
        display_name=display,
        password_hash=generate_password_hash(TEST_PASSWORD),
        is_active=True,
        is_admin=bool(TEST_IS_ADMIN),
        approved_at=datetime.utcnow(),
    )
    u.player = Player(name=u.display_name)

    db.session.add(u)
    db.session.flush()

    default_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG).first()
    if default_pod and u.player:
        db.session.add(PodMembership(pod_id=default_pod.id, player_id=u.player.id, role=role))

    db.session.commit()
    return u


# -------------------------
# App bootstrap
# -------------------------
with app.app_context():
    def run_schema_migrations():
        existing_tables = {
            row[0]
            for row in db.session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }

        if "game" not in existing_tables:
            db.create_all()
            existing_tables = {
                row[0]
                for row in db.session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }

        db.session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )

        applied = {
            row[0] for row in db.session.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }

        if "001_pods" not in applied:
            db.session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS pod (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL UNIQUE,
                        slug VARCHAR(120) NOT NULL UNIQUE,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_pod_slug ON pod (slug)"))
            db.session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS pod_membership (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pod_id INTEGER NOT NULL,
                        player_id INTEGER NOT NULL,
                        role VARCHAR(20) NOT NULL DEFAULT 'member',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(pod_id) REFERENCES pod (id),
                        FOREIGN KEY(player_id) REFERENCES player (id),
                        CONSTRAINT uq_pod_membership_pod_player UNIQUE (pod_id, player_id)
                    )
                    """
                )
            )

            cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game)")).fetchall()
            }
            if "pod_id" not in cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN pod_id INTEGER"))
                db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_game_pod_id ON game (pod_id)"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('001_pods')")
            )

        if "002_game_timer_metadata" not in applied:
            cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game)")).fetchall()
            }

            if "timed_mode" not in cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN timed_mode VARCHAR(32)"))
            if "time_control" not in cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN time_control TEXT"))
            if "ended_on_time" not in cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN ended_on_time BOOLEAN"))
            if "duration_seconds" not in cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN duration_seconds INTEGER"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('002_game_timer_metadata')")
            )

        if "003_user_sigtaara_preference" not in applied:
            user_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()
            }
            if "use_sigtaara" not in user_cols:
                db.session.execute(
                    text("ALTER TABLE user ADD COLUMN use_sigtaara BOOLEAN NOT NULL DEFAULT 0")
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('003_user_sigtaara_preference')")
            )

        if "004_deck_decklist_text" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "decklist_text" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN decklist_text TEXT"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('004_deck_decklist_text')")
            )

        if "005_game_participant_flags" not in applied:
            participant_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game_participant)")).fetchall()
            }
            if "flags_json" not in participant_cols:
                db.session.execute(text("ALTER TABLE game_participant ADD COLUMN flags_json TEXT"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('005_game_participant_flags')")
            )

        if "006_deck_planned_status" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "planned" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN planned BOOLEAN NOT NULL DEFAULT 0"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('006_deck_planned_status')")
            )

        if "007_game_ending_turn" not in applied:
            game_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game)")).fetchall()
            }
            if "ending_turn" not in game_cols:
                db.session.execute(text("ALTER TABLE game ADD COLUMN ending_turn INTEGER"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('007_game_ending_turn')")
            )

        if "008_user_salt_action_values" not in applied:
            user_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()
            }
            if "mana_fucked_salt_value" not in user_cols:
                db.session.execute(
                    text("ALTER TABLE user ADD COLUMN mana_fucked_salt_value INTEGER NOT NULL DEFAULT 1")
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('008_user_salt_action_values')")
            )

        if "009_user_misplayed_salt_value" not in applied:
            user_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()
            }
            if "misplayed_salt_value" not in user_cols:
                db.session.execute(
                    text("ALTER TABLE user ADD COLUMN misplayed_salt_value INTEGER NOT NULL DEFAULT 1")
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('009_user_misplayed_salt_value')")
            )

        if "010_game_participant_seat_position" not in applied:
            participant_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game_participant)")).fetchall()
            }
            if "seat_position" not in participant_cols:
                db.session.execute(text("ALTER TABLE game_participant ADD COLUMN seat_position INTEGER"))

            db.session.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_game_participant_game_id_seat_position ON game_participant (game_id, seat_position) WHERE seat_position IS NOT NULL"
                )
            )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('010_game_participant_seat_position')")
            )

        if "010_game_participant_hot_fields" not in applied:
            participant_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game_participant)")).fetchall()
            }
            if "salt_count" not in participant_cols:
                db.session.execute(
                    text("ALTER TABLE game_participant ADD COLUMN salt_count INTEGER NOT NULL DEFAULT 0")
                )
            if "mana_fucked" not in participant_cols:
                db.session.execute(
                    text("ALTER TABLE game_participant ADD COLUMN mana_fucked BOOLEAN NOT NULL DEFAULT 0")
                )
            if "misplayed" not in participant_cols:
                db.session.execute(
                    text("ALTER TABLE game_participant ADD COLUMN misplayed BOOLEAN NOT NULL DEFAULT 0")
                )
            if "life_delta_total" not in participant_cols:
                db.session.execute(
                    text("ALTER TABLE game_participant ADD COLUMN life_delta_total INTEGER DEFAULT 0")
                )

            participant_rows = db.session.execute(
                text("SELECT id, flags_json FROM game_participant")
            ).fetchall()
            for row in participant_rows:
                hot_fields = participant_hot_fields_from_flags(row.flags_json)
                db.session.execute(
                    text(
                        """
                        UPDATE game_participant
                        SET salt_count = :salt_count,
                            mana_fucked = :mana_fucked,
                            misplayed = :misplayed,
                            life_delta_total = :life_delta_total
                        WHERE id = :participant_id
                        """
                    ),
                    {
                        "participant_id": row.id,
                        "salt_count": int(hot_fields["salt_count"]),
                        "mana_fucked": 1 if hot_fields["mana_fucked"] else 0,
                        "misplayed": 1 if hot_fields["misplayed"] else 0,
                        "life_delta_total": int(hot_fields["life_delta_total"]),
                    },
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('010_game_participant_hot_fields')")
            )


        if "011_deck_custom_art_urls" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "custom_commander_art_url" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN custom_commander_art_url VARCHAR(500)"))
            if "custom_card_art_url" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN custom_card_art_url VARCHAR(500)"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('011_deck_custom_art_urls')")
            )

        if "013_deck_local_art_split" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "commander_local_art_crop" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN commander_local_art_crop VARCHAR(300)"))
            if "commander_local_art_custom" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN commander_local_art_custom VARCHAR(300)"))

            if "commander_local_art" in deck_cols:
                db.session.execute(
                    text(
                        "UPDATE deck SET commander_local_art_crop = commander_local_art "
                        "WHERE commander_local_art_crop IS NULL AND commander_local_art IS NOT NULL"
                    )
                )

            db.session.execute(
                text(
                    "UPDATE deck "
                    "SET commander_local_art_custom = custom_commander_art_url, custom_commander_art_url = NULL "
                    "WHERE commander_local_art_custom IS NULL "
                    "AND custom_commander_art_url LIKE '/art/%'"
                )
            )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('013_deck_local_art_split')")
            )

        if "014_deck_custom_card_local_art" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "custom_card_art_local" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN custom_card_art_local VARCHAR(300)"))

            db.session.execute(
                text(
                    "UPDATE deck "
                    "SET custom_card_art_local = custom_card_art_url, custom_card_art_url = NULL "
                    "WHERE custom_card_art_local IS NULL "
                    "AND custom_card_art_url LIKE '/art/%'"
                )
            )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('014_deck_custom_card_local_art')")
            )

        if "015_deck_tags_json" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "tags_json" not in deck_cols:
                db.session.execute(
                    text("ALTER TABLE deck ADD COLUMN tags_json TEXT NOT NULL DEFAULT '{}'"))

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('015_deck_tags_json')")
            )

        if "011_game_result_canonical_values" not in applied:
            game_rows = db.session.execute(text("SELECT id, win_type, timed_mode FROM game")).fetchall()
            for row in game_rows:
                canonical_win_type = canonicalize_win_type(row.win_type, unknown_default="other")
                canonical_timed_mode = canonicalize_timed_mode(row.timed_mode)
                if canonical_win_type != row.win_type or canonical_timed_mode != row.timed_mode:
                    db.session.execute(
                        text(
                            """
                            UPDATE game
                            SET win_type = :win_type,
                                timed_mode = :timed_mode
                            WHERE id = :game_id
                            """
                        ),
                        {
                            "game_id": row.id,
                            "win_type": canonical_win_type,
                            "timed_mode": canonical_timed_mode,
                        },
                    )

            db.session.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ck_game_win_type_insert
                    BEFORE INSERT ON game
                    FOR EACH ROW
                    WHEN NEW.win_type IS NOT NULL
                         AND NEW.win_type NOT IN ('combat','combo','alt_win','concede','time','lock','other')
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid win_type');
                    END;
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ck_game_win_type_update
                    BEFORE UPDATE OF win_type ON game
                    FOR EACH ROW
                    WHEN NEW.win_type IS NOT NULL
                         AND NEW.win_type NOT IN ('combat','combo','alt_win','concede','time','lock','other')
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid win_type');
                    END;
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ck_game_timed_mode_insert
                    BEFORE INSERT ON game
                    FOR EACH ROW
                    WHEN NEW.timed_mode IS NOT NULL
                         AND NEW.timed_mode NOT IN ('off','chess_clock','turn_timer')
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid timed_mode');
                    END;
                    """
                )
            )
            db.session.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS ck_game_timed_mode_update
                    BEFORE UPDATE OF timed_mode ON game
                    FOR EACH ROW
                    WHEN NEW.timed_mode IS NOT NULL
                         AND NEW.timed_mode NOT IN ('off','chess_clock','turn_timer')
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid timed_mode');
                    END;
                    """
                )
            )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('011_game_result_canonical_values')")
            )

        if "012_game_and_participant_query_indexes" not in applied:
            db.session.execute(
                text("CREATE INDEX IF NOT EXISTS ix_game_pod_id_date ON game (pod_id, date)")
            )
            db.session.execute(
                text("CREATE INDEX IF NOT EXISTS ix_game_winner_id_date ON game (winner_id, date)")
            )
            db.session.execute(
                text("CREATE INDEX IF NOT EXISTS ix_game_starting_player_id ON game (starting_player_id)")
            )

            db.session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_game_participant_game_id_player_id "
                    "ON game_participant (game_id, player_id)"
                )
            )
            db.session.execute(
                text("CREATE INDEX IF NOT EXISTS ix_game_participant_deck_id ON game_participant (deck_id)")
            )
            db.session.execute(
                text("CREATE INDEX IF NOT EXISTS ix_game_participant_player_id ON game_participant (player_id)")
            )

            participant_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(game_participant)")).fetchall()
            }
            if "salt_count" in participant_cols:
                db.session.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_game_participant_salt_count ON game_participant (salt_count)")
                )
            if "mana_fucked" in participant_cols:
                db.session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_game_participant_mana_fucked_true "
                        "ON game_participant (game_id, player_id) WHERE mana_fucked = 1"
                    )
                )
            if "misplayed" in participant_cols:
                db.session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_game_participant_misplayed_true "
                        "ON game_participant (game_id, player_id) WHERE misplayed = 1"
                    )
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('012_game_and_participant_query_indexes')")
            )

        if "017_deck_tags_versioning" not in applied:
            deck_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(deck)")).fetchall()
            }
            if "tags_version" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN tags_version INTEGER"))
            if "tags_computed_at" not in deck_cols:
                db.session.execute(text("ALTER TABLE deck ADD COLUMN tags_computed_at DATETIME"))

            if TRUST_LEGACY_DECK_TAGS:
                db.session.execute(
                    text(
                        """
                        UPDATE deck
                        SET tags_version = 1
                        WHERE tags_version IS NULL
                          AND tags_json IS NOT NULL
                          AND TRIM(tags_json) != ''
                          AND TRIM(tags_json) != '{}'
                        """
                    )
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('017_deck_tags_versioning')")
            )

        if "016_registration_requests" not in applied:
            db.session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS registration_request (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL UNIQUE,
                        requested_pod_id INTEGER NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        reviewed_at DATETIME,
                        reviewed_by_user_id INTEGER,
                        FOREIGN KEY(user_id) REFERENCES user (id),
                        FOREIGN KEY(requested_pod_id) REFERENCES pod (id),
                        FOREIGN KEY(reviewed_by_user_id) REFERENCES user (id)
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_registration_request_requested_pod_id "
                    "ON registration_request (requested_pod_id)"
                )
            )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('016_registration_requests')")
            )

        if "018_active_game_table" not in applied:
            db.session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS active_game (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token VARCHAR(32) NOT NULL UNIQUE,
                        host_user_id INTEGER NOT NULL,
                        pod_id INTEGER,
                        participants_json TEXT NOT NULL,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(host_user_id) REFERENCES user (id),
                        FOREIGN KEY(pod_id) REFERENCES pod (id)
                    )
                    """
                )
            )
            db.session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_active_game_token ON active_game (token)"
                )
            )
            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('018_active_game_table')")
            )

        if "019_user_light_theme_preference" not in applied:
            user_cols = {
                row[1] for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()
            }
            if "use_light_theme" not in user_cols:
                db.session.execute(
                    text("ALTER TABLE user ADD COLUMN use_light_theme BOOLEAN NOT NULL DEFAULT 0")
                )

            db.session.execute(
                text("INSERT INTO schema_migrations(version) VALUES ('019_user_light_theme_preference')")
            )

        default_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG).first()
        if not default_pod:
            default_pod = Pod(name=DEFAULT_POD_NAME, slug=DEFAULT_POD_SLUG, is_active=True)
            db.session.add(default_pod)
            db.session.flush()

        db.session.execute(
            text("UPDATE game SET pod_id = :pod_id WHERE pod_id IS NULL"),
            {"pod_id": default_pod.id},
        )

        db.session.execute(
            text(
                """
                INSERT OR IGNORE INTO pod_membership (pod_id, player_id, role)
                SELECT :pod_id, player.id, 'member' FROM player
                """
            ),
            {"pod_id": default_pod.id},
        )
        db.session.commit()

    run_schema_migrations()

    def ensure_indexes():
        stmts = [
            "CREATE INDEX IF NOT EXISTS ix_gameparticipant_player_id ON game_participant (player_id)",
            "CREATE INDEX IF NOT EXISTS ix_gameparticipant_deck_id   ON game_participant (deck_id)",
            "CREATE INDEX IF NOT EXISTS ix_deck_player_id            ON deck (player_id)",
            "CREATE INDEX IF NOT EXISTS ix_game_winner_id            ON game (winner_id)",
            "CREATE INDEX IF NOT EXISTS ix_podmembership_player_id   ON pod_membership (player_id)",
        ]
        with db.engine.connect() as conn:
            for s in stmts:
                conn.execute(text(s))
            conn.commit()

    ensure_indexes()

    if os.getenv("AUTO_CREATE_DB") == "1":
        db.create_all()

    # Dev-only: create/ensure test user during bootstrap
    bootstrap_test_user()

    # Bootstrap admin (env var BOOTSTRAP_ADMIN_USERNAME)
    admin_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME")
    if admin_username:
        u = User.query.filter_by(username=admin_username).first()
        if u:
            changed = False
            if not u.is_admin:
                u.is_admin = True
                changed = True
            if not u.is_active:
                u.is_active = True
                u.approved_at = datetime.utcnow()
                changed = True
            if not u.display_name:
                u.display_name = u.username
                changed = True

            if changed:
                db.session.commit()
                print(f"[bootstrap_admin] Promoted '{admin_username}' to admin and activated account.")
        else:
            print(f"[bootstrap_admin] No user '{admin_username}' found (yet).")


# -------------------------
# Scryfall helper functions
# -------------------------


def _safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("_")


def _card_art_cache_key(card_name: str) -> str:
    return (card_name or "").strip().lower()


def _load_json_index(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json_index(path: Path, payload: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _initialize_card_art_indexes() -> None:
    global CARD_ART_NAME_INDEX, CARD_ART_FAILURE_INDEX
    with CARD_ART_CACHE_LOCK:
        CARD_ART_NAME_INDEX = {
            str(k): str(v)
            for k, v in _load_json_index(CARD_ART_INDEX_FILE).items()
            if isinstance(k, str) and isinstance(v, str) and v.startswith("/art/card_art/")
        }
        loaded_failures = _load_json_index(CARD_ART_FAILURE_FILE)
        cleaned: dict[str, dict[str, float | int | str]] = {}
        now = time.time()
        for key, value in loaded_failures.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            reason = value.get("reason")
            status = value.get("status")
            expires_at = value.get("expires_at")
            if not isinstance(reason, str) or not isinstance(status, int):
                continue
            if not isinstance(expires_at, (int, float)) or expires_at <= now:
                continue
            cleaned[key] = {"reason": reason, "status": status, "expires_at": float(expires_at)}
        CARD_ART_FAILURE_INDEX = cleaned
        _save_json_index(CARD_ART_FAILURE_FILE, CARD_ART_FAILURE_INDEX)


def _get_card_art_web_path_from_index(card_name: str) -> str | None:
    key = _card_art_cache_key(card_name)
    if not key:
        return None
    with CARD_ART_CACHE_LOCK:
        cached_path = CARD_ART_NAME_INDEX.get(key)
    if not cached_path:
        return None
    disk_path = ART_DIR / cached_path.removeprefix("/art/")
    if disk_path.exists() and disk_path.stat().st_size > 0:
        return cached_path
    with CARD_ART_CACHE_LOCK:
        CARD_ART_NAME_INDEX.pop(key, None)
        _save_json_index(CARD_ART_INDEX_FILE, CARD_ART_NAME_INDEX)
    return None


def _set_card_art_web_path_index(card_name: str, web_path: str) -> None:
    key = _card_art_cache_key(card_name)
    if not key or not web_path:
        return
    with CARD_ART_CACHE_LOCK:
        CARD_ART_NAME_INDEX[key] = web_path
        _save_json_index(CARD_ART_INDEX_FILE, CARD_ART_NAME_INDEX)


def _failure_ttl_seconds(reason: str) -> int:
    if reason == "upstream_rate_limited":
        return 90
    if reason in ("upstream_timeout", "upstream_failure"):
        return 300
    if reason == "not_found":
        return 21600
    return 300


def _set_card_art_failure_index(card_name: str, reason: str, status: int) -> None:
    key = _card_art_cache_key(card_name)
    if not key or not reason:
        return
    payload = {
        "reason": reason,
        "status": int(status),
        "expires_at": float(time.time() + _failure_ttl_seconds(reason)),
    }
    with CARD_ART_CACHE_LOCK:
        CARD_ART_FAILURE_INDEX[key] = payload
        _save_json_index(CARD_ART_FAILURE_FILE, CARD_ART_FAILURE_INDEX)


def _pop_card_art_failure_index(card_name: str) -> tuple[str | None, int] | None:
    key = _card_art_cache_key(card_name)
    if not key:
        return None
    now = time.time()
    with CARD_ART_CACHE_LOCK:
        payload = CARD_ART_FAILURE_INDEX.get(key)
        if not payload:
            return None
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, (int, float)) or float(expires_at) <= now:
            CARD_ART_FAILURE_INDEX.pop(key, None)
            _save_json_index(CARD_ART_FAILURE_FILE, CARD_ART_FAILURE_INDEX)
            return None
        reason = payload.get("reason")
        status = payload.get("status")
        if not isinstance(reason, str) or not isinstance(status, int):
            CARD_ART_FAILURE_INDEX.pop(key, None)
            _save_json_index(CARD_ART_FAILURE_FILE, CARD_ART_FAILURE_INDEX)
            return None
        return reason, status


def _clear_card_art_failure_index(card_name: str) -> None:
    key = _card_art_cache_key(card_name)
    if not key:
        return
    with CARD_ART_CACHE_LOCK:
        if key in CARD_ART_FAILURE_INDEX:
            CARD_ART_FAILURE_INDEX.pop(key, None)
            _save_json_index(CARD_ART_FAILURE_FILE, CARD_ART_FAILURE_INDEX)


_initialize_card_art_indexes()


def is_valid_custom_art_url(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return True
    if not candidate.lower().startswith(("http://", "https://")):
        return False
    return len(candidate) <= 500


def has_uploaded_custom_art(upload) -> bool:
    return bool(upload and (upload.filename or "").strip())


def normalized_custom_art_url(value: str) -> str | None:
    candidate = (value or "").strip()
    return candidate or None


def _store_custom_art_upload(upload, field_label: str) -> str | None:
    if not upload or not upload.filename:
        return None

    filename = upload.filename.strip()
    ext = Path(filename).suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
    if ext not in allowed:
        raise DeckParserError(
            f"Unsupported {field_label} image '{filename}'. Allowed extensions: "
            ".png, .jpg, .jpeg, .webp, .gif, .avif."
        )

    raw_bytes = upload.read()
    if not raw_bytes:
        raise DeckParserError(f"Uploaded {field_label} image '{filename}' was empty.")

    safe_name = _safe_filename(Path(filename).stem) or "custom_art"
    out_filename = f"custom_{safe_name}_{uuid4().hex}{ext}"
    out_path = ART_DIR / out_filename
    out_path.write_bytes(raw_bytes)
    return f"/art/{out_filename}"


def resolve_custom_art_value(url_value: str, upload, field_label: str) -> tuple[str | None, str | None]:
    uploaded_path = _store_custom_art_upload(upload, field_label)
    if uploaded_path:
        return None, uploaded_path
    return normalized_custom_art_url(url_value), None


def _color_identity_from_mana(mana: str) -> str:
    """Extract WUBRG color identity from a cc-auto mana string like '6WR' or '3UB'."""
    return "".join(c for c in "WUBRG" if c in (mana or "").upper())


def scryfall_named_exact(name: str):
    """Best-effort exact-name lookup. Returns dict or None."""
    if not name:
        return None
    url = f"https://api.scryfall.com/cards/named?exact={quote(name)}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


def custommtg_gallery_named_exact(name: str) -> dict | None:
    """Look up a card by exact name in the custom MTG gallery. Returns card dict or None."""
    if not CCAUTO_BASE_URL or not name:
        return None
    name_lower = name.lower().strip()
    try:
        r = requests.get(
            f"{CCAUTO_BASE_URL}/api/cards/search?q={quote(name_lower)}",
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for card in data.get("data") or []:
            if isinstance(card, dict) and (card.get("name") or "").lower().strip() == name_lower:
                return card
    except (requests.RequestException, ValueError):
        return None
    return None


def _rewrite_gallery_image_uris(card: dict) -> dict:
    """Rewrite relative gallery image paths in image_uris to proxy URLs."""
    if not card:
        return card
    uris = card.get("image_uris")
    if not isinstance(uris, dict):
        return card
    rewritten = {}
    for key, val in uris.items():
        if isinstance(val, str) and val.startswith("/api/sets/"):
            rewritten[key] = f"/api/gallery-image?path={quote(val)}"
        else:
            rewritten[key] = val
    return {**card, "image_uris": rewritten}


def extract_art_crop(card: dict):
    """Returns art_crop URL or None. Handles DFC cards (card_faces)."""
    if not card:
        return None
    if card.get("image_uris") and card["image_uris"].get("art_crop"):
        return card["image_uris"]["art_crop"]
    faces = card.get("card_faces") or []
    if faces and faces[0].get("image_uris") and faces[0]["image_uris"].get("art_crop"):
        return faces[0]["image_uris"]["art_crop"]
    return None


def extract_oracle_text(card_json: dict) -> str:
    """Collects oracle text from single- and multi-face cards."""
    if not card_json:
        return ""

    chunks: list[str] = []
    oracle_text = card_json.get("oracle_text")
    if oracle_text:
        chunks.append(str(oracle_text))

    for face in card_json.get("card_faces") or []:
        face_text = face.get("oracle_text")
        if face_text:
            chunks.append(str(face_text))

    return "\n".join(chunks).lower()


def analyze_scryfall_card(card_json: dict) -> dict:
    oracle_text = extract_oracle_text(card_json)
    return {
        "monarch": "monarch" in oracle_text,
        "initiative": "initiative" in oracle_text,
        "citys_blessing": ("city's blessing" in oracle_text) or ("cityâ€™s blessing" in oracle_text),
        "poison": "poison" in oracle_text,
        "proliferate": "proliferate" in oracle_text,
        "energy": "{e}" in oracle_text,
        "experience": "experience counter" in oracle_text,
        "mana_fucked": "mana fucked" in oracle_text or "mana-fucked" in oracle_text,
        "misplayed": "misplayed" in oracle_text,
    }




def compute_commander_bracket(decklist_cards: list[str]) -> dict:
    """Heuristic Commander bracket estimate from card names only.

    Returns a dict with `bracket` (1..5), `score`, and feature counts.
    """
    unique_cards = {(name or "").strip().lower() for name in (decklist_cards or []) if (name or "").strip()}

    fast_mana_hits = sorted(name for name in unique_cards if name in COMMANDER_BRACKET_FAST_MANA)
    tutor_hits = sorted(name for name in unique_cards if name in COMMANDER_BRACKET_TUTORS)
    combo_hits = sorted(name for name in unique_cards if name in COMMANDER_BRACKET_CEDH_COMBOS)

    score = (len(fast_mana_hits) * 2) + len(tutor_hits) + (len(combo_hits) * 2)

    if len(combo_hits) >= 2 and len(fast_mana_hits) >= 1:
        bracket = 5
    elif score >= 7:
        bracket = 5
    elif score >= 4:
        bracket = 4
    elif score >= 2:
        bracket = 3
    else:
        bracket = 2 if unique_cards else 1

    return {
        "bracket": bracket,
        "score": score,
        "signals": {
            "fast_mana_count": len(fast_mana_hits),
            "tutor_count": len(tutor_hits),
            "combo_piece_count": len(combo_hits),
        },
        "matched_cards": {
            "fast_mana": fast_mana_hits,
            "tutors": tutor_hits,
            "combo_pieces": combo_hits,
        },
    }


def compute_commander_bracket_from_text(decklist_text: str | None) -> dict:
    raw_text = (decklist_text or "").strip()
    if not raw_text:
        return {"bracket": 1, "score": 0, "signals": {"fast_mana_count": 0, "tutor_count": 0, "combo_piece_count": 0}, "matched_cards": {"fast_mana": [], "tutors": [], "combo_pieces": []}}

    parsed = parse_plaintext_decklist(raw_text).as_dict()
    return compute_commander_bracket(extract_decklist_card_names(parsed))
def compute_deck_tags(decklist_cards: list[str]) -> tuple[dict, dict]:
    tags = {key: False for key in KNOWN_DECK_TAG_KEYS}
    seen_names: set[str] = set()
    unresolved_cards: list[str] = []

    for raw_name in decklist_cards:
        name = (raw_name or "").strip()
        if not name:
            continue

        lowered = name.lower()
        if lowered in seen_names:
            continue
        seen_names.add(lowered)

        try:
            card_json = scryfall_named_exact(name)
            if not card_json and CCAUTO_BASE_URL:
                card_json = custommtg_gallery_named_exact(name)
            if not card_json:
                unresolved_cards.append(name)
                continue

            card_tags = analyze_scryfall_card(card_json)
        except Exception:
            unresolved_cards.append(name)
            continue

        for key in tags:
            tags[key] = tags[key] or card_tags.get(key, False)

    diagnostics = {
        "unresolved_count": len(unresolved_cards),
        "unresolved_cards": unresolved_cards,
    }
    return tags, diagnostics


def extract_decklist_card_names(parsed: dict) -> list[str]:
    parsed_sections = parsed.get("sections", {}) or {}
    return [
        entry.get("name", "")
        for entries in parsed_sections.values()
        for entry in (entries or [])
    ]


def compute_deck_tags_from_text(decklist_text: str | None) -> tuple[dict, dict]:
    raw_text = (decklist_text or "").strip()
    if not raw_text:
        return {}, {"unresolved_count": 0, "unresolved_cards": []}

    parsed = parse_plaintext_decklist(raw_text).as_dict()
    return compute_deck_tags(extract_decklist_card_names(parsed))


def flash_unresolved_tag_warning(tag_diagnostics: dict | None) -> None:
    if not isinstance(tag_diagnostics, dict):
        return

    unresolved_cards = [str(name) for name in (tag_diagnostics.get("unresolved_cards") or []) if str(name).strip()]
    unresolved_count = int(tag_diagnostics.get("unresolved_count") or len(unresolved_cards))
    if unresolved_count <= 0:
        return

    preview = ", ".join(unresolved_cards[:5])
    extra = unresolved_count - min(len(unresolved_cards), 5)
    suffix = f", +{extra} more" if extra > 0 else ""
    details = f": {preview}{suffix}" if preview else ""
    flash(f"Deck tags were partially computed; {unresolved_count} card lookup(s) could not be resolved{details}.")


def parse_tags_json(raw: str | None) -> dict[str, bool]:
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    return {
        key: bool(parsed.get(key))
        for key in KNOWN_DECK_TAG_KEYS
        if key in parsed
    }


def get_deck_parsed_tags(deck: "Deck" | None, cache: dict[int, dict[str, bool]] | None = None) -> dict[str, bool]:
    if not deck:
        return {}

    deck_id = getattr(deck, "id", None)
    if cache is not None and deck_id is not None and deck_id in cache:
        return cache[deck_id]

    parsed_tags = getattr(deck, "_parsed_tags", None)
    if not isinstance(parsed_tags, dict):
        parsed_tags = parse_tags_json(deck.tags_json)
        deck._parsed_tags = parsed_tags

    if cache is not None and deck_id is not None:
        cache[deck_id] = parsed_tags

    return parsed_tags


def derive_deck_mechanics(tags: dict) -> dict:
    """Derive gameplay mechanics from deck tags; poison also keys off proliferate for consistency."""
    tags = tags if isinstance(tags, dict) else {}
    return {
        "monarch": bool(tags.get("monarch")),
        "initiative": bool(tags.get("initiative")),
        "citys_blessing": bool(tags.get("citys_blessing")),
        "poison": bool(tags.get("poison")) or bool(tags.get("proliferate")),
        "energy": bool(tags.get("energy")),
        "experience": bool(tags.get("experience")),
    }


def apply_deck_tags(deck: "Deck", tags: dict) -> None:
    deck.tags_json = json.dumps(tags, separators=(",", ":"), sort_keys=True)
    deck.tags_version = DECK_TAGS_VERSION
    deck.tags_computed_at = datetime.utcnow()


def is_deck_tags_stale(deck: "Deck") -> bool:
    return (deck.tags_version is None) or (deck.tags_version < DECK_TAGS_VERSION)


def _split_commander_names(value: str) -> list[str]:
    if not value:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for part in value.split("+"):
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def resolve_commander_metadata(commander_input: str) -> dict:
    commander_names = _split_commander_names(commander_input)
    if not commander_names and commander_input.strip():
        commander_names = [commander_input.strip()]

    cards = []
    for name in commander_names:
        card = scryfall_named_exact(name)
        if card:
            cards.append(card)

    if not cards:
        # Fallback: check Card Conjurer custom sets for the commander card.
        if CCAUTO_BASE_URL:
            ccauto_cards = [ccauto_named_exact(n) for n in commander_names]
            ccauto_cards = [c for c in ccauto_cards if c]
            if ccauto_cards:
                combined_color = "".join(
                    "".join(c.get("color_identity") or [])
                    or _color_identity_from_mana(c.get("mana_cost") or c.get("mana", ""))
                    for c in ccauto_cards
                )
                color_identity = "".join(c for c in "WUBRG" if c in combined_color)
                combined_name = " + ".join(
                    c.get("name") or commander_names[i] for i, c in enumerate(ccauto_cards)
                )
                primary = ccauto_cards[0]
                art_crop_path = (primary.get("image_uris") or {}).get("art_crop")
                art_url_internal = f"{CCAUTO_BASE_URL}{art_crop_path}" if art_crop_path else None
                art_url_proxy = f"/api/gallery-image?path={quote(art_crop_path)}" if art_crop_path else None
                card_id = primary.get("id")
                local_art = download_art_crop(art_url_internal, card_id, combined_name) if art_url_internal and card_id else None
                return {
                    "commander": combined_name,
                    "commander_name": combined_name,
                    "commander_scryfall_id": None,
                    "commander_art_crop_url": art_url_proxy,
                    "commander_local_art_crop": local_art,
                    "commander_local_art_custom": None,
                    "color_identity": color_identity,
                    "lookup_ok": True,
                }

        return {
            "commander": " + ".join(commander_names) if commander_names else commander_input.strip(),
            "commander_name": None,
            "commander_scryfall_id": None,
            "commander_art_crop_url": None,
            "commander_local_art_crop": None,
            "commander_local_art_custom": None,
            "color_identity": None,
            "lookup_ok": False,
        }

    canonical_names = [card.get("name") or commander_names[i] for i, card in enumerate(cards)]
    combined_commander = " + ".join(canonical_names)

    primary_card = cards[0]
    scry_id = primary_card.get("id")
    art_crop = extract_art_crop(primary_card)
    local_art = None
    if art_crop and scry_id:
        local_art = download_art_crop(art_crop, scry_id, canonical_names[0])

    color_identity = "".join(
        c for c in "WUBRG" if any(c in (card.get("color_identity") or []) for card in cards)
    )

    return {
        "commander": combined_commander,
        "commander_name": combined_commander,
        "commander_scryfall_id": scry_id,
        "commander_art_crop_url": art_crop,
        "commander_local_art_crop": local_art,
        "commander_local_art_custom": None,
        "color_identity": color_identity,
        "lookup_ok": len(cards) == len(commander_names),
    }


def download_art_crop(art_url: str, scryfall_id: str, commander_name: str) -> str | None:
    """
    Downloads art_crop into /data/art (persistent).
    Returns web path like '/art/<file>.jpg' or None.
    """
    if not (art_url and scryfall_id and commander_name):
        return None

    filename = f"{_safe_filename(commander_name)}_{scryfall_id}.jpg"
    out_path = COMMANDER_ART_DIR / filename
    web_path = f"/art/commander_art/{filename}"

    if out_path.exists() and out_path.stat().st_size > 0:
        return web_path

    try:
        req = Request(
            art_url,
            headers={
                "User-Agent": "CommanderTracker/1.0 (https://edh.figurensohn.de)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=20) as resp:
            data = resp.read()

        if not data:
            print("download_art_crop: empty response", art_url)
            return None

        out_path.write_bytes(data)
        return web_path

    except (HTTPError, URLError) as e:
        print("download_art_crop failed:", e, art_url)
        return None
    except Exception as e:
        print("download_art_crop unexpected error:", e, art_url)
        return None


def extract_normal_image(card: dict) -> str | None:
    if not card:
        return None
    if card.get("image_uris") and card["image_uris"].get("normal"):
        return card["image_uris"]["normal"]
    for face in card.get("card_faces") or []:
        if face.get("image_uris") and face["image_uris"].get("normal"):
            return face["image_uris"]["normal"]
    return None


def _classify_upstream_http_status(status_code: int) -> tuple[str, int]:
    if status_code == 404:
        return "not_found", 404
    if status_code == 429:
        return "upstream_rate_limited", 429
    if status_code in (408, 504):
        return "upstream_timeout", 504
    return "upstream_failure", 502


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(exc).lower()


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        return None


def _get_with_backoff(
    url: str,
    *,
    timeout: int,
    headers: dict | None = None,
    attempts: int = 3,
    base_backoff_seconds: float = 0.8,
) -> requests.Response:
    last_exc: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            if response.status_code != 429 or attempt >= (attempts - 1):
                return response
            retry_after_seconds = _parse_retry_after_seconds(response.headers.get("Retry-After"))
            backoff = retry_after_seconds if retry_after_seconds is not None else (
                base_backoff_seconds * (2 ** attempt) + random.uniform(0.1, 0.6)
            )
            app.logger.warning(
                "upstream 429; backing off %.2fs url=%s attempt=%s/%s",
                backoff,
                url,
                attempt + 1,
                attempts,
            )
            time.sleep(backoff)
        except requests.Timeout as exc:
            last_exc = exc
            if attempt >= (attempts - 1):
                raise
            backoff = base_backoff_seconds * (2 ** attempt) + random.uniform(0.1, 0.4)
            app.logger.warning(
                "upstream timeout; retrying after %.2fs url=%s attempt=%s/%s",
                backoff,
                url,
                attempt + 1,
                attempts,
            )
            time.sleep(backoff)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= (attempts - 1):
                raise
            backoff = base_backoff_seconds * (2 ** attempt) + random.uniform(0.1, 0.4)
            app.logger.warning(
                "upstream request exception; retrying after %.2fs url=%s attempt=%s/%s exception=%s",
                backoff,
                url,
                attempt + 1,
                attempts,
                exc.__class__.__name__,
            )
            time.sleep(backoff)
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry loop exited unexpectedly")


def _lookup_card_for_art(normalized_name: str) -> tuple[dict | None, str | None, int]:
    scryfall_url = f"https://api.scryfall.com/cards/named?exact={quote(normalized_name)}"
    try:
        r = _get_with_backoff(scryfall_url, timeout=10, attempts=4, base_backoff_seconds=0.9)
        if r.status_code == 200:
            return r.json(), None, 200
        reason, http_status = _classify_upstream_http_status(r.status_code)
        app.logger.warning(
            "cache_card_art_by_name lookup failed provider=scryfall url=%s status=%s",
            scryfall_url,
            r.status_code,
        )
        if reason == "not_found" and CCAUTO_BASE_URL:
            app.logger.info(
                "cache_card_art_by_name scryfall miss; trying custom gallery name=%s", normalized_name
            )
        else:
            return None, reason, http_status
    except requests.Timeout as exc:
        app.logger.error(
            "cache_card_art_by_name lookup exception provider=scryfall url=%s exception=%s",
            scryfall_url,
            exc.__class__.__name__,
        )
        return None, "upstream_timeout", 504
    except requests.RequestException as exc:
        app.logger.error(
            "cache_card_art_by_name lookup exception provider=scryfall url=%s exception=%s",
            scryfall_url,
            exc.__class__.__name__,
        )
        return None, "upstream_failure", 502

    if not CCAUTO_BASE_URL:
        return None, "not_found", 404

    gallery_url = f"{CCAUTO_BASE_URL}/api/cards/search?q={quote(normalized_name.lower())}"
    try:
        r = _get_with_backoff(gallery_url, timeout=5, attempts=3, base_backoff_seconds=0.6)
        if r.status_code != 200:
            reason, http_status = _classify_upstream_http_status(r.status_code)
            app.logger.warning(
                "cache_card_art_by_name lookup failed provider=custom_gallery url=%s status=%s",
                gallery_url,
                r.status_code,
            )
            return None, reason, http_status

        data = r.json()
        for gallery_card in data.get("data") or []:
            if isinstance(gallery_card, dict) and (gallery_card.get("name") or "").lower().strip() == normalized_name.lower():
                uris = gallery_card.get("image_uris") or {}
                normal_path = uris.get("normal") or ""
                if normal_path.startswith("/"):
                    normal_path = CCAUTO_BASE_URL + normal_path
                return {**gallery_card, "image_uris": {**uris, "normal": normal_path}}, None, 200
        return None, "not_found", 404
    except requests.Timeout as exc:
        app.logger.error(
            "cache_card_art_by_name lookup exception provider=custom_gallery url=%s exception=%s",
            gallery_url,
            exc.__class__.__name__,
        )
        return None, "upstream_timeout", 504
    except (requests.RequestException, ValueError) as exc:
        app.logger.error(
            "cache_card_art_by_name lookup exception provider=custom_gallery url=%s exception=%s",
            gallery_url,
            exc.__class__.__name__,
        )
        return None, "upstream_failure", 502


def cache_card_art_by_name(card_name: str) -> tuple[str | None, str | None, int]:
    normalized_name = (card_name or "").strip()
    if not normalized_name:
        return None, "not_found", 404

    cached_path = _get_card_art_web_path_from_index(normalized_name)
    if cached_path:
        return cached_path, None, 200

    cached_failure = _pop_card_art_failure_index(normalized_name)
    if cached_failure:
        reason, status = cached_failure
        return None, reason, status

    card, lookup_failure_reason, lookup_failure_status = _lookup_card_for_art(normalized_name)
    if not card:
        reason = lookup_failure_reason or "not_found"
        status = lookup_failure_status or 404
        _set_card_art_failure_index(normalized_name, reason, status)
        return None, reason, status

    image_url = extract_normal_image(card)
    if not image_url:
        _set_card_art_failure_index(normalized_name, "not_found", 404)
        return None, "not_found", 404

    card_id = card.get("id") or _safe_filename(normalized_name)
    safe_name = _safe_filename(card.get("name") or normalized_name) or "card"
    filename = f"{safe_name}_{card_id}.jpg"
    out_path = CARD_ART_DIR / filename
    web_path = f"/art/card_art/{filename}"

    if out_path.exists() and out_path.stat().st_size > 0:
        _set_card_art_web_path_index(normalized_name, web_path)
        _clear_card_art_failure_index(normalized_name)
        return web_path, None, 200

    try:
        r = _get_with_backoff(
            image_url,
            timeout=20,
            attempts=4,
            base_backoff_seconds=0.8,
            headers={
                "User-Agent": "CommanderTracker/1.0 (https://edh.figurensohn.de)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        if r.status_code != 200:
            reason, http_status = _classify_upstream_http_status(r.status_code)
            app.logger.warning(
                "cache_card_art_by_name download failed url=%s status=%s",
                image_url,
                r.status_code,
            )
            _set_card_art_failure_index(normalized_name, reason, http_status)
            return None, reason, http_status

        data = r.content

        if not data:
            app.logger.warning(
                "cache_card_art_by_name download empty body url=%s status=200",
                image_url,
            )
            _set_card_art_failure_index(normalized_name, "upstream_failure", 502)
            return None, "upstream_failure", 502

        try:
            out_path.write_bytes(data)
        except (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, OSError) as exc:
            app.logger.error(
                "cache_card_art_by_name write failed path=%s exception=%s detail=%s",
                out_path,
                exc.__class__.__name__,
                str(exc),
            )
            _set_card_art_failure_index(normalized_name, "storage_write_failed", 500)
            return None, "storage_write_failed", 500
        _set_card_art_web_path_index(normalized_name, web_path)
        _clear_card_art_failure_index(normalized_name)
        return web_path, None, 200

    except HTTPError as exc:
        reason, http_status = _classify_upstream_http_status(exc.code)
        app.logger.warning(
            "cache_card_art_by_name download failed url=%s status=%s exception=%s",
            image_url,
            exc.code,
            exc.__class__.__name__,
        )
        _set_card_art_failure_index(normalized_name, reason, http_status)
        return None, reason, http_status
    except URLError as exc:
        reason = "upstream_timeout" if _is_timeout_error(exc) else "upstream_failure"
        http_status = 504 if reason == "upstream_timeout" else 502
        app.logger.error(
            "cache_card_art_by_name download failed url=%s status=network exception=%s",
            image_url,
            exc.__class__.__name__,
        )
        _set_card_art_failure_index(normalized_name, reason, http_status)
        return None, reason, http_status
    except requests.Timeout as exc:
        app.logger.error(
            "cache_card_art_by_name download timeout url=%s exception=%s",
            image_url,
            exc.__class__.__name__,
        )
        _set_card_art_failure_index(normalized_name, "upstream_timeout", 504)
        return None, "upstream_timeout", 504
    except requests.RequestException as exc:
        app.logger.error(
            "cache_card_art_by_name download failed url=%s status=network exception=%s",
            image_url,
            exc.__class__.__name__,
        )
        _set_card_art_failure_index(normalized_name, "upstream_failure", 502)
        return None, "upstream_failure", 502
    except Exception as exc:
        app.logger.error(
            "cache_card_art_by_name download failed url=%s status=unknown exception=%s",
            image_url,
            exc.__class__.__name__,
        )
        _set_card_art_failure_index(normalized_name, "upstream_failure", 502)
        return None, "upstream_failure", 502


def _extract_deck_import_text() -> tuple[str | None, str | None]:
    """Returns (raw_input, source_label) from URL/file/paste fallback fields."""
    decklist_url = request.form.get("decklist_url", "").strip()
    if decklist_url:
        return decklist_url, "URL"

    upload = request.files.get("decklist_file")
    if upload and upload.filename:
        filename = upload.filename.strip()
        ext = Path(filename).suffix.lower()
        allowed = {".txt", ".dek", ".csv"}
        if ext not in allowed:
            raise DeckParserError(
                f"Unsupported decklist file '{filename}'. Allowed extensions: .txt, .dek, .csv."
            )

        raw_bytes = upload.read()
        if not raw_bytes:
            raise DeckParserError(f"Uploaded decklist file '{filename}' was empty.")

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("latin-1")

        return text, f"file ({filename})"

    pasted = request.form.get("decklist_text", "").strip()
    if pasted:
        return pasted, "pasted text"

    return None, None


def _render_decklist_text(parsed: dict) -> str:
    section_order = ["commander", "mainboard", "sideboard", "maybeboard"]
    lines: list[str] = []

    for section in section_order:
        entries = parsed.get("sections", {}).get(section) or []
        if not entries:
            continue
        lines.append(f"{section.title()}:")
        for entry in entries:
            qty = entry.get("quantity", 1)
            name = entry.get("name", "")
            lines.append(f"{qty} {name}")
        lines.append("")

    for section, entries in (parsed.get("sections", {}) or {}).items():
        if section in section_order or not entries:
            continue
        lines.append(f"{section.title()}:")
        for entry in entries:
            qty = entry.get("quantity", 1)
            name = entry.get("name", "")
            lines.append(f"{qty} {name}")
        lines.append("")

    return "\n".join(lines).strip()


def _pretty_section_name(section: str) -> str:
    labels = {
        "commander": "Commander",
        "mainboard": "Mainboard",
        "sideboard": "Sideboard",
        "maybeboard": "Maybeboard",
    }
    return labels.get(section, section.replace("_", " ").title())


def _load_decklist_data(deck: Deck) -> dict:
    raw_text = (deck.decklist_text or "").strip()
    if not raw_text:
        return {
            "has_list": False,
            "mode": "empty",
            "sections": [],
            "total_cards": 0,
            "commander_count": 0,
            "validation_hints": [],
            "export_text": "",
            "raw_text": "",
        }

    try:
        parsed = parse_plaintext_decklist(raw_text).as_dict()
    except DeckParserError:
        return {
            "has_list": True,
            "mode": "raw",
            "sections": [],
            "total_cards": 0,
            "commander_count": 0,
            "validation_hints": [],
            "export_text": raw_text,
            "raw_text": raw_text,
        }

    section_order = ["commander", "mainboard", "sideboard", "maybeboard"]
    parsed_sections = parsed.get("sections", {}) or {}

    ordered_section_keys = [s for s in section_order if parsed_sections.get(s)]
    ordered_section_keys.extend(
        [s for s in parsed_sections.keys() if s not in ordered_section_keys and parsed_sections.get(s)]
    )

    sections = []
    total_cards = 0
    commander_count = 0

    for section_key in ordered_section_keys:
        entries = parsed_sections.get(section_key) or []
        section_total = sum(int(e.get("quantity", 0) or 0) for e in entries)
        total_cards += section_total
        if section_key == "commander":
            commander_count += section_total

        sections.append(
            {
                "key": section_key,
                "label": _pretty_section_name(section_key),
                "entries": entries,
                "total": section_total,
            }
        )

    validation_hints = []
    if commander_count == 0:
        validation_hints.append({"status": "warning", "text": "No commander section found."})
    elif commander_count == 1:
        validation_hints.append({"status": "ok", "text": "Commander count looks valid (1)."})
    elif commander_count == 2:
        validation_hints.append(
            {"status": "ok", "text": "Commander count is 2 (partner/background style deck)."}
        )
    else:
        validation_hints.append(
            {
                "status": "warning",
                "text": f"Commander section has {commander_count} cards; most Commander decks use 1-2.",
            }
        )

    if total_cards == 100:
        validation_hints.append({"status": "ok", "text": "Total card count is exactly 100."})
    elif total_cards < 100:
        validation_hints.append(
            {"status": "warning", "text": f"Total card count is {total_cards}; Commander usually needs 100."}
        )
    else:
        validation_hints.append(
            {"status": "warning", "text": f"Total card count is {total_cards}; Commander usually needs 100."}
        )

    return {
        "has_list": True,
        "mode": "parsed",
        "sections": sections,
        "total_cards": total_cards,
        "commander_count": commander_count,
        "validation_hints": validation_hints,
        "export_text": _render_decklist_text(parsed),
        "raw_text": raw_text,
    }


def _count_imported_cards(parsed: dict) -> int:
    total = 0
    for entries in (parsed.get("sections", {}) or {}).values():
        for entry in entries:
            total += int(entry.get("quantity", 0) or 0)
    return total


class DeckPayloadError(ValueError):
    """Validation error while constructing/updating a deck from structured payload data."""


def _prepare_deck_payload(
    payload: dict,
    *,
    current_deck: "Deck" | None = None,
    require_name: bool = True,
    require_commander_input: bool = False,
) -> tuple[dict, dict]:
    name = (payload.get("name") or "").strip()
    commander_input = (payload.get("commander") or "").strip()
    raw_import = (payload.get("raw_import") or "").strip()
    imported_from = payload.get("imported_from")

    custom_commander_art_url = (payload.get("custom_commander_art_url") or "").strip()
    custom_card_art_url = (payload.get("custom_card_art_url") or "").strip()
    custom_commander_art_upload = payload.get("custom_commander_art_upload")
    custom_card_art_upload = payload.get("custom_card_art_upload")

    if require_name and not name:
        raise DeckPayloadError("Deck name is required.")
    if require_commander_input and not commander_input:
        raise DeckPayloadError("Commander is required.")

    commander_url_invalid = (
        not has_uploaded_custom_art(custom_commander_art_upload)
        and not is_valid_custom_art_url(custom_commander_art_url)
    )
    card_url_invalid = (
        not has_uploaded_custom_art(custom_card_art_upload)
        and not is_valid_custom_art_url(custom_card_art_url)
    )
    if commander_url_invalid or card_url_invalid:
        raise DeckPayloadError("Custom art URLs must be valid http(s) links up to 500 characters.")

    parsed_import = None
    if raw_import:
        if current_deck and imported_from == "text" and raw_import == (current_deck.decklist_text or "").strip():
            raw_import = ""
            imported_from = None
        else:
            parsed_import = parse_deck_input(raw_import)

    custom_commander_art_url_value, custom_commander_art_local = resolve_custom_art_value(
        custom_commander_art_url,
        custom_commander_art_upload,
        "custom commander art",
    )
    custom_card_art_url_value, custom_card_art_local = resolve_custom_art_value(
        custom_card_art_url,
        custom_card_art_upload,
        "custom card art",
    )

    resolved_commander = parsed_import.get("commander") if parsed_import else None
    commander_to_set = (resolved_commander or commander_input or (current_deck.commander if current_deck else "")).strip()
    if not commander_to_set:
        raise DeckPayloadError("Commander is required, or include one in the imported list.")

    if current_deck and not has_uploaded_custom_art(custom_commander_art_upload) and not custom_commander_art_url:
        custom_commander_art_local = current_deck.commander_local_art_custom
    if current_deck and not has_uploaded_custom_art(custom_card_art_upload) and not custom_card_art_url:
        custom_card_art_local = current_deck.custom_card_art_local

    prepared = {
        "name": name,
        "commander_input": commander_input,
        "commander_to_set": commander_to_set,
        "parsed_import": parsed_import,
        "imported_from": imported_from,
        "custom_commander_art_url": custom_commander_art_url_value,
        "custom_commander_art_local": custom_commander_art_local,
        "custom_card_art_url": custom_card_art_url_value,
        "custom_card_art_local": custom_card_art_local,
    }
    diagnostics = {
        "resolved_commander": resolved_commander,
        "tag_diagnostics": {"unresolved_count": 0, "unresolved_cards": []},
    }
    return prepared, diagnostics


def _apply_deck_payload(deck: "Deck", prepared: dict, diagnostics: dict) -> None:
    deck.name = prepared["name"]
    deck.commander = prepared["commander_to_set"]
    deck.custom_commander_art_url = prepared["custom_commander_art_url"]
    deck.commander_local_art_custom = prepared["custom_commander_art_local"]
    deck.custom_card_art_url = prepared["custom_card_art_url"]
    deck.custom_card_art_local = prepared["custom_card_art_local"]

    parsed_import = prepared["parsed_import"]
    if parsed_import:
        deck.decklist_text = _render_decklist_text(parsed_import)
        tags, tag_diagnostics = compute_deck_tags(extract_decklist_card_names(parsed_import))
        apply_deck_tags(deck, tags)
        diagnostics["tag_diagnostics"] = tag_diagnostics

    commander_to_set = prepared["commander_to_set"]
    commander_meta = resolve_commander_metadata(commander_to_set)
    deck.commander = commander_meta["commander"] or commander_to_set
    deck.commander_name = commander_meta["commander_name"]
    deck.commander_scryfall_id = commander_meta["commander_scryfall_id"]
    deck.commander_art_crop_url = commander_meta["commander_art_crop_url"]
    deck.commander_local_art_crop = commander_meta["commander_local_art_crop"]
    deck.color_identity = commander_meta["color_identity"]
    diagnostics["commander_meta"] = commander_meta


def _create_deck_from_payload(payload: dict, *, player_id: int, is_admin: bool) -> tuple[Deck, dict]:
    prepared, diagnostics = _prepare_deck_payload(payload, require_name=True, require_commander_input=False)
    deck = Deck(name=prepared["name"], commander=prepared["commander_to_set"], player_id=player_id)
    if is_admin:
        deck.retired = bool(payload.get("retired", False))
        deck.planned = bool(payload.get("planned", False))
        if deck.retired:
            deck.planned = False
    else:
        deck.planned = bool(payload.get("planned"))
    _apply_deck_payload(deck, prepared, diagnostics)
    diagnostics["parsed_import"] = prepared["parsed_import"]
    diagnostics["imported_from"] = prepared["imported_from"]
    diagnostics["commander_input"] = prepared["commander_input"]
    return deck, diagnostics


def _update_deck_from_payload(
    deck: "Deck",
    payload: dict,
    *,
    is_admin: bool,
    allow_owner_update: bool,
    require_commander_input: bool,
) -> tuple[Deck, dict]:
    prepared, diagnostics = _prepare_deck_payload(
        payload,
        current_deck=deck,
        require_name=True,
        require_commander_input=require_commander_input,
    )

    _apply_deck_payload(deck, prepared, diagnostics)

    if is_admin:
        if allow_owner_update and payload.get("player_id"):
            owner = db.session.get(Player, payload.get("player_id"))
            if owner:
                deck.player_id = owner.id
        deck.retired = bool(payload.get("retired"))
        deck.planned = bool(payload.get("planned"))
        if deck.retired:
            deck.planned = False

    diagnostics["parsed_import"] = prepared["parsed_import"]
    diagnostics["imported_from"] = prepared["imported_from"]
    diagnostics["commander_input"] = prepared["commander_input"]
    return deck, diagnostics


# -------------------------
# Auth helpers / guards
# -------------------------


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)

def api_user_payload(user):
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
        "player_id": user.player.id if user.player else None,
    }



def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = get_current_user()
        if not u:
            return redirect(url_for("login"))
        if not getattr(u, "is_admin", False):
            abort(403)
        return f(*args, **kwargs)

    return wrapper


def get_active_pod():
    current_user = get_current_user()
    accessible_ids = {p.id for p in get_accessible_pods(current_user)}

    active_pod_id = session.get("active_pod_id")
    if active_pod_id and int(active_pod_id) in accessible_ids:
        pod = db.session.get(Pod, int(active_pod_id))
        if pod and pod.is_active:
            return pod

    pod = None
    if accessible_ids:
        pod = Pod.query.filter(Pod.id.in_(accessible_ids), Pod.slug == DEFAULT_POD_SLUG).first()
        if not pod:
            pod = Pod.query.filter(Pod.id.in_(accessible_ids), Pod.is_active == True).order_by(Pod.id.asc()).first()  # noqa: E712

    if pod:
        session["active_pod_id"] = pod.id
        session.modified = True
    return pod


def get_accessible_pods(user):
    if not user:
        return []

    if user.is_admin:
        return Pod.query.order_by(Pod.is_active.desc(), Pod.name.asc()).all()

    if not user.player:
        return []

    return (
        Pod.query.join(PodMembership, PodMembership.pod_id == Pod.id)
        .filter(Pod.is_active == True, PodMembership.player_id == user.player.id)  # noqa: E712
        .order_by(Pod.name.asc())
        .all()
    )


def get_requestable_pods(user):
    if user and user.is_admin:
        return Pod.query.order_by(Pod.name.asc()).all()

    return Pod.query.filter_by(is_active=True).order_by(Pod.name.asc()).all()


def can_manage_pod(user, pod_id):
    if not user:
        return False

    pod = db.session.get(Pod, pod_id)
    if not pod or not pod.is_active:
        return False

    if user.is_admin:
        return True
    if not user.player:
        return False

    membership = PodMembership.query.filter_by(pod_id=pod_id, player_id=user.player.id).first()
    return bool(membership and membership.role == "podmaster")


def can_access_registration_request_queue(user):
    if not user:
        return False
    if user.is_admin:
        return True
    if not user.player:
        return False

    podmaster_membership = (
        PodMembership.query
        .filter_by(player_id=user.player.id, role="podmaster")
        .first()
    )
    return bool(podmaster_membership)


def can_approve_registration_request(user, registration_request):
    if not user or not registration_request:
        return False

    requested_pod = db.session.get(Pod, registration_request.requested_pod_id)
    if requested_pod and not requested_pod.is_active:
        return False

    if user.is_admin:
        return True
    return can_manage_pod(user, registration_request.requested_pod_id)


def can_deny_registration_request(user, registration_request):
    if not user or not registration_request:
        return False

    if user.is_admin:
        return True

    requested_pod = db.session.get(Pod, registration_request.requested_pod_id)
    if not requested_pod or not requested_pod.is_active:
        return False

    return can_manage_pod(user, registration_request.requested_pod_id)


def deny_registration_request_permission_message(user, registration_request):
    if not user or not registration_request:
        return "You don't have permission to deny this registration request."

    if user.is_admin:
        return None

    requested_pod = db.session.get(Pod, registration_request.requested_pod_id)
    if not requested_pod:
        return "Only admins can deny requests for missing pods."
    if not requested_pod.is_active:
        return "Only admins can deny requests for inactive pods."

    return "You don't have permission to deny this registration request."


def ensure_membership(pod_id, player_id, role="member"):
    membership = PodMembership.query.filter_by(pod_id=pod_id, player_id=player_id).first()
    if membership:
        if role == "podmaster" and membership.role != "podmaster":
            membership.role = "podmaster"
        return membership

    membership = PodMembership(pod_id=pod_id, player_id=player_id, role=role)
    db.session.add(membership)
    return membership


def resolve_requested_pod_for_approval(registration_request):
    if not registration_request:
        return None, "missing_request"

    requested_pod = db.session.get(Pod, registration_request.requested_pod_id)
    if requested_pod and requested_pod.is_active:
        return requested_pod, "requested"

    if requested_pod and not requested_pod.is_active:
        app.logger.warning(
            "Registration request %s targeted inactive pod %s; refusing assignment.",
            registration_request.id,
            requested_pod.id,
        )
        return None, "inactive_requested_pod"

    fallback_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG, is_active=True).first()
    app.logger.warning(
        "Registration request %s targeted missing pod_id %s; falling back to default pod %s.",
        registration_request.id,
        registration_request.requested_pod_id,
        getattr(fallback_pod, "id", None),
    )
    flash("Requested pod could not be validated. Assigned user to the default pod instead.")
    return fallback_pod, "fallback_default"



def approve_user_from_registration_request(registration_request, reviewer_user_id):
    if not registration_request:
        return "missing_request", None

    u = registration_request.user
    if not u:
        return "missing_user", None

    if registration_request.status != "pending" or u.is_active:
        return "not_pending", u

    existing_player = Player.query.filter_by(name=u.display_name).first()
    if existing_player and (not u.player or existing_player.id != u.player.id):
        return "name_collision", u

    if not u.player:
        u.player = Player(name=u.display_name)
        db.session.flush()
    elif u.player.name != u.display_name:
        u.player.name = u.display_name
        db.session.flush()

    target_pod, resolution = resolve_requested_pod_for_approval(registration_request)
    if not target_pod:
        if resolution == "inactive_requested_pod":
            return "inactive_pod", u
        app.logger.warning(
            "Registration request %s could not resolve a valid target pod and has no fallback default pod.",
            registration_request.id,
        )
        return "missing_pod", u

    ensure_membership(target_pod.id, u.player.id, role="member")

    u.is_active = True
    u.approved_at = datetime.utcnow()
    registration_request.status = "approved"
    registration_request.reviewed_at = datetime.utcnow()
    registration_request.reviewed_by_user_id = reviewer_user_id
    db.session.commit()
    return "approved", u


def deny_user_from_registration_request(registration_request, reviewer_user_id):
    if not registration_request:
        return "missing_request", None

    u = registration_request.user
    if not u:
        return "missing_user", None

    if registration_request.status != "pending" or u.is_active:
        return "not_pending", u

    # Keep both user and request rows for auditability and referential integrity.
    u.is_active = False
    registration_request.status = "denied"
    registration_request.reviewed_at = datetime.utcnow()
    registration_request.reviewed_by_user_id = reviewer_user_id
    db.session.commit()
    return "denied", u


@app.context_processor
def inject_pod_context():
    user = get_current_user()
    if not user:
        return {
            "nav_active_pod": None,
            "nav_available_pods": [],
            "use_sigtaara": False,
            "use_light_theme": False,
        }

    return {
        "nav_active_pod": get_active_pod(),
        "nav_available_pods": get_accessible_pods(user),
        "use_sigtaara": bool(user.use_sigtaara),
        "use_light_theme": bool(user.use_light_theme),
        "can_access_registration_requests": can_access_registration_request_queue(user),
    }


def game_query_for_scope():
    scope = (request.args.get("scope") or "pod").strip().lower()
    q = Game.query
    active_pod = get_active_pod()
    if scope != "all" and active_pod:
        q = q.filter(Game.pod_id == active_pod.id)
        scope = "pod"
    return q, scope, active_pod


# -------------------------
# Login Required
# -------------------------
@app.before_request
def require_login():
    if "user_id" not in session:
        # Dev-only: auto-login test user (env-gated)
        if AUTO_LOGIN_TEST_USER:
            u = bootstrap_test_user()
            if u:
                # safety net: ensure player exists
                if not u.player:
                    u.player = Player(name=u.display_name)
                    db.session.commit()

                session["user_id"] = u.id
                session["username"] = u.username
                session["display_name"] = u.display_name
                session["is_admin"] = u.is_admin
                session["use_sigtaara"] = u.use_sigtaara
                session["use_light_theme"] = u.use_light_theme
                get_active_pod()
                return None

        # Allow auth routes + static assets + selected public API routes.
        public_endpoints = {
            "login",
            "register",
            "static",
            "art",
            "api_login",
            "api_logout",
            "api_me",
            "api_card_art",
            "api_gallery_image",
            "api_cards_autocomplete",
            "api_cards_named",
            "api_commander_bracket",
            "api_game_state",
            "api_join_get",
            "api_join_claim",
        }
        if request.endpoint not in public_endpoints:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login") + "?next=" + quote(request.full_path))

    get_active_pod()


# -------------------------
# Auth Routes
# -------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    requestable_pods = get_requestable_pods(None)

    if request.method == "POST":
        username = request.form["username"].strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form["password"]
        confirm = request.form["confirm"]
        requested_pod_raw = request.form.get("requested_pod_id", "").strip()

        requested_pod = None
        if not requested_pod_raw:
            flash("Please choose a pod to request access to.")
        else:
            try:
                requested_pod_id = int(requested_pod_raw)
            except ValueError:
                flash("Selected pod is invalid.")
            else:
                allowed_pod_ids = {pod.id for pod in requestable_pods}
                if requested_pod_id not in allowed_pod_ids:
                    flash("Selected pod is unavailable or retired.")
                else:
                    requested_pod = db.session.get(Pod, requested_pod_id)

        if not requested_pod:
            return render_template("register.html", requestable_pods=requestable_pods)

        if not username or not display_name or not password:
            flash("Username, display name, and password required")
        elif password != confirm:
            flash("Passwords do not match")
        elif User.query.filter_by(username=username).first():
            flash("Username already taken")
        elif User.query.filter_by(display_name=display_name).first() or Player.query.filter_by(name=display_name).first():
            flash("Display name already taken")
        else:
            password_error = validate_password_rules(password)
            if password_error:
                flash(password_error)
                return render_template("register.html", requestable_pods=requestable_pods)

            hashed = generate_password_hash(password)
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hashed,
                is_active=False,
                is_admin=False
            )

            db.session.add(user)
            db.session.flush()
            db.session.add(
                RegistrationRequest(
                    user_id=user.id,
                    requested_pod_id=requested_pod.id,
                    status="pending",
                )
            )
            db.session.commit()

            flash(f"Registration submitted for {requested_pod.name}; pending approval.")
            return redirect(url_for("login"))

    return render_template("register.html", requestable_pods=requestable_pods)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            if not user.is_active:
                flash("Account pending approval. Please contact an admin.")
                return render_template("login.html")

            # safety net: ensure player exists (shouldn't happen, but avoids broken accounts)
            if not user.player:
                user.player = Player(name=user.display_name)
                db.session.commit()

            session["user_id"] = user.id
            session["username"] = user.username
            session["display_name"] = user.display_name
            session["is_admin"] = user.is_admin
            session["use_sigtaara"] = user.use_sigtaara
            session["use_light_theme"] = user.use_light_theme
            get_active_pod()

            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

        flash("Invalid username or password")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully")
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u = get_current_user()
    if not u:
        return redirect(url_for("login"))

    if request.method == "POST":
        action = request.form.get("action")

        if not action:
            flash("Missing profile action. Please submit the form again.")
            return redirect(url_for("profile"))

        if action == "change_password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_new_password = request.form.get("confirm_new_password", "")

            if not current_password or not new_password or not confirm_new_password:
                flash("Please fill out all password fields.")
                return redirect(url_for("profile"))

            if not check_password_hash(u.password_hash, current_password):
                flash("Current password is incorrect.")
                return redirect(url_for("profile"))

            password_error = validate_password_rules(new_password)
            if password_error:
                flash(password_error)
                return redirect(url_for("profile"))

            if new_password != confirm_new_password:
                flash("New password and confirmation do not match.")
                return redirect(url_for("profile"))

            u.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Password changed successfully.")
            return redirect(url_for("profile"))

        if action == "update_profile":
            new_display = request.form.get("display_name", "").strip()
            use_sigtaara = request.form.get("use_sigtaara") == "on"
            use_light_theme = request.form.get("use_light_theme") == "on"
            mana_fucked_salt_value_raw = (request.form.get("mana_fucked_salt_value") or "").strip()
            misplayed_salt_value_raw = (request.form.get("misplayed_salt_value") or "").strip()

            if not new_display:
                flash("Display name cannot be empty.")
                return redirect(url_for("profile"))

            if not mana_fucked_salt_value_raw:
                flash("Mana Fucked salt value is required.")
                return redirect(url_for("profile"))

            if not misplayed_salt_value_raw:
                flash("Misplayed salt value is required.")
                return redirect(url_for("profile"))

            try:
                mana_fucked_salt_value = int(mana_fucked_salt_value_raw)
            except ValueError:
                flash("Mana Fucked salt value must be a whole number.")
                return redirect(url_for("profile"))

            try:
                misplayed_salt_value = int(misplayed_salt_value_raw)
            except ValueError:
                flash("Misplayed salt value must be a whole number.")
                return redirect(url_for("profile"))

            if mana_fucked_salt_value < 0 or mana_fucked_salt_value > 50:
                flash("Mana Fucked salt value must be between 0 and 50.")
                return redirect(url_for("profile"))

            if misplayed_salt_value < 0 or misplayed_salt_value > 50:
                flash("Misplayed salt value must be between 0 and 50.")
                return redirect(url_for("profile"))

            existing_user = User.query.filter(User.display_name == new_display, User.id != u.id).first()
            existing_player = Player.query.filter(Player.name == new_display).first()

            if existing_user or (existing_player and (not u.player or existing_player.id != u.player.id)):
                flash("That display name is already taken.")
                return redirect(url_for("profile"))

            u.display_name = new_display
            u.use_sigtaara = use_sigtaara
            u.use_light_theme = use_light_theme
            u.mana_fucked_salt_value = mana_fucked_salt_value
            u.misplayed_salt_value = misplayed_salt_value
            if u.player:
                u.player.name = new_display  # keep in sync with game tracking

            db.session.commit()
            session["display_name"] = new_display
            session["use_sigtaara"] = use_sigtaara
            session["use_light_theme"] = use_light_theme
            flash("Profile updated.")
            return redirect(url_for("profile"))

        flash("Unknown profile action.")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=u)


# -------------------------
# Main App Routes
# -------------------------
@app.route("/art/<path:filename>")
def art(filename):
    return send_from_directory(ART_DIR, filename)


@app.route("/apk/<path:filename>")
def apk_file(filename):
    return send_from_directory(APK_DIR, filename, as_attachment=True)


@app.route("/apk")
def apk_download():
    payload, error = _load_android_release_manifest()
    available = error is None
    return render_template("apk_download.html", manifest=payload, available=available)


def _load_android_release_manifest() -> tuple[dict | None, tuple[Response, int] | None]:
    manifest = None
    manifest_error = None
    if ANDROID_LATEST_RELEASE_MANIFEST.is_file():
        try:
            manifest = json.loads(ANDROID_LATEST_RELEASE_MANIFEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            manifest_error = exc

    artifact_path = _find_manifest_android_release_artifact(manifest)
    if artifact_path is None:
        artifact_path = _find_latest_android_release_artifact()
    if artifact_path is None:
        if manifest_error is not None:
            return None, (jsonify({"error": f"Invalid Android release manifest: {manifest_error}"}), 500)
        if manifest is not None:
            artifact_file_name = manifest.get("artifactFileName")
            if isinstance(artifact_file_name, str) and artifact_file_name.strip():
                return None, (jsonify({"error": f"Android release artifact missing: {artifact_file_name}"}), 500)
        return None, (jsonify({"error": "Android release artifact not found"}), 404)

    payload = _build_android_release_payload(artifact_path, manifest=manifest)
    return payload, None


def _find_manifest_android_release_artifact(manifest: dict | None) -> Path | None:
    if not isinstance(manifest, dict):
        return None

    artifact_file_name = manifest.get("artifactFileName")
    if not isinstance(artifact_file_name, str):
        return None

    artifact_file_name = artifact_file_name.strip()
    if not artifact_file_name:
        return None

    artifact_path = APK_DIR / Path(artifact_file_name).name
    if artifact_path.is_file():
        return artifact_path

    return None


def _find_latest_android_release_artifact() -> Path | None:
    apk_paths = [path for path in APK_DIR.glob("*.apk") if path.is_file()]
    if not apk_paths:
        return None
    apk_paths.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return apk_paths[0]


def _build_android_release_payload(artifact_path: Path, manifest: dict | None = None) -> dict:
    stat_result = artifact_path.stat()
    published_at = datetime.utcfromtimestamp(stat_result.st_mtime).replace(microsecond=0).isoformat() + "Z"
    payload = {
        "artifactFileName": artifact_path.name,
        "artifactPath": f"apk/{artifact_path.name}",
        "artifactUrl": url_for("apk_file", filename=artifact_path.name, _external=True),
        "buildDate": published_at,
        "publishedAt": published_at,
    }

    if isinstance(manifest, dict) and manifest.get("artifactFileName") == artifact_path.name:
        payload.update(manifest)
        payload["artifactFileName"] = artifact_path.name
        payload["artifactPath"] = f"apk/{artifact_path.name}"
        payload["artifactUrl"] = url_for("apk_file", filename=artifact_path.name, _external=True)
        payload.setdefault("buildDate", published_at)
        payload.setdefault("publishedAt", published_at)
        return payload

    match = APK_VERSION_FILENAME_RE.match(artifact_path.name)
    if match:
        version_name = match.group("version_name")
        if version_name:
            payload["versionName"] = version_name.lstrip("v")
        version_code = match.group("version_code")
        if version_code:
            payload["versionCode"] = int(version_code)

    return payload


@app.route("/admin/users")
@admin_required
def admin_users():
    pending_requests = (
        RegistrationRequest.query
        .join(User, RegistrationRequest.user_id == User.id)
        .filter(
            RegistrationRequest.status == "pending",
            User.is_active == False,  # noqa: E712
        )
        .order_by(RegistrationRequest.created_at.asc())
        .all()
    )
    pending = [req.user for req in pending_requests if req.user]
    active = User.query.filter_by(is_active=True).order_by(User.created_at.desc()).all()
    pending_requests_by_user_id = {
        req.user_id: req
        for req in pending_requests
    }
    return render_template(
        "admin_users.html",
        pending=pending,
        active=active,
        pending_requests_by_user_id=pending_requests_by_user_id,
    )


@app.route("/registration_requests")
@login_required
def registration_requests():
    me = get_current_user()
    if not can_access_registration_request_queue(me):
        abort(403)

    pending_query = (
        RegistrationRequest.query
        .join(User, RegistrationRequest.user_id == User.id)
        .filter(
            RegistrationRequest.status == "pending",
            User.is_active == False,  # noqa: E712
        )
    )

    if not me.is_admin:
        manageable_pod_ids = {
            m.pod_id
            for m in PodMembership.query.filter_by(player_id=me.player.id, role="podmaster").all()
        }
        pending_query = pending_query.filter(
            RegistrationRequest.requested_pod_id.in_(manageable_pod_ids if manageable_pod_ids else [-1])
        )

    pending = pending_query.order_by(RegistrationRequest.created_at.asc()).all()
    return render_template("registration_requests.html", pending=pending)


@app.route("/registration_requests/<int:request_id>/approve", methods=["POST"])
@login_required
def approve_registration_request(request_id):
    me = get_current_user()
    registration_request = db.session.get(RegistrationRequest, request_id)
    if not registration_request:
        abort(404)
    if not can_approve_registration_request(me, registration_request):
        abort(403)

    status, approved_user = approve_user_from_registration_request(registration_request, me.id if me else None)
    if status in {"missing_request", "missing_user"}:
        abort(404)
    if status == "not_pending":
        flash("Registration request is no longer pending.")
        return redirect(url_for("registration_requests"))
    if status == "name_collision":
        flash(f"Can't approve: display name '{approved_user.display_name}' is already used by a Player.")
        return redirect(url_for("registration_requests"))
    if status == "inactive_pod":
        flash("Can't approve: requested pod is inactive.")
        return redirect(url_for("registration_requests"))
    if status == "missing_pod":
        flash("Can't approve: no valid pod is available for this registration request.")
        return redirect(url_for("registration_requests"))

    flash(f"Approved {approved_user.display_name}")
    return redirect(url_for("registration_requests"))



@app.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@admin_required
def admin_approve_user(user_id):
    me = get_current_user()
    registration_request = RegistrationRequest.query.filter_by(user_id=user_id).first()
    if not registration_request:
        flash("No pending registration request found for that user.")
        return redirect(url_for("admin_users"))

    status, approved_user = approve_user_from_registration_request(registration_request, me.id if me else None)
    if status in {"missing_request", "missing_user"}:
        flash("No pending registration request found for that user.")
        return redirect(url_for("admin_users"))
    if status == "not_pending":
        flash("Registration request is no longer pending.")
        return redirect(url_for("admin_users"))
    if status == "name_collision":
        flash(f"Can't approve: display name '{approved_user.display_name}' is already used by a Player.")
        return redirect(url_for("admin_users"))
    if status == "inactive_pod":
        flash("Can't approve: requested pod is inactive.")
        return redirect(url_for("admin_users"))
    if status == "missing_pod":
        flash("Can't approve: no valid pod is available for this registration request.")
        return redirect(url_for("admin_users"))

    flash(f"Approved {approved_user.display_name}")
    return redirect(url_for("admin_users"))

@app.route("/saltmine")
@login_required
def saltmine():
    game_q, scope, active_pod = game_query_for_scope()

    scoped_games = game_q.all()
    scoped_game_ids = [g.id for g in scoped_games]

    participants = (
        GameParticipant.query
        .filter(GameParticipant.game_id.in_(scoped_game_ids if scoped_game_ids else [-1]))
        .all()
    )

    game_salt_stats: dict[int, dict[str, int | bool]] = {}
    player_salt_stats: dict[int, dict[str, int]] = {}
    deck_salt_stats: dict[int, dict[str, int]] = {}

    for gp in participants:
        parsed_flags = participant_flags_snapshot(gp)
        salt_count = participant_salt_count(parsed_flags)
        salted = salt_count > 0

        game_entry = game_salt_stats.setdefault(gp.game_id, {
            "salted_players": 0,
            "participants": 0,
            "any_salted": False,
            "salt_clicks": 0,
        })
        game_entry["participants"] += 1
        if salted:
            game_entry["salted_players"] += 1
            game_entry["any_salted"] = True
        game_entry["salt_clicks"] += salt_count

        player_entry = player_salt_stats.setdefault(gp.player_id, {
            "salted_games": 0,
            "games": 0,
            "salt_clicks": 0,
        })
        player_entry["games"] += 1
        if salted:
            player_entry["salted_games"] += 1
        player_entry["salt_clicks"] += salt_count

        deck_entry = deck_salt_stats.setdefault(gp.deck_id, {
            "salted_games": 0,
            "games": 0,
            "salt_clicks": 0,
        })
        deck_entry["games"] += 1
        if salted:
            deck_entry["salted_games"] += 1
        deck_entry["salt_clicks"] += salt_count

    for g in scoped_games:
        stats = game_salt_stats.setdefault(g.id, {
            "salted_players": 0,
            "participants": 0,
            "any_salted": False,
            "salt_clicks": 0,
        })
        legacy_salt = g.salt_rating is not None
        stats["legacy_salt_rating"] = g.salt_rating
        stats["has_legacy_salt"] = legacy_salt
        stats["sort_salted_players"] = int(stats["salted_players"])
        stats["sort_salt_clicks"] = int(stats["salt_clicks"])
        stats["sort_has_salt"] = int(stats["any_salted"] or legacy_salt)

    # Top salty games by participant-level salted flags
    salty_games = sorted(
        scoped_games,
        key=lambda g: (
            int(game_salt_stats[g.id]["sort_salted_players"]),
            int(game_salt_stats[g.id]["sort_salt_clicks"]),
            int(game_salt_stats[g.id]["sort_has_salt"]),
            g.date,
        ),
        reverse=True,
    )
    salty_games = [g for g in salty_games if game_salt_stats[g.id]["sort_has_salt"]][:10]

    # Saltiest players by salted participation rate (min 3 games)
    player_ids = list(player_salt_stats.keys())
    players_by_id = {
        p.id: p
        for p in Player.query.filter(Player.id.in_(player_ids if player_ids else [-1])).all()
    }
    salty_players = []
    for player_id, stats in player_salt_stats.items():
        games_played = int(stats["games"])
        salted_games = int(stats["salted_games"])
        if games_played < 3 or player_id not in players_by_id:
            continue
        salt_rate = (salted_games / games_played) * 100
        salty_players.append((players_by_id[player_id], salt_rate, salted_games, games_played))
    salty_players.sort(key=lambda row: (row[1], row[2], row[3], row[0].name.lower()), reverse=True)
    salty_players = salty_players[:10]

    # Saltiest decks by salted participation rate (min 3 games)
    deck_ids = list(deck_salt_stats.keys())
    decks_by_id = {
        d.id: d
        for d in Deck.query.filter(Deck.id.in_(deck_ids if deck_ids else [-1])).all()
    }
    salty_decks = []
    for deck_id, stats in deck_salt_stats.items():
        games_played = int(stats["games"])
        salted_games = int(stats["salted_games"])
        if games_played < 3 or deck_id not in decks_by_id:
            continue
        salt_rate = (salted_games / games_played) * 100
        salty_decks.append((decks_by_id[deck_id], salt_rate, salted_games, games_played))
    salty_decks.sort(key=lambda row: (row[1], row[2], row[3], row[0].name.lower()), reverse=True)
    salty_decks = salty_decks[:10]

    # Starting player advantage
    sp = (
        db.session.query(
            func.count(Game.id).label("games"),
            func.sum(
                case(
                    (Game.winner_id == Game.starting_player_id, 1),
                    else_=0
                )
            ).label("wins")
        )
        .filter(Game.starting_player_id.isnot(None))
        .filter(Game.id.in_(game_q.with_entities(Game.id)))
        .first()
    )
    start_games = int(sp.games or 0)
    start_wins = int(sp.wins or 0)
    start_winrate = round((start_wins / start_games) * 100, 1) if start_games else None

    seat_winrate_rows = (
        db.session.query(
            GameParticipant.seat_position.label("seat_position"),
            func.count(GameParticipant.id).label("games"),
            func.sum(
                case(
                    (Game.winner_id == GameParticipant.player_id, 1),
                    else_=0,
                )
            ).label("wins"),
        )
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(Game.id.in_(game_q.with_entities(Game.id)))
        .filter(GameParticipant.seat_position.isnot(None))
        .group_by(GameParticipant.seat_position)
        .order_by(GameParticipant.seat_position.asc())
        .all()
    )
    seat_winrates = []
    for row in seat_winrate_rows:
        games = int(row.games or 0)
        wins = int(row.wins or 0)
        seat_winrates.append(
            {
                "seat_position": int(row.seat_position),
                "games": games,
                "wins": wins,
                "winrate": round((wins / games) * 100, 1) if games else None,
            }
        )

    return render_template(
        "saltmine.html",
        salty_games=salty_games,
        game_salt_stats=game_salt_stats,
        salty_players=salty_players,
        salty_decks=salty_decks,
        start_games=start_games,
        start_wins=start_wins,
        start_winrate=start_winrate,
        seat_winrates=seat_winrates,
        scope=scope,
        active_pod=active_pod,
    )


@app.route("/admin/users/<int:user_id>/deactivate", methods=["POST"])
@admin_required
def admin_deactivate_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)

    me = get_current_user()
    if me and me.id == u.id:
        flash("You can't deactivate your own account.")
        return redirect(url_for("admin_users"))

    u.is_active = False
    db.session.commit()
    flash(f"Deactivated {u.display_name}")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)

    me = get_current_user()
    if me and me.id == u.id:
        flash("You can't delete your own account.")
        return redirect(url_for("admin_users"))

    linked_player = u.player
    if linked_player:
        linked_player.user_id = None

    RegistrationRequest.query.filter_by(reviewed_by_user_id=u.id).update(
        {RegistrationRequest.reviewed_by_user_id: None},
        synchronize_session=False,
    )
    RegistrationRequest.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    flash("User account deleted.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle_admin", methods=["POST"])
@admin_required
def admin_toggle_admin(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)

    me = get_current_user()
    if me and me.id == u.id:
        flash("You can't change your own admin status here.")
        return redirect(url_for("admin_users"))

    u.is_admin = not u.is_admin
    db.session.commit()
    flash(f"Admin for {u.display_name}: {u.is_admin}")
    return redirect(url_for("admin_users"))


@app.route("/admin/fix_art_paths")
@admin_required
def fix_art_paths():
    decks = Deck.query.all()
    changed = 0
    for d in decks:
        deck_changed = False
        if d.commander_local_art_crop and d.commander_local_art_crop.startswith("/static/commander_art/"):
            d.commander_local_art_crop = None
            deck_changed = True
        if d.commander_local_art_custom and d.commander_local_art_custom.startswith("/static/commander_art/"):
            d.commander_local_art_custom = None
            deck_changed = True
        if deck_changed:
            changed += 1
    db.session.commit()
    return f"Fixed {changed} decks"

@app.route("/registration_requests/<int:request_id>/deny", methods=["POST"])
@login_required
def deny_registration_request(request_id):
    me = get_current_user()
    registration_request = db.session.get(RegistrationRequest, request_id)
    if not registration_request:
        abort(404)
    if not can_deny_registration_request(me, registration_request):
        flash(deny_registration_request_permission_message(me, registration_request))
        return redirect(url_for("registration_requests"))

    u = registration_request.user
    if not u:
        abort(404)

    status, denied_user = deny_user_from_registration_request(registration_request, me.id if me else None)
    if status == "missing_user":
        abort(404)
    if status == "not_pending":
        flash("Only pending users can be denied.")
        return redirect(url_for("registration_requests"))

    flash(f"Denied registration request for '{denied_user.username}'.")
    return redirect(url_for("registration_requests"))




@app.route("/admin/users/<int:user_id>/deny", methods=["POST"])
@admin_required
def admin_deny_user(user_id):
    me = get_current_user()
    registration_request = RegistrationRequest.query.filter_by(user_id=user_id).first()
    if not registration_request:
        flash("No pending registration request found for that user.")
        return redirect(url_for("admin_users"))

    if not can_deny_registration_request(me, registration_request):
        flash(deny_registration_request_permission_message(me, registration_request))
        return redirect(url_for("admin_users"))

    status, denied_user = deny_user_from_registration_request(registration_request, me.id if me else None)
    if status == "missing_user":
        abort(404)
    if status == "not_pending":
        flash("Only pending users can be denied.")
        return redirect(url_for("admin_users"))

    flash(f"Denied registration request for '{denied_user.username}'.")
    return redirect(url_for("admin_users"))

@app.route("/")
def index():
    game_q, scope, active_pod = game_query_for_scope()
    game_ids_subquery = game_q.with_entities(Game.id)
    current_user = get_current_user()
    available_pods = get_accessible_pods(current_user)

    # Player stats — aggregate queries instead of per-player counts
    players = Player.query.all()
    wins_by_player = dict(
        db.session.query(Game.winner_id, func.count(Game.id))
        .filter(Game.id.in_(game_ids_subquery))
        .group_by(Game.winner_id)
        .all()
    )
    played_by_player = dict(
        db.session.query(GameParticipant.player_id, func.count(GameParticipant.id))
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(Game.id.in_(game_ids_subquery))
        .group_by(GameParticipant.player_id)
        .all()
    )
    player_stats = []
    for p in players:
        wins = wins_by_player.get(p.id, 0)
        played = played_by_player.get(p.id, 0)
        winrate = round(wins / played * 100, 1) if played > 0 else 0.0
        player_stats.append({"player": p, "wins": wins, "played": played, "winrate": winrate})

    player_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))

    # Enrich top players with most-played deck art
    top_players = player_stats[:3]
    for row in top_players:
        p = row["player"]

        most_played = (
            db.session.query(Deck, func.count(GameParticipant.id).label("plays"))
            .join(GameParticipant, GameParticipant.deck_id == Deck.id)
            .join(Game, Game.id == GameParticipant.game_id)
            .filter(GameParticipant.player_id == p.id, Game.id.in_(game_ids_subquery))
            .group_by(Deck.id)
            .order_by(text("plays DESC"))
            .first()
        )

        if most_played:
            deck = most_played[0]
            row["most_played_deck"] = deck
            row["bg_art"] = deck.commander_art_url
        else:
            row["most_played_deck"] = None
            row["bg_art"] = None

    # Deck stats — aggregate queries instead of per-deck counts
    decks = Deck.query.all()
    uses_by_deck = dict(
        db.session.query(GameParticipant.deck_id, func.count(GameParticipant.id))
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(Game.id.in_(game_ids_subquery))
        .group_by(GameParticipant.deck_id)
        .all()
    )
    wins_by_deck = dict(
        db.session.query(GameParticipant.deck_id, func.count(GameParticipant.id))
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(Game.id.in_(game_ids_subquery), Game.winner_id == GameParticipant.player_id)
        .group_by(GameParticipant.deck_id)
        .all()
    )
    deck_stats = []
    for d in decks:
        wins = wins_by_deck.get(d.id, 0)
        uses = uses_by_deck.get(d.id, 0)
        winrate = round(wins / uses * 100, 1) if uses > 0 else 0.0
        deck_stats.append({"deck": d, "wins": wins, "uses": uses, "winrate": winrate})

    deck_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))
    top_decks = deck_stats[:6]

    # Recent games
    recent_games = game_q.order_by(Game.date.desc()).limit(10).all()
    _recent_ids = [g.id for g in recent_games]
    _all_parts = GameParticipant.query.filter(GameParticipant.game_id.in_(_recent_ids)).all() if _recent_ids else []
    game_parts: dict[int, list] = {}
    for _p in _all_parts:
        game_parts.setdefault(_p.game_id, []).append(_p)

    # Deck Spotlight: deck that won last (winner's deck in most recent game)
    last_winning_deck = None
    if recent_games:
        last_game = recent_games[0]
        winner_part = GameParticipant.query.filter_by(game_id=last_game.id, player_id=last_game.winner_id).first()
        if winner_part:
            last_winning_deck = winner_part.deck
    
    # Best deck by winrate (prefer decks with >= min_games)
    min_games = 3
    best_deck = None
    best_candidates = []
    for row in deck_stats:
        d = row["deck"]
        uses = row["uses"]
        wins = row["wins"]
        winrate = row["winrate"]
        if uses >= min_games:
            best_candidates.append((winrate, uses, wins, d))

    if best_candidates:
        best_candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        best_deck = best_candidates[0][3]

    scoped_participants = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(Game.id.in_(game_ids_subquery))
        .all()
    )

    deck_tags_cache: dict[int, dict[str, bool]] = {}
    deck_mechanics_by_id = {}
    for d in decks:
        tags = get_deck_parsed_tags(d, cache=deck_tags_cache)
        deck_mechanics_by_id[d.id] = derive_deck_mechanics(tags)

    mechanic_keys = ("monarch", "poison", "energy", "experience")
    tag_presence_counts = {key: 0 for key in mechanic_keys}
    for mechanics in deck_mechanics_by_id.values():
        for key in mechanic_keys:
            if mechanics[key]:
                tag_presence_counts[key] += 1

    capability_uses_wins = {
        key: {"uses": 0, "wins": 0}
        for key in mechanic_keys
    }
    activation_correlation_denominators = {
        key: {"activated_games_with_capability": 0, "games_with_capability": 0}
        for key in ("monarch", "poison")
    }

    participants_by_game_id: dict[int, list[GameParticipant]] = {}

    for gp in scoped_participants:
        mechanics = deck_mechanics_by_id.get(gp.deck_id)
        if not mechanics:
            continue

        gp.deck_mechanics = mechanics
        participants_by_game_id.setdefault(gp.game_id, []).append(gp)

        for key in mechanic_keys:
            if not mechanics[key]:
                continue
            capability_uses_wins[key]["uses"] += 1
            if gp.game and gp.game.winner_id == gp.player_id:
                capability_uses_wins[key]["wins"] += 1

    for participants in participants_by_game_id.values():
        game_activation = compute_game_mechanic_activation(participants)

        if game_activation["monarch_capable_present"]:
            activation_correlation_denominators["monarch"]["games_with_capability"] += 1
            if game_activation["monarch_activated"]:
                activation_correlation_denominators["monarch"]["activated_games_with_capability"] += 1

        if game_activation["poison_capable_present"]:
            activation_correlation_denominators["poison"]["games_with_capability"] += 1
            if game_activation["poison_activated"]:
                activation_correlation_denominators["poison"]["activated_games_with_capability"] += 1

    for key, counts in activation_correlation_denominators.items():
        games_with_capability = counts["games_with_capability"]
        if games_with_capability > 0:
            ratio = counts["activated_games_with_capability"] / games_with_capability
            counts["activation_given_capability"] = round(ratio * 100, 1)
        else:
            counts["activation_given_capability"] = 0.0

    return render_template(
        "index.html",
        player_stats=player_stats,
        deck_stats=top_decks,
        recent_games=recent_games,
        game_parts=game_parts,
        top_players=top_players,
        best_deck=best_deck,
        last_winning_deck=last_winning_deck,
        tag_presence_counts=tag_presence_counts,
        capability_uses_wins=capability_uses_wins,
        activation_correlation_denominators=activation_correlation_denominators,
        scope=scope,
        active_pod=active_pod,
        available_pods=available_pods,
    )


@app.route("/pods")
@login_required
def pods():
    me = get_current_user()
    pods_list = get_accessible_pods(me)
    active_pod = get_active_pod()
    manageable_pod_ids = {pod.id for pod in pods_list if can_manage_pod(me, pod.id)}

    pending_request_count = 0
    if can_access_registration_request_queue(me):
        pending_query = (
            RegistrationRequest.query
            .join(User, RegistrationRequest.user_id == User.id)
            .filter(
                RegistrationRequest.status == "pending",
                User.is_active == False,  # noqa: E712
            )
        )
        if not me.is_admin:
            pending_query = pending_query.filter(
                RegistrationRequest.requested_pod_id.in_(manageable_pod_ids if manageable_pod_ids else [-1])
            )
        pending_request_count = pending_query.count()

    selected_pod_id = request.args.get("pod_id", type=int)
    selected_pod = db.session.get(Pod, selected_pod_id) if selected_pod_id else active_pod
    if selected_pod and selected_pod not in pods_list and not me.is_admin:
        abort(403)

    memberships = []
    all_players = []
    if selected_pod and can_manage_pod(me, selected_pod.id):
        memberships = (
            PodMembership.query
            .filter_by(pod_id=selected_pod.id)
            .join(Player, Player.id == PodMembership.player_id)
            .order_by(text("CASE WHEN pod_membership.role = 'podmaster' THEN 0 ELSE 1 END"), Player.name.asc())
            .all()
        )
        all_players = Player.query.order_by(Player.name.asc()).all()

    return render_template(
        "pods.html",
        pods=pods_list,
        active_pod=active_pod,
        manageable_pod_ids=manageable_pod_ids,
        selected_pod=selected_pod,
        memberships=memberships,
        all_players=all_players,
        can_manage_selected=bool(selected_pod and can_manage_pod(me, selected_pod.id)),
        is_admin=bool(me and me.is_admin),
        can_access_registration_requests=can_access_registration_request_queue(me),
        pending_request_count=pending_request_count,
    )


@app.route("/pods", methods=["POST"])
@admin_required
def create_pod():
    name = (request.form.get("name") or "").strip()
    slug_input = (request.form.get("slug") or "").strip().lower()
    slug = slug_input or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    if not name:
        flash("Pod name is required.")
        return redirect(url_for("pods"))

    if not slug:
        flash("Pod slug is required.")
        return redirect(url_for("pods"))

    if Pod.query.filter((Pod.name == name) | (Pod.slug == slug)).first():
        flash("Pod with same name or slug already exists.")
        return redirect(url_for("pods"))

    pod = Pod(name=name, slug=slug, is_active=True)
    db.session.add(pod)
    db.session.flush()

    for player in Player.query.all():
        ensure_membership(pod.id, player.id, role="member")

    db.session.commit()
    flash(f"Created pod '{name}'.")
    return redirect(url_for("pods", pod_id=pod.id))


@app.route("/pods/<int:pod_id>/name", methods=["POST"])
@login_required
def update_pod_name(pod_id):
    me = get_current_user()
    if not can_manage_pod(me, pod_id):
        abort(403)

    pod = db.session.get(Pod, pod_id)
    if not pod:
        abort(404)

    new_name = (request.form.get("name") or "").strip()
    if not new_name:
        flash("Pod name is required.")
        return redirect(url_for("pods", pod_id=pod_id))

    duplicate = Pod.query.filter(Pod.id != pod_id, Pod.name == new_name).first()
    if duplicate:
        flash("A pod with that name already exists.")
        return redirect(url_for("pods", pod_id=pod_id))

    pod.name = new_name
    db.session.commit()
    flash("Pod name updated.")
    return redirect(url_for("pods", pod_id=pod_id))


@app.route("/pods/switch/<int:pod_id>", methods=["POST"])
@login_required
def switch_pod(pod_id):
    me = get_current_user()
    pod = db.session.get(Pod, pod_id)
    if not pod or not pod.is_active:
        abort(404)

    allowed_ids = {p.id for p in get_accessible_pods(me)}
    if pod.id not in allowed_ids:
        abort(403)

    session["active_pod_id"] = pod.id
    session.modified = True

    msg = f"Switched to {pod.name}."
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "message": msg, "pod": {"id": pod.id, "name": pod.name}})

    flash(msg)
    return redirect(url_for("pods", pod_id=pod.id))


@app.route("/pods/<int:pod_id>/retire", methods=["POST"])
@admin_required
def retire_pod(pod_id):
    pod = db.session.get(Pod, pod_id)
    if not pod:
        abort(404)

    if pod.slug == DEFAULT_POD_SLUG:
        flash("The default pod cannot be retired.")
        return redirect(url_for("pods", pod_id=pod_id))

    pod.is_active = False
    db.session.commit()

    if session.get("active_pod_id") == pod_id:
        session.pop("active_pod_id", None)
        session.modified = True

    flash(f"Retired pod '{pod.name}'.")
    return redirect(url_for("pods"))


@app.route("/pods/<int:pod_id>/restore", methods=["POST"])
@admin_required
def restore_pod(pod_id):
    pod = db.session.get(Pod, pod_id)
    if not pod:
        abort(404)

    pod.is_active = True
    db.session.commit()
    flash(f"Restored pod '{pod.name}'.")
    return redirect(url_for("pods", pod_id=pod.id))


@app.route("/pods/<int:pod_id>/delete", methods=["POST"])
@admin_required
def delete_pod(pod_id):
    pod = db.session.get(Pod, pod_id)
    if not pod:
        abort(404)

    if pod.slug == DEFAULT_POD_SLUG:
        flash("The default pod cannot be deleted.")
        return redirect(url_for("pods", pod_id=pod_id))

    games_count = Game.query.filter_by(pod_id=pod_id).count()
    if games_count > 0:
        flash("Cannot delete pod with recorded games. Retire it instead.")
        return redirect(url_for("pods", pod_id=pod_id))

    PodMembership.query.filter_by(pod_id=pod_id).delete()

    if session.get("active_pod_id") == pod_id:
        session.pop("active_pod_id", None)
        session.modified = True

    db.session.delete(pod)
    db.session.commit()
    flash("Pod deleted.")
    return redirect(url_for("pods"))


@app.route("/pods/<int:pod_id>/members", methods=["POST"])
@login_required
def add_pod_member(pod_id):
    me = get_current_user()
    if not can_manage_pod(me, pod_id):
        abort(403)

    pod = db.session.get(Pod, pod_id)
    if not pod or not pod.is_active:
        abort(404)

    player_id = request.form.get("player_id", type=int)
    role = (request.form.get("role") or "member").strip().lower()
    if role not in {"member", "podmaster"}:
        role = "member"

    player = db.session.get(Player, player_id)
    if not player:
        flash("Player not found.")
        return redirect(url_for("pods", pod_id=pod_id))

    if role == "podmaster" and not me.is_admin:
        role = "member"

    ensure_membership(pod_id, player_id, role=role)
    db.session.commit()
    flash(f"Added {player.name} to {pod.name}.")
    return redirect(url_for("pods", pod_id=pod_id))


@app.route("/pods/<int:pod_id>/members/<int:player_id>/role", methods=["POST"])
@login_required
def update_pod_member_role(pod_id, player_id):
    me = get_current_user()
    if not can_manage_pod(me, pod_id):
        abort(403)

    membership = PodMembership.query.filter_by(pod_id=pod_id, player_id=player_id).first()
    if not membership:
        abort(404)

    role = (request.form.get("role") or "member").strip().lower()
    if role not in {"member", "podmaster"}:
        role = "member"

    if role == "podmaster" and not me.is_admin:
        abort(403)

    membership.role = role
    db.session.commit()
    flash("Member role updated.")
    return redirect(url_for("pods", pod_id=pod_id))


@app.route("/pods/<int:pod_id>/members/<int:player_id>/remove", methods=["POST"])
@login_required
def remove_pod_member(pod_id, player_id):
    me = get_current_user()
    if not can_manage_pod(me, pod_id):
        abort(403)

    membership = PodMembership.query.filter_by(pod_id=pod_id, player_id=player_id).first()
    if not membership:
        abort(404)

    # Keep at least one podmaster in a pod if possible
    if membership.role == "podmaster":
        podmasters_left = PodMembership.query.filter_by(pod_id=pod_id, role="podmaster").count()
        if podmasters_left <= 1 and not me.is_admin:
            flash("At least one podmaster must remain. Ask an admin.")
            return redirect(url_for("pods", pod_id=pod_id))

    db.session.delete(membership)
    db.session.commit()

    if session.get("active_pod_id") == pod_id:
        session.pop("active_pod_id", None)
        session.modified = True

    flash("Member removed from pod.")
    return redirect(url_for("pods", pod_id=pod_id))


@app.route("/delete_deck/<int:deck_id>", methods=["POST"])
def delete_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    used = GameParticipant.query.filter_by(deck_id=deck_id).count()
    if used > 0:
        flash("Can't delete this deck: it has been used in recorded games.")
        return redirect(url_for("deck_detail", deck_id=deck_id))

    db.session.delete(deck)
    db.session.commit()
    flash("Deck deleted.")
    return redirect(url_for("decks"))


@app.route("/deck/<int:deck_id>/retire", methods=["POST"])
def retire_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    deck.retired = True
    deck.planned = False
    db.session.commit()
    flash(f"Retired deck: {deck.name}")
    return redirect(url_for("decks"))


@app.route("/deck/<int:deck_id>/unretire", methods=["POST"])
def unretire_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    deck.retired = False
    db.session.commit()
    flash(f"Unretired deck: {deck.name}")
    return redirect(url_for("decks"))


@app.route("/deck/<int:deck_id>/plan", methods=["POST"])
def plan_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    deck.planned = True
    deck.retired = False
    db.session.commit()
    flash(f"Set deck as planned: {deck.name}")
    return redirect(url_for("decks"))


@app.route("/deck/<int:deck_id>/unplan", methods=["POST"])
def unplan_deck(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    deck.planned = False
    db.session.commit()
    flash(f"Deck is now active: {deck.name}")
    return redirect(url_for("decks"))


@app.route("/delete_player/<int:player_id>", methods=["POST"])
def delete_player(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        flash("Player not found.")
        return redirect(url_for("players"))

    # Never allow deleting a user-linked player through this route
    if player.user_id is not None:
        flash("Can't delete a user-linked player.")
        return redirect(url_for("players"))

    played = GameParticipant.query.filter_by(player_id=player_id).count()
    won = Game.query.filter_by(winner_id=player_id).count()
    started = Game.query.filter_by(starting_player_id=player_id).count()
    if played > 0 or won > 0 or started > 0:
        flash("Can't delete this player: they appear in recorded games.")
        return redirect(url_for("players"))

    # If player has decks, only delete if those decks aren't used in games
    for d in player.decks:
        used = GameParticipant.query.filter_by(deck_id=d.id).count()
        if used > 0:
            flash(f"Can't delete {player.name}: deck '{d.name}' has recorded games.")
            return redirect(url_for("players"))

    # Safe to delete decks first, then player
    for d in list(player.decks):
        db.session.delete(d)

    # Remove pod memberships before deleting the player to satisfy FK constraints
    PodMembership.query.filter_by(player_id=player_id).delete()

    db.session.delete(player)
    db.session.commit()
    flash("Player deleted.")
    return redirect(url_for("players"))


@app.route("/games")
def games():
    game_q, scope, active_pod = game_query_for_scope()
    # --------
    # Filters (GET)
    # --------
    player_id = request.args.get("player_id", type=int)
    deck_id = request.args.get("deck_id", type=int)
    winner_id = request.args.get("winner_id", type=int)

    date_from_raw = request.args.get("date_from", "").strip()
    date_to_raw = request.args.get("date_to", "").strip()

    min_players = request.args.get("min_players", type=int)
    max_players = request.args.get("max_players", type=int)

    # Pagination
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)
    per_page = max(10, min(per_page, 100))  # clamp

    date_from = None
    date_to = None
    try:
        if date_from_raw:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
    except ValueError:
        date_from = None

    try:
        if date_to_raw:
            # inclusive end-of-day
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d")
            date_to = date_to.replace(hour=23, minute=59, second=59)
    except ValueError:
        date_to = None

    # --------
    # Base query
    # --------
    q = game_q

    if winner_id:
        q = q.filter(Game.winner_id == winner_id)

    if date_from:
        q = q.filter(Game.date >= date_from)

    if date_to:
        q = q.filter(Game.date <= date_to)

    # Filter by participant player/deck using EXISTS-style joins
    # (join once per filter; distinct later)
    if player_id:
        gp_player = aliased(GameParticipant)
        q = q.join(gp_player, gp_player.game_id == Game.id).filter(gp_player.player_id == player_id)

    if deck_id:
        gp_deck = aliased(GameParticipant)
        q = q.join(gp_deck, gp_deck.game_id == Game.id).filter(gp_deck.deck_id == deck_id)

    # Player count filters (HAVING)
    if min_players or max_players:
        gp_count = aliased(GameParticipant)
        q = q.join(gp_count, gp_count.game_id == Game.id).group_by(Game.id)

        if min_players:
            q = q.having(func.count(gp_count.id) >= min_players)
        if max_players:
            q = q.having(func.count(gp_count.id) <= max_players)

    # Ensure no duplicates when joining for filters
    q = q.distinct().order_by(Game.date.desc())

    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    games_page = pagination.items

    # Pull participants for only the games on this page (fast)
    game_ids = [g.id for g in games_page]
    parts = (
        GameParticipant.query
        .filter(GameParticipant.game_id.in_(game_ids if game_ids else [-1]))
        .all()
    )

    game_parts = {}
    game_salt_stats = {}
    for gp in parts:
        gp.parsed_flags = participant_flags_snapshot(gp)
        game_parts.setdefault(gp.game_id, []).append(gp)

        stats = game_salt_stats.setdefault(gp.game_id, {"salted_players": 0, "participants": 0, "salt_clicks": 0})
        stats["participants"] += 1
        salt_count = participant_salt_count(gp.parsed_flags)
        if salt_count > 0:
            stats["salted_players"] += 1
        stats["salt_clicks"] = int(stats.get("salt_clicks", 0)) + salt_count

    for g in games_page:
        game_salt_stats.setdefault(g.id, {"salted_players": 0, "participants": 0, "salt_clicks": 0})

    # Dropdown data
    players = Player.query.order_by(Player.name.asc()).all()
    decks = Deck.query.order_by(Deck.name.asc()).all()

    # Optional: show counts in UI (per game id)
    counts = (
        db.session.query(GameParticipant.game_id, func.count(GameParticipant.id))
        .filter(GameParticipant.game_id.in_(game_ids if game_ids else [-1]))
        .group_by(GameParticipant.game_id)
        .all()
    )
    player_counts = {gid: c for gid, c in counts}

    # --------
    # Stats panel — aggregates over the full filtered set
    # --------
    all_game_ids = [row[0] for row in q.with_entities(Game.id).all()]
    _ids = all_game_ids if all_game_ids else [-1]

    avg_turns_val = db.session.query(func.avg(Game.ending_turn)).filter(
        Game.id.in_(_ids),
        Game.ending_turn.isnot(None),
    ).scalar()

    avg_duration_val = db.session.query(func.avg(Game.duration_seconds)).filter(
        Game.id.in_(_ids),
        Game.duration_seconds.isnot(None),
    ).scalar()

    win_type_rows = (
        db.session.query(Game.win_type, func.count(Game.id))
        .filter(Game.id.in_(_ids))
        .group_by(Game.win_type)
        .all()
    )
    win_type_counts = {(r[0] or "unknown"): r[1] for r in win_type_rows}
    win_type_sorted = sorted(win_type_counts.items(), key=lambda x: x[1], reverse=True)

    color_win_rows = (
        db.session.query(Deck.color_identity, func.count(Game.id))
        .join(GameParticipant, GameParticipant.deck_id == Deck.id)
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(
            Game.id.in_(_ids),
            Game.winner_id == GameParticipant.player_id,
            Deck.color_identity.isnot(None),
            Deck.color_identity != "",
        )
        .group_by(Deck.color_identity)
        .order_by(func.count(Game.id).desc())
        .limit(8)
        .all()
    )
    color_wins = [{"identity": r[0], "wins": r[1]} for r in color_win_rows]

    timeline_rows = (
        db.session.query(
            func.strftime("%Y-%m", Game.date),
            func.count(Game.id),
        )
        .filter(Game.id.in_(_ids))
        .group_by(func.strftime("%Y-%m", Game.date))
        .order_by(func.strftime("%Y-%m", Game.date))
        .all()
    )
    activity_timeline = [{"month": r[0], "count": r[1]} for r in timeline_rows]

    games_stats = {
        "total": len(all_game_ids),
        "avg_turns": round(avg_turns_val, 1) if avg_turns_val else None,
        "avg_duration": int(avg_duration_val) if avg_duration_val else None,
        "win_types": win_type_sorted,
        "win_types_total": sum(win_type_counts.values()) if win_type_counts else 0,
        "color_wins": color_wins,
        "activity_timeline": activity_timeline,
    }

    return render_template(
        "games.html",
        games=games_page,
        game_parts=game_parts,
        player_counts=player_counts,
        game_salt_stats=game_salt_stats,
        players=players,
        decks=decks,
        pagination=pagination,
        selected_player_id=player_id,
        selected_deck_id=deck_id,
        selected_winner_id=winner_id,
        date_from=date_from_raw,
        date_to=date_to_raw,
        min_players=min_players,
        max_players=max_players,
        per_page=per_page,
        scope=scope,
        active_pod=active_pod,
        games_stats=games_stats,
    )

@app.route("/games/<int:game_id>")
def game_detail(game_id):
    g = db.session.get(Game, game_id)
    if not g:
        abort(404)

    parts = GameParticipant.query.filter_by(game_id=game_id).all()

    salted_players = 0
    total_salt_clicks = 0
    for gp in parts:
        gp.parsed_flags = participant_flags_snapshot(gp)
        gp.turn_stats = parse_participant_turn_stats(gp.flags_json)
        salt_count = participant_salt_count(gp.parsed_flags)
        gp.salt_count = salt_count
        total_salt_clicks += salt_count
        if salt_count > 0:
            salted_players += 1

    g.salted_players = salted_players
    g.total_salt_clicks = total_salt_clicks

    # Nice for display: show winner first (optional)
    parts_sorted = sorted(parts, key=lambda p: (0 if p.player_id == g.winner_id else 1, p.player.name.lower()))

    return render_template("game_detail.html", game=g, parts=parts_sorted)

@app.route("/games/<int:game_id>/delete", methods=["POST"])
@admin_required
def delete_game(game_id):
    g = db.session.get(Game, game_id)
    if not g:
        abort(404)

    # delete participants first (FKs)
    GameParticipant.query.filter_by(game_id=game_id).delete()
    db.session.delete(g)
    db.session.commit()

    flash(f"Deleted Game #{game_id}.")
    return redirect(url_for("games"))


@app.route("/players")
def players():
    players_list = Player.query.order_by(Player.name.asc()).all()

    player_can_delete = {}
    player_stats = {}
    for p in players_list:
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        won = Game.query.filter_by(winner_id=p.id).count()

        deck_count = Deck.query.filter_by(player_id=p.id).count()
        winrate = round((won / played) * 100, 1) if played else 0.0
        joined_on = p.user.created_at if p.user else None

        player_stats[p.id] = {
            "winrate": winrate,
            "deck_count": deck_count,
            "joined_on": joined_on,
            "won": won,
            "played": played,
        }

        deck_used = (
            db.session.query(GameParticipant.id)
            .join(Deck, GameParticipant.deck_id == Deck.id)
            .filter(Deck.player_id == p.id)
            .first()
            is not None
        )

        # Only deletable if:
        # - not linked to a user
        # - not in any game (played or won)
        # - none of their decks are used
        player_can_delete[p.id] = (p.user_id is None and played == 0 and won == 0 and not deck_used)

    return render_template(
        "players.html",
        players=players_list,
        player_can_delete=player_can_delete,
        player_stats=player_stats,
    )


@app.route("/player/<int:player_id>")
def player_detail(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        abort(404)

    decks = Deck.query.filter_by(player_id=player.id).order_by(Deck.name.asc()).all()

    games_played = GameParticipant.query.filter_by(player_id=player.id).count()
    games_won = Game.query.filter_by(winner_id=player.id).count()
    games_started = Game.query.filter_by(starting_player_id=player.id).count()
    winrate = round((games_won / games_played) * 100, 1) if games_played else 0.0

    deck_stats = {}
    for d in decks:
        deck_wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id)
            .count()
        )
        deck_games = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_losses = max(0, deck_games - deck_wins)
        deck_winrate = round((deck_wins / deck_games) * 100, 1) if deck_games else 0.0

        deck_stats[d.id] = {
            "wins": deck_wins,
            "losses": deck_losses,
            "games": deck_games,
            "winrate": deck_winrate,
        }

    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.player_id == player.id)
        .order_by(Game.date.desc())
        .all()
    )

    recent_games = []
    for gp in participations:
        game = gp.game
        participant_count = GameParticipant.query.filter_by(game_id=game.id).count()
        recent_games.append(
            {
                "game_id": game.id,
                "date": game.date,
                "won": game.winner_id == player.id,
                "deck": gp.deck,
                "participant_count": participant_count,
            }
        )

    return render_template(
        "player_detail.html",
        player=player,
        decks=decks,
        deck_stats=deck_stats,
        recent_games=recent_games,
        games_played=games_played,
        games_won=games_won,
        games_started=games_started,
        winrate=winrate,
    )


@app.route("/compare")
def compare_players():
    a_id = request.args.get("a", type=int)
    b_id = request.args.get("b", type=int)
    if not a_id or not b_id or a_id == b_id:
        flash("Select two different players to compare.", "warning")
        return redirect(url_for("players"))

    player_a = db.session.get(Player, a_id)
    player_b = db.session.get(Player, b_id)
    if not player_a or not player_b:
        abort(404)

    def _stats(p):
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        won = Game.query.filter_by(winner_id=p.id).count()
        deck_count = Deck.query.filter_by(player_id=p.id).count()
        winrate = round((won / played) * 100, 1) if played else 0.0
        return {"played": played, "won": won, "deck_count": deck_count, "winrate": winrate}

    stats_a = _stats(player_a)
    stats_b = _stats(player_b)

    # Games where both players participated
    a_game_ids = db.session.query(GameParticipant.game_id).filter_by(player_id=a_id)
    b_game_ids = db.session.query(GameParticipant.game_id).filter_by(player_id=b_id)
    shared_games_q = (
        Game.query
        .filter(Game.id.in_(a_game_ids), Game.id.in_(b_game_ids))
        .order_by(Game.date.desc())
        .all()
    )

    h2h_a_wins = sum(1 for g in shared_games_q if g.winner_id == a_id)
    h2h_b_wins = sum(1 for g in shared_games_q if g.winner_id == b_id)
    h2h_other = len(shared_games_q) - h2h_a_wins - h2h_b_wins

    shared_game_ids = [g.id for g in shared_games_q]
    _ab_parts = (
        GameParticipant.query
        .filter(
            GameParticipant.game_id.in_(shared_game_ids),
            GameParticipant.player_id.in_([a_id, b_id]),
        )
        .all()
    ) if shared_game_ids else []
    _parts_by_game: dict[int, dict[int, GameParticipant]] = {}
    for _part in _ab_parts:
        _parts_by_game.setdefault(_part.game_id, {})[_part.player_id] = _part

    _part_counts = dict(
        db.session.query(GameParticipant.game_id, func.count(GameParticipant.id))
        .filter(GameParticipant.game_id.in_(shared_game_ids))
        .group_by(GameParticipant.game_id)
        .all()
    ) if shared_game_ids else {}

    winner_ids = {g.winner_id for g in shared_games_q}
    _winners = {p.id: p for p in Player.query.filter(Player.id.in_(winner_ids)).all()} if winner_ids else {}

    shared_games = []
    for g in shared_games_q:
        gp_a = _parts_by_game.get(g.id, {}).get(a_id)
        gp_b = _parts_by_game.get(g.id, {}).get(b_id)
        winner = _winners.get(g.winner_id)
        shared_games.append({
            "game_id": g.id,
            "date": g.date,
            "winner_id": g.winner_id,
            "winner_name": winner.name if winner else "Unknown",
            "deck_a": gp_a.deck.name if gp_a and gp_a.deck else "Unknown",
            "deck_b": gp_b.deck.name if gp_b and gp_b.deck else "Unknown",
            "participant_count": _part_counts.get(g.id, 0),
        })

    return render_template(
        "compare.html",
        player_a=player_a,
        player_b=player_b,
        stats_a=stats_a,
        stats_b=stats_b,
        h2h_a_wins=h2h_a_wins,
        h2h_b_wins=h2h_b_wins,
        h2h_other=h2h_other,
        shared_games=shared_games,
    )


@app.route("/player/<int:player_id>/export")
def player_export(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        abort(404)

    decks = Deck.query.filter_by(player_id=player.id).order_by(Deck.name.asc()).all()

    games_played = GameParticipant.query.filter_by(player_id=player.id).count()
    games_won = Game.query.filter_by(winner_id=player.id).count()
    games_started = Game.query.filter_by(starting_player_id=player.id).count()
    winrate = round((games_won / games_played) * 100, 1) if games_played else 0.0

    decks_data = []
    for d in decks:
        deck_wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id)
            .count()
        )
        deck_games = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_losses = max(0, deck_games - deck_wins)
        deck_winrate = round((deck_wins / deck_games) * 100, 1) if deck_games else 0.0
        tags = {}
        try:
            tags = json.loads(d.tags_json or "{}")
        except (ValueError, TypeError):
            pass
        decks_data.append({
            "id": d.id,
            "name": d.name,
            "commander": d.commander_name or d.commander,
            "color_identity": d.color_identity,
            "retired": d.retired,
            "planned": d.planned,
            "decklist": d.decklist_text or "",
            "stats": {
                "games": deck_games,
                "wins": deck_wins,
                "losses": deck_losses,
                "winrate": deck_winrate,
            },
            "tags": tags,
        })

    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.player_id == player.id)
        .order_by(Game.date.desc())
        .all()
    )

    games_data = []
    for gp in participations:
        game = gp.game
        participant_count = GameParticipant.query.filter_by(game_id=game.id).count()
        games_data.append({
            "game_id": game.id,
            "date": game.date.isoformat() if game.date else None,
            "won": game.winner_id == player.id,
            "deck_id": gp.deck_id,
            "deck_name": gp.deck.name if gp.deck else None,
            "commander": (gp.deck.commander_name or gp.deck.commander) if gp.deck else None,
            "participant_count": participant_count,
            "win_type": canonicalize_win_type(game.win_type) if game.win_type else None,
            "salt_count": gp.salt_count,
            "mana_fucked": gp.mana_fucked,
            "misplayed": gp.misplayed,
            "life_delta": gp.life_delta_total,
        })

    payload = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "player": {
            "id": player.id,
            "name": player.name,
            "linked_account": player.user_id is not None,
        },
        "stats": {
            "games_played": games_played,
            "games_won": games_won,
            "games_started": games_started,
            "winrate": winrate,
        },
        "decks": decks_data,
        "games": games_data,
    }

    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", player.name.strip()) or f"player_{player.id}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}_profile.json"'
    }
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers=headers,
    )


@app.route("/add_player", methods=["POST"])
def add_player():
    name = request.form["name"].strip()
    if name and not Player.query.filter_by(name=name).first():
        player = Player(name=name)
        db.session.add(player)
        db.session.flush()

        default_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG).first()
        if default_pod:
            ensure_membership(default_pod.id, player.id)

        db.session.commit()
    return redirect(url_for("players"))


@app.route("/decks")
@login_required
def decks():
    u = get_current_user()

    # Owners list: admins can see/filter all, non-admins only themselves
    if u and u.is_admin:
        players_list = Player.query.order_by(Player.name.asc()).all()
    else:
        players_list = [u.player] if (u and u.player) else []

    player_id = request.args.get("player_id", type=int)
    show_retired = request.args.get("show_retired", type=int)

    q = Deck.query

    if not show_retired:
        q = q.filter(Deck.retired == False, Deck.planned == False)  # noqa: E712

    # Non-admins: force own decks (ignore player_id from query string)
    if u and not u.is_admin:
        if u.player:
            q = q.filter(Deck.player_id == u.player.id)
            player_id = u.player.id
        else:
            q = q.filter(text("1=0"))

    # Admins: allow filter by selected player
    if u and u.is_admin and player_id:
        q = q.filter(Deck.player_id == player_id)

    decks_list = q.order_by(Deck.retired.asc(), Deck.planned.asc(), Deck.name.asc()).all()

    deck_tags_cache: dict[int, dict[str, bool]] = {}
    for d in decks_list:
        get_deck_parsed_tags(d, cache=deck_tags_cache)

    # Stats
    stats = {}
    for d in decks_list:
        wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id)
            .count()
        )
        uses = GameParticipant.query.filter_by(deck_id=d.id).count()
        losses = max(0, uses - wins)
        winrate = round((wins / uses) * 100, 1) if uses else 0.0
        stats[d.id] = {"wins": wins, "uses": uses, "losses": losses, "winrate": winrate}

    deck_can_delete = {}
    deck_tags_stale = {}
    for d in decks_list:
        used = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_can_delete[d.id] = (used == 0)
        deck_tags_stale[d.id] = bool(d.decklist_text and is_deck_tags_stale(d))

    stale_deck_count = sum(1 for stale in deck_tags_stale.values() if stale)
    show_tags_refresh_banner = bool(
        u and u.is_admin and stale_deck_count > 0 and not session.get("deck_tags_banner_dismissed")
    )

    return render_template(
        "decks.html",
        decks=decks_list,
        players=players_list,
        selected_player_id=player_id,
        deck_stats=stats,
        deck_can_delete=deck_can_delete,
        deck_tags_stale=deck_tags_stale,
        stale_deck_count=stale_deck_count,
        show_tags_refresh_banner=show_tags_refresh_banner,
        deck_tags_version=DECK_TAGS_VERSION,
        show_retired=show_retired,
        is_admin=bool(u and u.is_admin),
    )


@app.route("/decks/dismiss-tags-banner", methods=["POST"])
@login_required
def dismiss_deck_tags_banner():
    u = get_current_user()
    if not (u and u.is_admin):
        return "Forbidden", 403

    session["deck_tags_banner_dismissed"] = True
    session.modified = True
    return redirect(request.form.get("next") or url_for("decks"))


@app.route("/deck/<int:deck_id>")
def deck_detail(deck_id):
    u = get_current_user()

    deck = db.session.get(Deck, deck_id)
    if not deck:
        return "Deck not found", 404

    wins = (
        GameParticipant.query.join(Game)
        .filter(GameParticipant.deck_id == deck.id, Game.winner_id == GameParticipant.player_id)
        .count()
    )

    games = GameParticipant.query.filter_by(deck_id=deck.id).count()
    losses = max(0, games - wins)
    winrate = round((wins / games) * 100, 1) if games else 0.0

    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.deck_id == deck.id)
        .order_by(Game.date.desc())
        .all()
    )

    history = []
    matchups = {}
    deck_matchups = {}

    for part in participations:
        game = part.game
        won_game = game.winner_id == part.player_id

        opponents = (
            GameParticipant.query.filter(
                GameParticipant.game_id == game.id, GameParticipant.player_id != part.player_id
            ).all()
        )

        opponent_names = []
        for o in opponents:
            name = o.player.name
            opponent_names.append(name)

            if name not in matchups:
                matchups[name] = {"wins": 0, "losses": 0}

            if won_game:
                matchups[name]["wins"] += 1
            else:
                matchups[name]["losses"] += 1

            matchup_key = o.deck_id if o.deck_id else "Unknown deck"
            opponent_deck = o.deck
            if matchup_key not in deck_matchups:
                deck_matchups[matchup_key] = {
                    "deck_id": opponent_deck.id if opponent_deck else None,
                    "deck_name": (opponent_deck.name if opponent_deck else "Unknown deck"),
                    "commander": (
                        (opponent_deck.commander_name or opponent_deck.commander)
                        if opponent_deck
                        else None
                    ),
                    "owner_name": (
                        opponent_deck.owner.name
                        if opponent_deck and opponent_deck.owner
                        else o.player.name
                    ),
                    "wins": 0,
                    "losses": 0,
                }

            if won_game:
                deck_matchups[matchup_key]["wins"] += 1
            else:
                deck_matchups[matchup_key]["losses"] += 1

        history.append(
            {"game_id": game.id, "date": game.date, "won": won_game, "opponents": opponent_names}
        )

    for name, data in matchups.items():
        total = data["wins"] + data["losses"]
        data["games"] = total
        data["winrate"] = round((data["wins"] / total) * 100, 1) if total else 0.0

    matchups = dict(sorted(matchups.items(), key=lambda x: -x[1]["games"]))

    deck_matchup_rows = []
    for matchup in deck_matchups.values():
        total = matchup["wins"] + matchup["losses"]
        matchup["games"] = total
        matchup["winrate"] = round((matchup["wins"] / total) * 100, 1) if total else 0.0
        deck_matchup_rows.append(matchup)

    deck_matchup_rows.sort(
        key=lambda row: (-row["games"], -row["winrate"], row["deck_name"].lower())
    )

    decklist_data = _load_decklist_data(deck)

    commander_bracket = compute_commander_bracket_from_text(deck.decklist_text)

    deck_tags_cache: dict[int, dict[str, bool]] = {}
    deck_tags = get_deck_parsed_tags(deck, cache=deck_tags_cache)
    deck_mechanics = derive_deck_mechanics(deck_tags)

    return render_template(
        "deck_detail.html",
        deck=deck,
        wins=wins,
        losses=losses,
        games=games,
        winrate=winrate,
        history=history,
        matchups=matchups,
        deck_matchups=deck_matchup_rows,
        decklist_data=decklist_data,
        commander_bracket=commander_bracket,
        deck_mechanics=deck_mechanics,
        is_admin=bool(u and u.is_admin),
        players=(Player.query.order_by(Player.name.asc()).all() if (u and u.is_admin) else []),
    )


@app.route("/deck/<int:deck_id>/export")
def deck_export(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return "Deck not found", 404

    decklist_data = _load_decklist_data(deck)
    export_text = (decklist_data.get("export_text") or "").strip()
    if not export_text:
        export_text = (deck.decklist_text or "").strip()

    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", deck.name.strip()) or f"deck_{deck.id}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}.txt"'
    }
    return Response(export_text + "\n", mimetype="text/plain; charset=utf-8", headers=headers)


@app.route("/deck/<int:deck_id>/edit")
@login_required
def deck_editor(deck_id):
    u = get_current_user()
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    if not u.is_admin and (not u.player or deck.player_id != u.player.id):
        flash("You don't have permission to edit this deck.")
        return redirect(url_for("deck_detail", deck_id=deck_id))

    decklist_data = _load_decklist_data(deck)

    return render_template(
        "deck_editor.html",
        deck=deck,
        decklist_data=decklist_data,
        is_admin=bool(u.is_admin),
        players=(Player.query.order_by(Player.name.asc()).all() if u.is_admin else []),
        has_ccauto=bool(CCAUTO_BASE_URL),
    )


@app.route("/add_deck", methods=["POST"])
@login_required
def add_deck():
    u = get_current_user()

    if u.is_admin:
        player_id = request.form.get("player_id", type=int)
        if not player_id:
            flash("Owner is required.")
            return redirect(url_for("decks"))
    else:
        if not u.player:
            flash("No player profile found for your account.")
            return redirect(url_for("decks"))
        player_id = u.player.id

    name = request.form.get("name", "").strip()
    commander_input = request.form.get("commander", "").strip()
    if not (name and commander_input):
        flash("Deck name and commander are required.")
        return redirect(url_for("decks"))

    try:
        raw_import, imported_from = _extract_deck_import_text()
        deck, diagnostics = _create_deck_from_payload(
            {
                "name": name,
                "commander": commander_input,
                "raw_import": raw_import,
                "imported_from": imported_from,
                "custom_commander_art_url": request.form.get("custom_commander_art_url", ""),
                "custom_card_art_url": request.form.get("custom_card_art_url", ""),
                "custom_commander_art_upload": request.files.get("custom_commander_art_file"),
                "custom_card_art_upload": request.files.get("custom_card_art_file"),
                "planned": request.form.get("planned"),
            },
            player_id=player_id,
            is_admin=bool(u.is_admin),
        )
    except DeckParserError as exc:
        flash(f"Deck setup failed: {exc}")
        return redirect(url_for("decks"))
    except DeckPayloadError as exc:
        flash(str(exc))
        return redirect(url_for("decks"))

    db.session.add(deck)
    db.session.commit()

    parsed_import = diagnostics["parsed_import"]
    commander_meta = diagnostics["commander_meta"]
    if parsed_import:
        imported_cards = _count_imported_cards(parsed_import)
        resolved_commander = diagnostics["resolved_commander"]
        commander_input = diagnostics["commander_input"]
        commander_msg = (
            f"commander resolved: {resolved_commander}" if resolved_commander else "commander unresolved"
        )
        warnings = []
        if resolved_commander and commander_input and resolved_commander.lower() != commander_input.lower():
            warnings.append(f"manual commander '{commander_input}' overridden")
        if not commander_meta["lookup_ok"]:
            warnings.append("Scryfall lookup failed for one or more commanders")

        warning_msg = f"; warnings: {', '.join(warnings)}" if warnings else ""
        flash(
            f"Deck added. {imported_cards} cards imported from {diagnostics['imported_from']}; "
            f"{commander_msg}{warning_msg}."
        )
    else:
        flash("Deck added.")
    flash_unresolved_tag_warning(diagnostics["tag_diagnostics"])
    return redirect(url_for("decks"))


@app.route("/api/card-art")
def api_card_art():
    card_name = (request.args.get("name") or "").strip()
    if not card_name:
        return jsonify({"image": None, "failure_reason": "not_found", "error_code": "not_found"}), 404

    image, failure_reason, status_code = cache_card_art_by_name(card_name)
    payload = {"image": image, "failure_reason": failure_reason, "error_code": failure_reason}
    return jsonify(payload), status_code


@app.route("/api/gallery-image")
def api_gallery_image():
    """Proxy gallery images to the browser (internal Docker URL not reachable from browser)."""
    path_param = (request.args.get("path") or "").strip()
    if not path_param or not CCAUTO_BASE_URL:
        return "Not found", 404
    try:
        r = requests.get(f"{CCAUTO_BASE_URL}{path_param}", timeout=10, stream=True)
        if r.status_code != 200:
            return "Not found", 404
        content_type = r.headers.get("Content-Type", "image/jpeg")
        return r.content, 200, {"Content-Type": content_type, "Cache-Control": "public, max-age=86400"}
    except requests.RequestException:
        return "Not found", 404


@app.route("/api/ccauto/sets")
def api_ccauto_sets():
    """List custom card sets from the cc-auto gallery. Returns [] if unavailable."""
    if not CCAUTO_BASE_URL:
        return jsonify([])
    try:
        r = requests.get(f"{CCAUTO_BASE_URL}/api/sets", timeout=5)
        if r.status_code == 200:
            payload = r.json()
            # cc-auto wraps lists as {object: 'list', data: [...]}
            if isinstance(payload, dict) and "data" in payload:
                payload = payload["data"]
            return jsonify(payload)
    except requests.RequestException:
        pass
    return jsonify([])


@app.route("/api/ccauto/sets/<set_name>")
def api_ccauto_set_cards(set_name):
    """Proxy a cc-auto set's card list ({name, image_uris, …} per card)."""
    if not CCAUTO_BASE_URL:
        return jsonify({"error": "Custom gallery not configured"}), 404
    try:
        r = requests.get(f"{CCAUTO_BASE_URL}/api/sets/{quote(set_name)}/cards", timeout=10)
        if r.status_code == 404:
            return jsonify({"error": "Set not found"}), 404
        if r.status_code != 200:
            return jsonify({"error": "Gallery error"}), 502
        payload = r.json()
        # cc-auto wraps lists as {object: 'list', data: [...]}
        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        if not isinstance(payload, list):
            return jsonify({"error": "Unexpected response shape"}), 502
        # Rewrite image paths so the browser can reach them via our proxy
        rewritten = [_rewrite_gallery_image_uris(c) if isinstance(c, dict) else c for c in payload]
        return jsonify(rewritten)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/cards/autocomplete")
def api_cards_autocomplete():
    """Merge Scryfall + gallery autocomplete results.

    Optional ?source=scryfall|custom|all (default: all).
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"data": []})

    source = (request.args.get("source") or "all").strip().lower()
    results = []
    seen = set()

    # Scryfall
    if source in ("all", "scryfall"):
        try:
            r = requests.get(
                f"https://api.scryfall.com/cards/autocomplete?q={quote(q)}", timeout=5
            )
            if r.status_code == 200:
                for name in r.json().get("data") or []:
                    key = name.lower()
                    if key not in seen:
                        seen.add(key)
                        results.append(name)
        except requests.RequestException:
            pass

    # Gallery
    if source in ("all", "custom") and CCAUTO_BASE_URL:
        try:
            r = requests.get(
                f"{CCAUTO_BASE_URL}/api/cards/autocomplete?q={quote(q)}", timeout=5
            )
            if r.status_code == 200:
                for name in r.json().get("data") or []:
                    key = name.lower()
                    if key not in seen:
                        seen.add(key)
                        results.append(name)
        except requests.RequestException:
            pass

    return jsonify({"data": results})


@app.route("/api/cards/named")
def api_cards_named():
    """Try Scryfall then gallery for exact card lookup, rewriting gallery image URLs."""
    exact = (request.args.get("exact") or "").strip()
    if not exact:
        return jsonify({"error": "Missing 'exact' parameter"}), 400

    card = scryfall_named_exact(exact)
    if card:
        return jsonify(card)

    if CCAUTO_BASE_URL:
        card = custommtg_gallery_named_exact(exact)
        if card:
            return jsonify(_rewrite_gallery_image_uris(card))

    return jsonify({"error": "Card not found"}), 404


@app.route("/deck/<int:deck_id>/retag", methods=["POST"])
@login_required
def retag_deck(deck_id):
    u = get_current_user()
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return "Deck not found", 404

    if not (u and u.is_admin) and (not u or not u.player or deck.player_id != u.player.id):
        return "Forbidden", 403

    next_url = request.form.get("next") or request.referrer or url_for("decks")

    if not (deck.decklist_text or "").strip():
        flash("Cannot retag deck without a decklist.")
        return redirect(next_url)

    try:
        tags, tag_diagnostics = compute_deck_tags_from_text(deck.decklist_text)
    except DeckParserError as exc:
        flash(f"Retag failed: {exc}")
        return redirect(next_url)

    apply_deck_tags(deck, tags)
    db.session.commit()
    flash(f"Retagged deck: {deck.name}")
    flash_unresolved_tag_warning(tag_diagnostics)
    return redirect(next_url)


@app.route("/api/commander-bracket", methods=["POST"])
def api_commander_bracket():
    payload = request.get_json(silent=True) or {}

    cards = payload.get("cards")
    if isinstance(cards, list):
        return jsonify(compute_commander_bracket([str(card) for card in cards]))

    decklist_text = payload.get("decklist_text")
    if isinstance(decklist_text, str):
        return jsonify(compute_commander_bracket_from_text(decklist_text))

    return jsonify({"error": "Provide either 'cards' (array) or 'decklist_text' (string)."}), 400


@app.route("/api/deck-import-preview", methods=["POST"])
@login_required
def deck_import_preview():
    payload = request.get_json(silent=True) or {}
    raw_import = (payload.get("raw_import") or "").strip()
    if not raw_import:
        return jsonify({"commanders": [], "commander": None})

    try:
        parsed = parse_deck_input(raw_import)
    except DeckParserError as exc:
        return jsonify({"error": str(exc)}), 400

    commanders = [name for name in (parsed.get("commanders") or []) if isinstance(name, str) and name.strip()]
    commander = parsed.get("commander") if isinstance(parsed.get("commander"), str) else None
    primary = commanders[0] if commanders else None
    partner = commanders[1] if len(commanders) > 1 else None
    return jsonify(
        {
            "commander": commander,
            "commanders": commanders,
            "primary_commander": primary,
            "partner_commander": partner,
        }
    )


@app.route("/deck/<int:deck_id>/update", methods=["POST"])
@login_required
def update_deck(deck_id):
    u = get_current_user()
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(request.form.get("next") or url_for("decks"))

    if not u.is_admin and (not u.player or deck.player_id != u.player.id):
        flash("You don't have permission to edit this deck.")
        return redirect(request.form.get("next") or url_for("decks"))

    old_name = deck.name
    old_commander = deck.commander
    old_player_id = deck.player_id
    old_retired = deck.retired
    old_planned = deck.planned
    old_decklist_text = deck.decklist_text
    old_commander_name = deck.commander_name
    old_commander_scryfall_id = deck.commander_scryfall_id
    old_commander_art_crop_url = deck.commander_art_crop_url
    old_commander_local_art_crop = deck.commander_local_art_crop
    old_commander_local_art_custom = deck.commander_local_art_custom
    old_custom_commander_art_url = deck.custom_commander_art_url
    old_custom_card_art_url = deck.custom_card_art_url
    old_custom_card_art_local = deck.custom_card_art_local
    old_color_identity = deck.color_identity
    old_tags_json = deck.tags_json
    old_tags_version = deck.tags_version
    old_tags_computed_at = deck.tags_computed_at

    name = request.form.get("name", "").strip()
    commander_input = request.form.get("commander", "").strip()
    if not name:
        flash("Deck name is required.")
        return redirect(request.form.get("next") or url_for("decks"))
    if not commander_input:
        flash("Commander is required.")
        return redirect(request.form.get("next") or url_for("decks"))

    try:
        raw_import, imported_from = _extract_deck_import_text()
        deck, diagnostics = _update_deck_from_payload(
            deck,
            {
                "name": name,
                "commander": commander_input,
                "raw_import": raw_import,
                "imported_from": "text" if imported_from == "pasted text" else imported_from,
                "custom_commander_art_url": request.form.get("custom_commander_art_url", ""),
                "custom_card_art_url": request.form.get("custom_card_art_url", ""),
                "custom_commander_art_upload": request.files.get("custom_commander_art_file"),
                "custom_card_art_upload": request.files.get("custom_card_art_file"),
                "player_id": request.form.get("player_id", type=int),
                "retired": request.form.get("retired"),
                "planned": request.form.get("planned"),
            },
            is_admin=bool(u.is_admin),
            allow_owner_update=True,
            require_commander_input=True,
        )
    except DeckParserError as exc:
        flash(f"Deck setup failed: {exc}")
        return redirect(request.form.get("next") or url_for("decks"))
    except DeckPayloadError as exc:
        flash(str(exc))
        return redirect(request.form.get("next") or url_for("decks"))

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()

        deck.name = old_name
        deck.commander = old_commander
        deck.player_id = old_player_id
        deck.retired = old_retired
        deck.planned = old_planned
        deck.decklist_text = old_decklist_text
        deck.commander_name = old_commander_name
        deck.commander_scryfall_id = old_commander_scryfall_id
        deck.commander_art_crop_url = old_commander_art_crop_url
        deck.commander_local_art_crop = old_commander_local_art_crop
        deck.commander_local_art_custom = old_commander_local_art_custom
        deck.custom_commander_art_url = old_custom_commander_art_url
        deck.custom_card_art_url = old_custom_card_art_url
        deck.custom_card_art_local = old_custom_card_art_local
        deck.color_identity = old_color_identity
        deck.tags_json = old_tags_json
        deck.tags_version = old_tags_version
        deck.tags_computed_at = old_tags_computed_at

        flash(f"Failed to update deck: {exc}")
        return redirect(request.form.get("next") or url_for("decks"))

    parsed_import = diagnostics["parsed_import"]
    commander_meta = diagnostics["commander_meta"]
    if parsed_import:
        imported_cards = _count_imported_cards(parsed_import)
        resolved_commander = diagnostics["resolved_commander"]
        commander_input = diagnostics["commander_input"]
        commander_msg = (
            f"commander resolved: {resolved_commander}" if resolved_commander else "commander unresolved"
        )
        warnings = []
        if resolved_commander and commander_input and resolved_commander.lower() != commander_input.lower():
            warnings.append(f"manual commander '{commander_input}' overridden")
        if not commander_meta["lookup_ok"]:
            warnings.append("Scryfall lookup failed for one or more commanders")

        warning_msg = f"; warnings: {', '.join(warnings)}" if warnings else ""
        flash(
            f"Updated deck: {deck.name}. {imported_cards} cards imported from {diagnostics['imported_from']}; "
            f"{commander_msg}{warning_msg}."
        )
    else:
        flash(f"Updated deck: {deck.name}")
    flash_unresolved_tag_warning(diagnostics["tag_diagnostics"])
    return redirect(request.form.get("next") or url_for("decks"))


@app.route("/deck/<int:deck_id>/remove-decklist", methods=["POST"])
@login_required
def remove_deck_decklist(deck_id):
    u = get_current_user()
    deck = db.session.get(Deck, deck_id)
    if not deck:
        flash("Deck not found.")
        return redirect(url_for("decks"))

    if not u.is_admin and (not u.player or deck.player_id != u.player.id):
        flash("You don't have permission to edit this deck.")
        return redirect(url_for("deck_detail", deck_id=deck_id))

    deck.decklist_text = None
    deck.tags_json = "{}"
    deck.tags_version = None
    deck.tags_computed_at = None

    try:
        db.session.commit()
        flash("Decklist removed.")
    except Exception as exc:
        db.session.rollback()
        flash(f"Failed to remove decklist: {exc}")

    return redirect(url_for("deck_detail", deck_id=deck_id))


@app.route("/add_game")
def add_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = (
            Deck.query.filter_by(player_id=p.id, retired=False, planned=False).order_by(Deck.name.asc()).all()
        )
        decks_by_player[str(p.id)] = [
            {
                "id": d.id,
                "name": d.name,
                "art": d.commander_art_url,
                "art_scale": d.commander_art_scale,
            }
            for d in active_decks
        ]
    decks_json = json.dumps(decks_by_player)
    return render_template("add_game.html", players=players, decks_json=decks_json)


@app.route("/play_game")
def play_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = (
            Deck.query.filter_by(player_id=p.id, retired=False, planned=False)
            .order_by(Deck.name.asc())
            .all()
        )
        decks_by_player[str(p.id)] = [
            {
                "id": d.id,
                "name": d.name,
                "art": d.commander_art_url,
                "art_scale": d.commander_art_scale,
                "owner_name": p.name,
            }
            for d in active_decks
        ]

    decks_json = json.dumps(decks_by_player)
    return render_template("play_game.html", players=players, decks_json=decks_json)


@app.route("/start_game", methods=["POST"])
@login_required
def start_game():
    participants = []
    seen = set()

    for i in range(1, 7):
        p_id = request.form.get(f"player{i}")
        d_id = request.form.get(f"deck{i}")
        if p_id and d_id:
            try:
                p_id = int(p_id)
                d_id = int(d_id)
            except (TypeError, ValueError):
                return "Invalid player or deck id", 400

            if p_id in seen:
                return "Duplicate players not allowed", 400
            seen.add(p_id)

            borrowing = request.form.get(f"borrow{i}") == "1"
            deck = db.session.get(Deck, d_id)
            if not deck or deck.retired or deck.planned:
                return "Invalid deck", 400
            if not borrowing and deck.player_id != p_id:
                return "Invalid deck for player", 400

            player = db.session.get(Player, p_id)
            if not player:
                return "Invalid player", 400

            deck_tags = get_deck_parsed_tags(deck)
            deck_mechanics = derive_deck_mechanics(deck_tags)

            participants.append({
                "player_id": p_id,
                "deck_id": d_id,
                "seat_position": len(participants) + 1,
                "player_name": player.name,
                "deck_name": deck.name,
                "commander_art": deck.commander_art_url,
                "commander_art_scale": deck.commander_art_scale,
                "mechanics": deck_mechanics,
            })

    if len(participants) < 2:
        return "Need at least 2 players", 400

    starting_player = request.form.get("starting_player", type=int)
    if not starting_player:
        return "Starting player required", 400

    if starting_player not in seen:
        return "Starting player must be a participant", 400

    timer_mode = (request.form.get("timer_mode") or "off").strip().lower()
    allowed_timer_modes = {"off", "chess_clock", "turn_timer"}
    if timer_mode not in allowed_timer_modes:
        return "Invalid timer mode", 400

    timer_config = {"mode": timer_mode}

    if timer_mode == "off":
        if (
            (request.form.get("timer_minutes_per_player") or "").strip()
            or (request.form.get("timer_increment_seconds") or "").strip()
            or (request.form.get("timer_seconds_per_turn") or "").strip()
        ):
            return "Timer inputs are not allowed when timer mode is off", 400

    elif timer_mode == "chess_clock":
        minutes_raw = (request.form.get("timer_minutes_per_player") or "").strip()
        increment_raw = (request.form.get("timer_increment_seconds") or "").strip()
        rope_raw = (request.form.get("timer_seconds_per_turn") or "").strip()

        if rope_raw:
            return "Turn timer value is not allowed in chess clock mode", 400
        if not minutes_raw:
            return "Minutes per player is required for chess clock mode", 400

        try:
            minutes_per_player = int(minutes_raw)
        except ValueError:
            return "Minutes per player must be an integer", 400

        if minutes_per_player < 1 or minutes_per_player > 180:
            return "Minutes per player must be between 1 and 180", 400

        increment_seconds = 0
        if increment_raw:
            try:
                increment_seconds = int(increment_raw)
            except ValueError:
                return "Increment seconds must be an integer", 400
            if increment_seconds < 0 or increment_seconds > 300:
                return "Increment seconds must be between 0 and 300", 400

        timer_config = {
            "mode": "chess_clock",
            "minutes_per_player": minutes_per_player,
            "increment_seconds": increment_seconds,
        }

    elif timer_mode == "turn_timer":
        seconds_raw = (request.form.get("timer_seconds_per_turn") or "").strip()
        minutes_raw = (request.form.get("timer_minutes_per_player") or "").strip()
        increment_raw = (request.form.get("timer_increment_seconds") or "").strip()

        if minutes_raw or increment_raw:
            return "Chess clock values are not allowed in turn timer mode", 400
        if not seconds_raw:
            return "Seconds per turn is required for turn timer mode", 400

        try:
            seconds_per_turn = int(seconds_raw)
        except ValueError:
            return "Seconds per turn must be an integer", 400

        if seconds_per_turn < 5 or seconds_per_turn > 3600:
            return "Seconds per turn must be between 5 and 3600", 400

        timer_config = {
            "mode": "turn_timer",
            "seconds_per_turn": seconds_per_turn,
        }

    session["game_participants"] = participants
    session["active_player_id"] = starting_player
    session["turn_number"] = 1
    session["timer_config"] = timer_config
    session["game_started_at"] = int(datetime.utcnow().timestamp())
    session.modified = True

    # Create ActiveGame record for multiplayer sync
    try:
        game_token = uuid4().hex
        initial_state = {
            "version": 0,
            "life": {str(p["player_id"]): 40 for p in participants},
            "flags": {
                str(p["player_id"]): {"mana_fucked": False, "misplayed": False, "salt_count": 0}
                for p in participants
            },
            "card_state": {
                str(p["player_id"]): {"statuses": {}, "counters": {}, "commander_damage": {}}
                for p in participants
            },
            "turn": 1,
            "active_player_id": starting_player,
        }
        active_pod = get_active_pod()
        active_game_rec = ActiveGame(
            token=game_token,
            host_user_id=session["user_id"],
            pod_id=active_pod.id if active_pod else None,
            participants_json=json.dumps(participants),
            state_json=json.dumps(initial_state),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.session.add(active_game_rec)
        db.session.commit()
        session["game_token"] = game_token
        session.modified = True
    except Exception:
        pass  # graceful degradation: game works without sync

    return redirect(url_for("life_counter"))



@app.route("/cancel_game", methods=["POST"])
@login_required
def cancel_game():
    # Delete ActiveGame record if present
    game_token = session.pop("game_token", None)
    if game_token:
        try:
            active_game_rec = ActiveGame.query.filter_by(token=game_token).first()
            if active_game_rec:
                db.session.delete(active_game_rec)
                db.session.commit()
        except Exception:
            pass
    # Drop the active game from session
    session.pop("game_participants", None)
    session.pop("active_player_id", None)
    session.pop("timer_config", None)
    session.pop("turn_number", None)
    flash("Game cancelled.")
    return redirect(url_for("play_game"))


@app.route("/life_counter")
@login_required
def life_counter():
    participants = session.get("game_participants")
    if not participants or len(participants) < 2:
        flash("No active game. Please start a new game.")
        return redirect(url_for("play_game"))

    colors = ["--blue", "--red", "--green", "--purple", "--orange", "--yellow"]

    active_mechanics = {
        "monarch": False,
        "initiative": False,
        "citys_blessing": False,
        "poison": False,
    }

    deck_tags_cache: dict[int, dict[str, bool]] = {}
    for i, p in enumerate(participants, 1):
        p["index"] = i
        p["color"] = colors[(i - 1) % len(colors)]

        deck = db.session.get(Deck, p["deck_id"])
        p["commander_art"] = deck.commander_art_url if deck else None
        p["commander_art_scale"] = deck.commander_art_scale if deck else "cover"

        tags = get_deck_parsed_tags(deck, cache=deck_tags_cache)
        mechanics = derive_deck_mechanics(tags)
        p["mechanics"] = mechanics

        active_mechanics["monarch"] = active_mechanics["monarch"] or mechanics["monarch"]
        active_mechanics["initiative"] = active_mechanics["initiative"] or mechanics["initiative"]
        active_mechanics["citys_blessing"] = active_mechanics["citys_blessing"] or mechanics["citys_blessing"]
        active_mechanics["poison"] = active_mechanics["poison"] or mechanics["poison"]

    game_token = session.get("game_token")
    if game_token:
        active_game_check = ActiveGame.query.filter_by(token=game_token).first()
        if not active_game_check:
            game_token = None

    current_user = get_current_user()
    debug_ui_enabled = app.debug and request.args.get("debug_ui") == "1"
    salt_action_values = {
        "mana_fucked": max(0, int(getattr(current_user, "mana_fucked_salt_value", 1) or 0)),
        "misplayed": max(0, int(getattr(current_user, "misplayed_salt_value", 1) or 0)),
    }

    card_logic_catalog = {
        "statuses": [
            {"id": "monarch", "label": "Monarch", "icon": "ðŸ‘‘", "kind": "exclusive"},
            {"id": "initiative", "label": "Initiative", "icon": "âš”ï¸", "kind": "exclusive"},
            {"id": "citys_blessing", "label": "City's Blessing", "icon": "ðŸ™ï¸", "kind": "toggle"},
        ],
        "counters": [
            {"id": "energy", "label": "Energy", "icon": "âš¡", "step": 1, "min": 0},
            {"id": "experience", "label": "Experience", "icon": "âœ¨", "step": 1, "min": 0},
            {"id": "poison", "label": "Poison", "icon": "â˜ ï¸", "step": 1, "min": 0, "max": 10},
        ],
        "commander_damage_threshold": 21,
    }
    card_logic_catalog["statuses"] = [
        status
        for status in card_logic_catalog["statuses"]
        if status.get("always_available") or active_mechanics.get(status.get("id"), True)
    ]

    return render_template(
        "life_counter.html",
        participants=participants,
        starting_player_id=session.get("active_player_id"),
        timer_config=session.get("timer_config", {"mode": "off"}),
        game_started_at=session.get("game_started_at"),
        turn_number=session.get("turn_number", 1),
        salt_action_values=salt_action_values,
        active_mechanics=active_mechanics,
        card_logic_catalog=card_logic_catalog,
        debug_ui_enabled=debug_ui_enabled,
        game_token=game_token,
    )


@app.route("/end_game", methods=["POST"])
def end_game():
    winner_id = request.form.get("winner", type=int)
    if not winner_id:
        return "Must select a winner", 400

    participants = session.get("game_participants")
    if not participants:
        return "No game in session", 400

    seen = {p["player_id"] for p in participants}
    if winner_id not in seen:
        return "Winner must be a participant", 400

    seat_validation_error, _ = validate_participant_seat_positions(participants)
    if seat_validation_error:
        return seat_validation_error, 400

    starting_player_id = session.get("active_player_id")
    if starting_player_id is not None:
        try:
            starting_player_id = int(starting_player_id)
        except Exception:
            starting_player_id = None

    # Legacy game-level salt rating is deprecated for new submissions.
    win_type_raw = request.form.get("win_type")
    win_type = canonicalize_win_type(win_type_raw, unknown_default="other")

    timed_mode_raw = request.form.get("timed_mode")
    timed_mode = canonicalize_timed_mode(timed_mode_raw)

    time_control_raw = (request.form.get("time_control") or "").strip()
    time_control = None
    if time_control_raw:
        if len(time_control_raw) > 1000:
            return "Invalid time control payload", 400
        try:
            parsed_time_control = json.loads(time_control_raw)
        except json.JSONDecodeError:
            return "Invalid time control JSON", 400
        if not isinstance(parsed_time_control, dict):
            return "Invalid time control JSON", 400
        sanitized_time_control = {}
        if timed_mode == "chess_clock":
            minutes = parsed_time_control.get("minutes_per_player")
            increment = parsed_time_control.get("increment_seconds", 0)
            if not isinstance(minutes, int) or minutes < 1 or minutes > 180:
                return "Invalid chess clock minutes", 400
            if not isinstance(increment, int) or increment < 0 or increment > 300:
                return "Invalid chess clock increment", 400
            sanitized_time_control = {
                "minutes_per_player": minutes,
                "increment_seconds": increment,
            }
        elif timed_mode == "turn_timer":
            seconds = parsed_time_control.get("seconds_per_turn")
            if not isinstance(seconds, int) or seconds < 5 or seconds > 3600:
                return "Invalid turn timer seconds", 400
            sanitized_time_control = {"seconds_per_turn": seconds}
        elif timed_mode == "off":
            sanitized_time_control = {}

        time_control = json.dumps(sanitized_time_control) if timed_mode else None

    ended_on_time_raw = (request.form.get("ended_on_time") or "").strip().lower()
    ended_on_time = None
    if ended_on_time_raw:
        if ended_on_time_raw not in {"true", "false"}:
            return "Invalid ended_on_time value", 400
        ended_on_time = ended_on_time_raw == "true"

    duration_seconds_raw = (request.form.get("duration_seconds") or "").strip()
    duration_seconds = None
    if duration_seconds_raw:
        try:
            duration_seconds = int(duration_seconds_raw)
        except ValueError:
            return "Invalid duration_seconds", 400
        if duration_seconds < 0 or duration_seconds > 172800:
            return "Invalid duration_seconds", 400

    ending_turn_raw = (request.form.get("ending_turn") or "").strip()
    ending_turn = None
    if ending_turn_raw:
        try:
            ending_turn = int(ending_turn_raw)
        except ValueError:
            return "Invalid ending_turn", 400
        if ending_turn < 1 or ending_turn > 500:
            return "Invalid ending_turn", 400

    if timed_mode is None and (time_control is not None or ended_on_time is not None):
        return "Timer metadata requires valid timed_mode", 400

    participant_flags_raw = (request.form.get("participant_flags") or request.form.get("flags") or "").strip()
    participant_flags_by_player = {}
    if participant_flags_raw:
        if len(participant_flags_raw.encode("utf-8")) > MAX_PARTICIPANT_FLAGS_PAYLOAD_BYTES:
            return "Participant flags payload too large", 400

        try:
            parsed_participant_flags = json.loads(participant_flags_raw)
        except json.JSONDecodeError:
            return "Invalid participant flags JSON", 400

        if not isinstance(parsed_participant_flags, dict):
            return "Invalid participant flags payload", 400

        valid_player_ids = {p["player_id"] for p in participants}
        for player_id_raw, player_flags_raw in parsed_participant_flags.items():
            try:
                player_id = int(player_id_raw)
            except (TypeError, ValueError):
                return "Invalid participant flags payload", 400

            if player_id not in valid_player_ids:
                return "Participant flags include unknown player", 400
            if not isinstance(player_flags_raw, dict):
                return "Invalid participant flags payload", 400

            sanitized_player_flags = {}
            for flag_key, flag_value in player_flags_raw.items():
                if flag_key not in ALLOWED_PARTICIPANT_FLAG_KEYS:
                    return "Unsupported participant flag key", 400
                if flag_key == "mana_fucked":
                    if not isinstance(flag_value, bool):
                        return "mana_fucked must be boolean", 400
                    sanitized_player_flags[flag_key] = flag_value
                elif flag_key == "misplayed":
                    if not isinstance(flag_value, bool):
                        return "misplayed must be boolean", 400
                    sanitized_player_flags[flag_key] = flag_value
                elif flag_key == "monarch":
                    if not isinstance(flag_value, bool):
                        return "monarch must be boolean", 400
                    sanitized_player_flags[flag_key] = flag_value
                elif flag_key == "poison":
                    if not isinstance(flag_value, int) or isinstance(flag_value, bool) or flag_value < 0:
                        return "poison must be a non-negative integer", 400
                    sanitized_player_flags[flag_key] = min(flag_value, 10)
                elif flag_key == "salt_count":
                    if not isinstance(flag_value, int) or isinstance(flag_value, bool) or flag_value < 0:
                        return "salt_count must be a non-negative integer", 400
                    sanitized_player_flags[flag_key] = flag_value
                elif flag_key == "turn_stats":
                    if not isinstance(flag_value, list):
                        return "turn_stats must be a list", 400
                    if len(flag_value) > MAX_PER_PLAYER_TURN_STATS:
                        return "turn_stats payload too large", 400

                    sanitized_turn_stats = []
                    for turn_entry in flag_value:
                        if not isinstance(turn_entry, dict):
                            return "turn_stats entries must be objects", 400

                        turn = turn_entry.get("turn")
                        life_delta = turn_entry.get("life_delta")
                        mana_fucked = turn_entry.get("mana_fucked")
                        misplayed = turn_entry.get("misplayed")
                        turn_seconds = turn_entry.get("turn_seconds", 0)

                        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1 or turn > 500:
                            return "turn_stats.turn must be an integer between 1 and 500", 400
                        if (
                            not isinstance(life_delta, int)
                            or isinstance(life_delta, bool)
                            or life_delta < -1000
                            or life_delta > 1000
                        ):
                            return "turn_stats.life_delta must be an integer between -1000 and 1000", 400
                        if not isinstance(mana_fucked, bool):
                            return "turn_stats.mana_fucked must be boolean", 400
                        if not isinstance(misplayed, bool):
                            return "turn_stats.misplayed must be boolean", 400
                        if (
                            not isinstance(turn_seconds, int)
                            or isinstance(turn_seconds, bool)
                            or turn_seconds < 0
                            or turn_seconds > 172800
                        ):
                            return "turn_stats.turn_seconds must be an integer between 0 and 172800", 400

                        sanitized_turn_stats.append(
                            {
                                "turn": turn,
                                "life_delta": life_delta,
                                "mana_fucked": mana_fucked,
                                "misplayed": misplayed,
                                "turn_seconds": turn_seconds,
                            }
                        )

                    sanitized_player_flags[flag_key] = sanitized_turn_stats
                elif flag_key == "card_state":
                    sanitized_card_state = sanitize_card_state_payload(flag_value, valid_player_ids)
                    if sanitized_card_state:
                        sanitized_player_flags[flag_key] = sanitized_card_state

            if sanitized_player_flags:
                participant_flags_by_player[player_id] = json.dumps(
                    sanitized_player_flags,
                    separators=(",", ":"),
                    sort_keys=True,
                )

    active_pod = get_active_pod()
    if not active_pod:
        return "No active pod available", 400

    game = Game(
        winner_id=winner_id,
        starting_player_id=starting_player_id,
        win_type=win_type,
        timed_mode=timed_mode,
        time_control=time_control,
        ended_on_time=ended_on_time,
        duration_seconds=duration_seconds,
        ending_turn=ending_turn,
        pod_id=active_pod.id,
    )
    db.session.add(game)
    db.session.flush()

    for p in participants:
        participant_flags_json = participant_flags_by_player.get(p["player_id"])
        participant_hot_fields = participant_hot_fields_from_flags(participant_flags_json)
        db.session.add(
            GameParticipant(
                game_id=game.id,
                player_id=p["player_id"],
                deck_id=p["deck_id"],
                seat_position=p.get("seat_position"),
                flags_json=participant_flags_json,
                salt_count=int(participant_hot_fields["salt_count"]),
                mana_fucked=bool(participant_hot_fields["mana_fucked"]),
                misplayed=bool(participant_hot_fields["misplayed"]),
                life_delta_total=int(participant_hot_fields["life_delta_total"]),
            )
        )

    db.session.commit()

    # Delete ActiveGame record if present
    game_token = session.pop("game_token", None)
    if game_token:
        try:
            active_game_rec = ActiveGame.query.filter_by(token=game_token).first()
            if active_game_rec:
                db.session.delete(active_game_rec)
                db.session.commit()
        except Exception:
            pass

    session.pop("game_participants", None)
    session.pop("active_player_id", None)
    session.pop("timer_config", None)
    session.pop("game_started_at", None)
    session.pop("turn_number", None)
    return redirect(url_for("index"))


# -------------------------
# Multiplayer sync API
# -------------------------

@app.route("/api/game/<token>/state", methods=["GET", "POST"])
def api_game_state(token):
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return jsonify({"error": "Game not found"}), 404

    if request.method == "GET":
        try:
            state = json.loads(active_game_rec.state_json)
        except (json.JSONDecodeError, Exception):
            state = {}
        return jsonify(state)

    # POST â€” apply a state update
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400

    player_id_raw = data.get("player_id")
    if player_id_raw is None:
        return jsonify({"error": "player_id required"}), 400
    try:
        player_id = int(player_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid player_id"}), 400

    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []
    valid_player_ids = {p["player_id"] for p in participants}
    if player_id not in valid_player_ids:
        return jsonify({"error": "Player not in game"}), 403

    # Authorization: host can update any player; phone players only their own
    is_host = session.get("user_id") == active_game_rec.host_user_id
    if not is_host:
        claimed_pid = session.get(f"game_join_{token}")
        if claimed_pid is None or int(claimed_pid) != player_id:
            return jsonify({"error": "Unauthorized"}), 403

    try:
        state = json.loads(active_game_rec.state_json)
    except (json.JSONDecodeError, Exception):
        state = {}

    if "life" not in state:
        state["life"] = {}
    if "flags" not in state:
        state["flags"] = {}
    if "card_state" not in state:
        state["card_state"] = {}
    if "version" not in state:
        state["version"] = 0

    pid = str(player_id)

    # Life: prefer absolute value from host, delta from phone
    life_abs = data.get("life")
    if life_abs is not None:
        try:
            life_abs = int(life_abs)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid life value"}), 400
        state["life"][pid] = life_abs

    life_delta = data.get("life_delta")
    if life_delta is not None and life_abs is None:
        try:
            life_delta = int(life_delta)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid life_delta"}), 400
        if life_delta < -1000 or life_delta > 1000:
            return jsonify({"error": "life_delta out of range"}), 400
        current_life = int(state["life"].get(pid, 40))
        state["life"][pid] = max(0, current_life + life_delta)

    # Flags
    flags_update = data.get("flags")
    if flags_update is not None and isinstance(flags_update, dict):
        if pid not in state["flags"]:
            state["flags"][pid] = {}
        for k, v in flags_update.items():
            if k in {"mana_fucked", "misplayed"} and isinstance(v, bool):
                state["flags"][pid][k] = v
            elif k == "salt_count" and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                state["flags"][pid][k] = v

    # Card state (scoped to requested player id for non-host via authorization above)
    card_state_update = data.get("card_state")
    if card_state_update is not None:
        sanitized_card_state = sanitize_card_state_payload(card_state_update, valid_player_ids)
        if sanitized_card_state:
            existing_player_card_state = state["card_state"].get(pid)
            if not isinstance(existing_player_card_state, dict):
                existing_player_card_state = {}

            merged_player_card_state = {
                "counters": dict(existing_player_card_state.get("counters", {}))
                if isinstance(existing_player_card_state.get("counters"), dict)
                else {},
                "commander_damage": dict(existing_player_card_state.get("commander_damage", {}))
                if isinstance(existing_player_card_state.get("commander_damage"), dict)
                else {},
                "statuses": dict(existing_player_card_state.get("statuses", {}))
                if isinstance(existing_player_card_state.get("statuses"), dict)
                else {},
            }

            for section in ("counters", "commander_damage", "statuses"):
                section_update = sanitized_card_state.get(section)
                if isinstance(section_update, dict) and section_update:
                    merged_player_card_state[section].update(section_update)

            state["card_state"][pid] = merged_player_card_state

    # Host-only: update active_player_id and turn number
    if is_host:
        active_player_raw = data.get("active_player_id")
        if active_player_raw is not None:
            try:
                apid = int(active_player_raw)
            except (TypeError, ValueError):
                apid = None
            if apid in valid_player_ids:
                state["active_player_id"] = apid
        turn_raw = data.get("turn")
        if turn_raw is not None:
            try:
                t = int(turn_raw)
                if 1 <= t <= 500:
                    state["turn"] = t
            except (TypeError, ValueError):
                pass

    # pass_turn: advance the active player to the next in seat order
    if data.get("pass_turn"):
        pid_list = [p["player_id"] for p in participants]
        if len(pid_list) > 1:
            if "passed" not in state or not isinstance(state["passed"], list):
                state["passed"] = []
            if player_id not in state["passed"]:
                state["passed"].append(player_id)
            current_active = state.get("active_player_id")
            # Advance to next player
            if current_active in pid_list:
                current_idx = pid_list.index(current_active)
            else:
                current_idx = 0
            next_idx = (current_idx + 1) % len(pid_list)
            state["active_player_id"] = pid_list[next_idx]
            # Increment turn counter when all players have passed
            if len(set(state["passed"])) >= len(pid_list):
                state["turn"] = int(state.get("turn", 1)) + 1
                state["passed"] = []

    state["version"] = int(state.get("version", 0)) + 1
    active_game_rec.state_json = json.dumps(state)
    active_game_rec.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify(state)


# -------------------------
# Multiplayer join routes
# -------------------------

@app.route("/join/<token>", methods=["GET"])
def join_game(token):
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return render_template("join_game.html", error="Game not found or already ended.", participants=[], token=token)
    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []
    return render_template("join_game.html", error=None, participants=participants, token=token)


@app.route("/join/<token>", methods=["POST"])
def join_game_claim(token):
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return render_template("join_game.html", error="Game not found or already ended.", participants=[], token=token)
    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []

    player_id_raw = request.form.get("player_id")
    try:
        player_id = int(player_id_raw)
    except (TypeError, ValueError):
        return render_template("join_game.html", error="Invalid selection.", participants=participants, token=token)

    valid_player_ids = {p["player_id"] for p in participants}
    if player_id not in valid_player_ids:
        return render_template("join_game.html", error="Invalid player selection.", participants=participants, token=token)

    session[f"game_join_{token}"] = player_id
    session.modified = True
    return redirect(url_for("player_panel", token=token, player_id=player_id))


@app.route("/join/<token>/<int:player_id>", methods=["GET"])
def player_panel(token, player_id):
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return render_template("join_game.html", error="Game not found or already ended.", participants=[], token=token)

    claimed = session.get(f"game_join_{token}")
    if claimed is None or int(claimed) != player_id:
        try:
            participants = json.loads(active_game_rec.participants_json)
        except (json.JSONDecodeError, Exception):
            participants = []
        return render_template("join_game.html", error="Please select your seat first.", participants=participants, token=token)

    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []

    this_player = next((p for p in participants if p["player_id"] == player_id), None)
    if not this_player:
        return render_template("join_game.html", error="Player not found in game.", participants=participants, token=token)

    try:
        state = json.loads(active_game_rec.state_json)
    except (json.JSONDecodeError, Exception):
        state = {}

    return render_template(
        "player_panel.html",
        token=token,
        player_id=player_id,
        this_player=this_player,
        participants=participants,
        initial_state=state,
    )


@app.route("/manual_game")
def manual_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = (
            Deck.query.filter_by(player_id=p.id, retired=False, planned=False).order_by(Deck.name.asc()).all()
        )
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in active_decks]
    decks_json = json.dumps(decks_by_player)
    return render_template("manual_game.html", players=players, decks_json=decks_json)


@app.route("/manual_record_game", methods=["POST"])
def manual_record_game():
    winner_id = request.form.get("winner")
    if not winner_id:
        return "Must select a winner", 400

    participants = []
    seen = set()
    for i in range(1, 7):
        p_id = request.form.get(f"player{i}")
        d_id = request.form.get(f"deck{i}")
        if p_id and d_id:
            p_id = int(p_id)
            d_id = int(d_id)

            if p_id in seen:
                return "Duplicate players not allowed", 400
            seen.add(p_id)

            deck = db.session.get(Deck, d_id)
            if not deck or deck.player_id != p_id or deck.retired or deck.planned:
                return "Invalid deck for player", 400

            participants.append({"player_id": p_id, "deck_id": d_id, "seat_position": len(participants) + 1})

    if len(participants) < 2:
        return "Need at least 2 players", 400
    if int(winner_id) not in seen:
        return "Winner must be a participant", 400

    active_pod = get_active_pod()
    if not active_pod:
        return "No active pod available", 400

    game = Game(winner_id=int(winner_id), pod_id=active_pod.id)
    db.session.add(game)
    db.session.flush()

    seat_validation_error, _ = validate_participant_seat_positions(participants)
    if seat_validation_error:
        return seat_validation_error, 400

    for participant in participants:
        db.session.add(
            GameParticipant(
                game_id=game.id,
                player_id=participant["player_id"],
                deck_id=participant["deck_id"],
                seat_position=participant["seat_position"],
            )
        )

    db.session.commit()
    return redirect(url_for("index"))


@app.route("/record_game", methods=["POST"])
def record_game():
    """Legacy 4-player record route (kept for compatibility with old forms)."""
    winner_id = request.form.get("winner")
    if not winner_id:
        return "Must select a winner", 400

    participants = []
    seen = set()
    for i in range(1, 5):
        p_id = request.form.get(f"player{i}")
        d_id = request.form.get(f"deck{i}")
        if p_id and d_id:
            p_id = int(p_id)
            d_id = int(d_id)

            if p_id in seen:
                return "Duplicate players not allowed", 400
            seen.add(p_id)

            deck = db.session.get(Deck, d_id)
            if not deck or deck.player_id != p_id:
                return "Invalid deck for player", 400

            participants.append({"player_id": p_id, "deck_id": d_id, "seat_position": len(participants) + 1})

    if len(participants) < 2:
        return "Need at least 2 players", 400
    if int(winner_id) not in seen:
        return "Winner must be a participant", 400

    active_pod = get_active_pod()
    if not active_pod:
        return "No active pod available", 400

    game = Game(winner_id=int(winner_id), pod_id=active_pod.id)
    db.session.add(game)
    db.session.flush()

    seat_validation_error, _ = validate_participant_seat_positions(participants)
    if seat_validation_error:
        return seat_validation_error, 400

    for participant in participants:
        db.session.add(
            GameParticipant(
                game_id=game.id,
                player_id=participant["player_id"],
                deck_id=participant["deck_id"],
                seat_position=participant["seat_position"],
            )
        )

    db.session.commit()
    return redirect(url_for("index"))


# -------------------------
# JSON REST API (for native clients)
# -------------------------


def api_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _serialize_deck_summary(deck: Deck, *, deck_tags_cache: dict[int, dict[str, bool]] | None = None) -> dict:
    wins = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.deck_id == deck.id, Game.winner_id == GameParticipant.player_id)
        .count()
    )
    uses = GameParticipant.query.filter_by(deck_id=deck.id).count()
    winrate = round((wins / uses) * 100, 1) if uses > 0 else 0.0
    deck_tags = get_deck_parsed_tags(deck, cache=deck_tags_cache)
    deck_mechanics = derive_deck_mechanics(deck_tags)
    return {
        "id": deck.id,
        "name": deck.name,
        "commander": deck.commander_name or deck.commander,
        "retired": deck.retired,
        "planned": deck.planned,
        "player_id": deck.player_id,
        "player_name": deck.owner.name,
        "wins": wins,
        "uses": uses,
        "winrate": winrate,
        "art_url": deck.commander_art_url,
        "mechanics": deck_mechanics,
    }


def _serialize_deck_detail(deck: Deck) -> dict:
    payload = _serialize_deck_summary(deck)
    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.deck_id == deck.id)
        .order_by(Game.date.desc())
        .limit(20)
        .all()
    )
    recent_games = []
    for gp in participations:
        game = gp.game
        recent_games.append({
            "game_id": game.id,
            "date": game.date.isoformat(),
            "won": game.winner_id == deck.player_id,
            "win_type": game.win_type,
            "ending_turn": game.ending_turn,
            "participant_count": GameParticipant.query.filter_by(game_id=game.id).count(),
        })
    payload["recent_games"] = recent_games
    payload["decklist_text"] = deck.decklist_text or ""
    return payload


def _serialize_pod_summary(pod: Pod, current_user: User | None, active_pod_id: int | None = None) -> dict:
    membership = None
    if current_user and current_user.player:
        membership = PodMembership.query.filter_by(pod_id=pod.id, player_id=current_user.player.id).first()

    games_count = Game.query.filter_by(pod_id=pod.id).count()
    member_count = PodMembership.query.filter_by(pod_id=pod.id).count()
    accessible_pod_ids = {candidate.id for candidate in get_accessible_pods(current_user)}
    return {
        "id": pod.id,
        "name": pod.name,
        "slug": pod.slug,
        "is_active": bool(pod.is_active),
        "is_active_selection": bool(active_pod_id == pod.id),
        "member_count": member_count,
        "games_count": games_count,
        "my_role": membership.role if membership else None,
        "can_manage": can_manage_pod(current_user, pod.id),
        "can_switch": bool(pod.is_active and pod.id in accessible_pod_ids),
        "can_retire": bool(current_user and current_user.is_admin and pod.slug != DEFAULT_POD_SLUG and pod.is_active),
        "can_restore": bool(current_user and current_user.is_admin and not pod.is_active),
        "can_delete": bool(current_user and current_user.is_admin and pod.slug != DEFAULT_POD_SLUG and games_count == 0),
    }


def _serialize_pod_member(membership: PodMembership, current_user: User | None) -> dict:
    return {
        "player_id": membership.player_id,
        "player_name": membership.player.name,
        "role": membership.role,
        "can_remove": can_manage_pod(current_user, membership.pod_id),
        "can_change_role": bool(current_user and current_user.is_admin and can_manage_pod(current_user, membership.pod_id)),
    }


def _serialize_pod_detail(pod: Pod, current_user: User | None, active_pod_id: int | None = None) -> dict:
    memberships = (
        PodMembership.query
        .filter_by(pod_id=pod.id)
        .join(Player, Player.id == PodMembership.player_id)
        .order_by(text("CASE WHEN pod_membership.role = 'podmaster' THEN 0 ELSE 1 END"), Player.name.asc())
        .all()
    )
    member_ids = {membership.player_id for membership in memberships}
    available_players = [
        {"id": player.id, "name": player.name}
        for player in Player.query.order_by(Player.name.asc()).all()
        if player.id not in member_ids
    ]
    payload = _serialize_pod_summary(pod, current_user, active_pod_id=active_pod_id)
    payload["members"] = [_serialize_pod_member(membership, current_user) for membership in memberships]
    payload["available_players"] = available_players
    return payload


def _serialize_admin_user(user: User, *, pending_request: RegistrationRequest | None = None) -> dict:
    pod_memberships = []
    if user.player:
        memberships = (
            PodMembership.query
            .join(Pod, Pod.id == PodMembership.pod_id)
            .filter(PodMembership.player_id == user.player.id)
            .order_by(Pod.name.asc())
            .all()
        )
        pod_memberships = [
            {
                "pod_id": membership.pod_id,
                "pod_name": membership.pod.name,
                "role": membership.role,
                "is_active": bool(membership.pod.is_active),
            }
            for membership in memberships
        ]

    requested_pod = pending_request.requested_pod if pending_request else None
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": bool(user.is_admin),
        "is_active": bool(user.is_active),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "approved_at": user.approved_at.isoformat() if user.approved_at else None,
        "player_id": user.player.id if user.player else None,
        "player_name": user.player.name if user.player else None,
        "pods": pod_memberships,
        "registration_request": (
            {
                "request_id": pending_request.id,
                "requested_pod_id": pending_request.requested_pod_id,
                "requested_pod_name": requested_pod.name if requested_pod else None,
                "requested_pod_active": bool(requested_pod.is_active) if requested_pod else None,
                "created_at": pending_request.created_at.isoformat() if pending_request.created_at else None,
            } if pending_request else None
        ),
    }


def _serialize_registration_request(registration_request: RegistrationRequest) -> dict:
    user = registration_request.user
    requested_pod = registration_request.requested_pod
    return {
        "request_id": registration_request.id,
        "user_id": registration_request.user_id,
        "username": user.username if user else None,
        "display_name": user.display_name if user else None,
        "created_at": registration_request.created_at.isoformat() if registration_request.created_at else None,
        "requested_pod_id": registration_request.requested_pod_id,
        "requested_pod_name": requested_pod.name if requested_pod else None,
        "requested_pod_active": bool(requested_pod.is_active) if requested_pod else None,
    }


def _api_json_payload() -> dict | None:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    return payload


def _api_deck_owner_id_from_payload(payload: dict, current_user: User) -> int | None:
    if current_user.is_admin:
        owner_id = payload.get("player_id")
        if not isinstance(owner_id, int):
            return None
        owner = db.session.get(Player, owner_id)
        if not owner:
            return None
        return owner.id

    if not current_user.player:
        return None
    return current_user.player.id


def _api_parse_manual_game_payload(payload: dict, current_user: User) -> tuple[dict | None, tuple[Response, int] | None]:
    participants_raw = payload.get("participants")
    winner_id = payload.get("winner_id")
    if not isinstance(participants_raw, list) or len(participants_raw) < 2 or len(participants_raw) > 6:
        return None, (jsonify({"error": "participants must contain between 2 and 6 entries"}), 400)
    if not isinstance(winner_id, int):
        return None, (jsonify({"error": "winner_id is required"}), 400)

    normalized_participants = []
    seen_player_ids = set()
    for index, participant_raw in enumerate(participants_raw):
        if not isinstance(participant_raw, dict):
            return None, (jsonify({"error": "participants must contain objects"}), 400)
        player_id = participant_raw.get("player_id")
        deck_id = participant_raw.get("deck_id")
        seat_position = participant_raw.get("seat_position")
        if not isinstance(player_id, int) or not db.session.get(Player, player_id):
            return None, (jsonify({"error": "participants.player_id must reference an existing player"}), 400)
        if not isinstance(deck_id, int):
            return None, (jsonify({"error": "participants.deck_id must be an integer"}), 400)
        deck = db.session.get(Deck, deck_id)
        if not deck:
            return None, (jsonify({"error": "participants.deck_id must reference an existing deck"}), 400)
        if deck.player_id != player_id:
            return None, (jsonify({"error": "participant deck must belong to participant player"}), 400)
        if player_id in seen_player_ids:
            return None, (jsonify({"error": "Duplicate players are not allowed"}), 400)
        seen_player_ids.add(player_id)
        if seat_position is None:
            seat_position = index + 1
        if not isinstance(seat_position, int) or seat_position < 1 or seat_position > 6:
            return None, (jsonify({"error": "seat_position must be an integer between 1 and 6"}), 400)
        normalized_participants.append({
            "player_id": player_id,
            "deck_id": deck_id,
            "seat_position": seat_position,
        })

    if winner_id not in seen_player_ids:
        return None, (jsonify({"error": "Winner must be a participant"}), 400)

    seat_validation_error, _ = validate_participant_seat_positions(normalized_participants)
    if seat_validation_error:
        return None, (jsonify({"error": seat_validation_error}), 400)

    starting_player_id = payload.get("starting_player_id")
    if starting_player_id is not None:
        if not isinstance(starting_player_id, int) or starting_player_id not in seen_player_ids:
            return None, (jsonify({"error": "starting_player_id must reference a participant"}), 400)

    win_type_raw = payload.get("win_type")
    win_type = canonicalize_win_type(win_type_raw, unknown_default="other")

    ending_turn = payload.get("ending_turn")
    if ending_turn is not None:
        if not isinstance(ending_turn, int) or ending_turn < 1 or ending_turn > 500:
            return None, (jsonify({"error": "ending_turn must be an integer between 1 and 500"}), 400)

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            return None, (jsonify({"error": "note must be a string"}), 400)
        note = note.strip() or None

    date_value = payload.get("date")
    game_date = None
    if isinstance(date_value, str) and date_value.strip():
        date_text = date_value.strip()
        for parser in (
            lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
            lambda value: datetime.strptime(value, "%Y-%m-%d"),
        ):
            try:
                game_date = parser(date_text)
                break
            except ValueError:
                continue
        if game_date is None:
            return None, (jsonify({"error": "date must be ISO-8601 or YYYY-MM-DD"}), 400)
    else:
        game_date = datetime.utcnow()

    participant_flags_by_player: dict[int, str] = {}
    for participant_raw in participants_raw:
        player_id = participant_raw["player_id"]
        salt_count = participant_raw.get("salt_count", 0)
        mana_fucked = participant_raw.get("mana_fucked", False)
        misplayed = participant_raw.get("misplayed", False)
        commander_damage = participant_raw.get("commander_damage", {})

        if not isinstance(salt_count, int) or isinstance(salt_count, bool) or salt_count < 0:
            return None, (jsonify({"error": "salt_count must be a non-negative integer"}), 400)
        if not isinstance(mana_fucked, bool):
            return None, (jsonify({"error": "mana_fucked must be boolean"}), 400)
        if not isinstance(misplayed, bool):
            return None, (jsonify({"error": "misplayed must be boolean"}), 400)

        flags_payload = {
            "salt_count": salt_count,
            "mana_fucked": mana_fucked,
            "misplayed": misplayed,
        }
        sanitized_card_state = sanitize_card_state_payload(
            {"commander_damage": commander_damage},
            seen_player_ids,
        ) if commander_damage else None
        if sanitized_card_state:
            flags_payload["card_state"] = sanitized_card_state

        monarch = participant_raw.get("monarch")
        if isinstance(monarch, bool):
            flags_payload["monarch"] = monarch

        poison = participant_raw.get("poison")
        if isinstance(poison, int) and not isinstance(poison, bool) and poison >= 0:
            flags_payload["poison"] = poison

        turn_stats_raw = participant_raw.get("turn_stats")
        if isinstance(turn_stats_raw, list):
            parsed_turn_stats = []
            for entry in turn_stats_raw:
                if not isinstance(entry, dict):
                    continue
                stat: dict = {}
                turn_num = entry.get("turn")
                if isinstance(turn_num, int) and not isinstance(turn_num, bool) and 1 <= turn_num <= 500:
                    stat["turn"] = turn_num
                life_delta = entry.get("life_delta")
                if isinstance(life_delta, int) and not isinstance(life_delta, bool):
                    stat["life_delta"] = life_delta
                for bool_key in ("mana_fucked", "misplayed"):
                    val = entry.get(bool_key)
                    if isinstance(val, bool):
                        stat[bool_key] = val
                turn_seconds = entry.get("turn_seconds")
                if isinstance(turn_seconds, int) and not isinstance(turn_seconds, bool) and turn_seconds >= 0:
                    stat["turn_seconds"] = turn_seconds
                if stat:
                    parsed_turn_stats.append(stat)
            if parsed_turn_stats:
                flags_payload["turn_stats"] = parsed_turn_stats[:MAX_PER_PLAYER_TURN_STATS]

        participant_flags_by_player[player_id] = json.dumps(
            flags_payload,
            separators=(",", ":"),
            sort_keys=True,
        )

    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is not None:
        if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool) or duration_seconds < 0:
            return None, (jsonify({"error": "duration_seconds must be a non-negative integer"}), 400)

    return {
        "participants": normalized_participants,
        "winner_id": winner_id,
        "starting_player_id": starting_player_id,
        "win_type": win_type,
        "ending_turn": ending_turn,
        "note": note,
        "date": game_date,
        "duration_seconds": duration_seconds,
        "participant_flags_by_player": participant_flags_by_player,
    }, None


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid username or password"}), 401
    if not user.is_active:
        return jsonify({"error": "Account pending approval. Please contact an admin."}), 403
    if not user.player:
        user.player = Player(name=user.display_name)
        db.session.commit()
    session["user_id"] = user.id
    session["username"] = user.username
    session["display_name"] = user.display_name
    session["is_admin"] = user.is_admin
    session["use_sigtaara"] = user.use_sigtaara
    session["use_light_theme"] = user.use_light_theme
    get_active_pod()
    return jsonify({
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "player_id": user.player.id if user.player else None,
        "can_access_registration_requests": can_access_registration_request_queue(user),
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"message": "Logged out"})


@app.route("/api/me")
@api_login_required
def api_me():
    u = get_current_user()
    if not u:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "user_id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "is_admin": u.is_admin,
        "player_id": u.player.id if u.player else None,
        "can_access_registration_requests": can_access_registration_request_queue(u),
    })


@app.route("/api/mobile/releases/android/latest")
def api_android_latest_release():
    payload, error = _load_android_release_manifest()
    if error is not None:
        return error
    return jsonify(payload)


@app.route("/api/stats")
@api_login_required
def api_stats():
    game_q, scope, active_pod = game_query_for_scope()
    game_ids_subquery = game_q.with_entities(Game.id)

    players = Player.query.all()
    player_stats = []
    for p in players:
        wins = game_q.filter_by(winner_id=p.id).count()
        played = (
            GameParticipant.query.join(Game)
            .filter(GameParticipant.player_id == p.id, Game.id.in_(game_ids_subquery))
            .count()
        )
        winrate = round(wins / played * 100, 1) if played > 0 else 0.0
        player_stats.append({"player_id": p.id, "name": p.name, "wins": wins, "played": played, "winrate": winrate})
    player_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))

    recent_games_list = game_q.order_by(Game.date.desc()).limit(10).all()
    recent_game_ids = [g.id for g in recent_games_list]
    parts = GameParticipant.query.filter(
        GameParticipant.game_id.in_(recent_game_ids if recent_game_ids else [-1])
    ).all()
    game_parts_map: dict = {}
    for gp in parts:
        game_parts_map.setdefault(gp.game_id, []).append(gp)

    recent_games = []
    for g in recent_games_list:
        gps = game_parts_map.get(g.id, [])
        recent_games.append({
            "id": g.id,
            "date": g.date.isoformat(),
            "winner": {"id": g.winner_id, "name": g.winner.name},
            "win_type": g.win_type,
            "ending_turn": g.ending_turn,
            "participants": [
                {
                    "player_id": gp.player_id,
                    "player_name": gp.player.name,
                    "deck_id": gp.deck_id,
                    "deck_name": gp.deck.name,
                    "commander": gp.deck.commander_name or gp.deck.commander,
                    "art_url": gp.deck.commander_art_url,
                    "won": g.winner_id == gp.player_id,
                }
                for gp in gps
            ],
        })

    decks = Deck.query.all()
    deck_stats = []
    for d in decks:
        wins = (
            GameParticipant.query.join(Game)
            .filter(
                GameParticipant.deck_id == d.id,
                Game.winner_id == GameParticipant.player_id,
                Game.id.in_(game_ids_subquery),
            )
            .count()
        )
        uses = (
            GameParticipant.query.join(Game)
            .filter(GameParticipant.deck_id == d.id, Game.id.in_(game_ids_subquery))
            .count()
        )
        winrate = round(wins / uses * 100, 1) if uses > 0 else 0.0
        deck_stats.append({
            "id": d.id,
            "name": d.name,
            "commander": d.commander_name or d.commander,
            "owner": d.owner.name,
            "owner_id": d.player_id,
            "wins": wins,
            "uses": uses,
            "winrate": winrate,
            "art_url": d.commander_art_url,
            "retired": d.retired,
        })
    deck_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))

    return jsonify({
        "player_stats": player_stats,
        "recent_games": recent_games,
        "top_decks": deck_stats[:6],
        "scope": scope,
        "pod_name": active_pod.name if active_pod else None,
    })


@app.route("/api/saltmine")
@api_login_required
def api_saltmine():
    game_q, scope, active_pod = game_query_for_scope()

    scoped_games = game_q.all()
    scoped_game_ids = [g.id for g in scoped_games]
    participants = (
        GameParticipant.query
        .filter(GameParticipant.game_id.in_(scoped_game_ids if scoped_game_ids else [-1]))
        .all()
    )

    game_salt_stats: dict[int, dict[str, int | bool | None]] = {}
    player_salt_stats: dict[int, dict[str, int]] = {}
    deck_salt_stats: dict[int, dict[str, int]] = {}

    for gp in participants:
        parsed_flags = participant_flags_snapshot(gp)
        salt_count = participant_salt_count(parsed_flags)
        salted = salt_count > 0

        game_entry = game_salt_stats.setdefault(gp.game_id, {
            "salted_players": 0,
            "participants": 0,
            "any_salted": False,
            "salt_clicks": 0,
            "legacy_salt_rating": None,
            "has_legacy_salt": False,
            "sort_salted_players": 0,
            "sort_salt_clicks": 0,
            "sort_has_salt": 0,
        })
        game_entry["participants"] += 1
        if salted:
            game_entry["salted_players"] += 1
            game_entry["any_salted"] = True
        game_entry["salt_clicks"] += salt_count

        player_entry = player_salt_stats.setdefault(gp.player_id, {
            "salted_games": 0,
            "games": 0,
            "salt_clicks": 0,
        })
        player_entry["games"] += 1
        if salted:
            player_entry["salted_games"] += 1
        player_entry["salt_clicks"] += salt_count

        deck_entry = deck_salt_stats.setdefault(gp.deck_id, {
            "salted_games": 0,
            "games": 0,
            "salt_clicks": 0,
        })
        deck_entry["games"] += 1
        if salted:
            deck_entry["salted_games"] += 1
        deck_entry["salt_clicks"] += salt_count

    for game in scoped_games:
        stats = game_salt_stats.setdefault(game.id, {
            "salted_players": 0,
            "participants": 0,
            "any_salted": False,
            "salt_clicks": 0,
            "legacy_salt_rating": None,
            "has_legacy_salt": False,
            "sort_salted_players": 0,
            "sort_salt_clicks": 0,
            "sort_has_salt": 0,
        })
        legacy_salt = game.salt_rating is not None
        stats["legacy_salt_rating"] = game.salt_rating
        stats["has_legacy_salt"] = legacy_salt
        stats["sort_salted_players"] = int(stats["salted_players"])
        stats["sort_salt_clicks"] = int(stats["salt_clicks"])
        stats["sort_has_salt"] = int(stats["any_salted"] or legacy_salt)

    salty_games = sorted(
        scoped_games,
        key=lambda game: (
            int(game_salt_stats[game.id]["sort_salted_players"]),
            int(game_salt_stats[game.id]["sort_salt_clicks"]),
            int(game_salt_stats[game.id]["sort_has_salt"]),
            game.date,
        ),
        reverse=True,
    )
    salty_games = [game for game in salty_games if game_salt_stats[game.id]["sort_has_salt"]][:10]

    player_ids = list(player_salt_stats.keys())
    players_by_id = {
        p.id: p for p in Player.query.filter(Player.id.in_(player_ids if player_ids else [-1])).all()
    }
    salty_players = []
    for player_id, stats in player_salt_stats.items():
        games_played = int(stats["games"])
        salted_games = int(stats["salted_games"])
        if games_played < 3 or player_id not in players_by_id:
            continue
        salty_players.append({
            "player_id": player_id,
            "player_name": players_by_id[player_id].name,
            "salt_rate": round((salted_games / games_played) * 100, 1),
            "salted_games": salted_games,
            "games_played": games_played,
            "salt_clicks": int(stats["salt_clicks"]),
        })
    salty_players.sort(
        key=lambda row: (row["salt_rate"], row["salted_games"], row["games_played"], row["player_name"].lower()),
        reverse=True,
    )
    salty_players = salty_players[:10]

    deck_ids = list(deck_salt_stats.keys())
    decks_by_id = {
        d.id: d for d in Deck.query.filter(Deck.id.in_(deck_ids if deck_ids else [-1])).all()
    }
    salty_decks = []
    for deck_id, stats in deck_salt_stats.items():
        games_played = int(stats["games"])
        salted_games = int(stats["salted_games"])
        if games_played < 3 or deck_id not in decks_by_id:
            continue
        deck = decks_by_id[deck_id]
        salty_decks.append({
            "deck_id": deck_id,
            "deck_name": deck.name,
            "commander": deck.commander_name or deck.commander,
            "owner_name": deck.owner.name,
            "owner_id": deck.player_id,
            "salt_rate": round((salted_games / games_played) * 100, 1),
            "salted_games": salted_games,
            "games_played": games_played,
            "salt_clicks": int(stats["salt_clicks"]),
        })
    salty_decks.sort(
        key=lambda row: (row["salt_rate"], row["salted_games"], row["games_played"], row["deck_name"].lower()),
        reverse=True,
    )
    salty_decks = salty_decks[:10]

    sp = (
        db.session.query(
            func.count(Game.id).label("games"),
            func.sum(
                case(
                    (Game.winner_id == Game.starting_player_id, 1),
                    else_=0,
                )
            ).label("wins")
        )
        .filter(Game.starting_player_id.isnot(None))
        .filter(Game.id.in_(game_q.with_entities(Game.id)))
        .first()
    )
    start_games = int(sp.games or 0)
    start_wins = int(sp.wins or 0)
    start_winrate = round((start_wins / start_games) * 100, 1) if start_games else None

    seat_winrate_rows = (
        db.session.query(
            GameParticipant.seat_position.label("seat_position"),
            func.count(GameParticipant.id).label("games"),
            func.sum(
                case(
                    (Game.winner_id == GameParticipant.player_id, 1),
                    else_=0,
                )
            ).label("wins"),
        )
        .join(Game, Game.id == GameParticipant.game_id)
        .filter(Game.id.in_(game_q.with_entities(Game.id)))
        .filter(GameParticipant.seat_position.isnot(None))
        .group_by(GameParticipant.seat_position)
        .order_by(GameParticipant.seat_position.asc())
        .all()
    )
    seat_winrates = []
    for row in seat_winrate_rows:
        games = int(row.games or 0)
        wins = int(row.wins or 0)
        seat_winrates.append({
            "seat_position": int(row.seat_position),
            "games": games,
            "wins": wins,
            "winrate": round((wins / games) * 100, 1) if games else None,
        })

    scoped_participants = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(Game.id.in_(game_q.with_entities(Game.id)))
        .all()
    )
    deck_tags_cache: dict[int, dict[str, bool]] = {}
    deck_mechanics_by_id = {}
    for deck in Deck.query.all():
        tags = get_deck_parsed_tags(deck, cache=deck_tags_cache)
        deck_mechanics_by_id[deck.id] = derive_deck_mechanics(tags)

    participants_by_game_id: dict[int, list[GameParticipant]] = {}
    capability_uses_wins = {
        key: {"uses": 0, "wins": 0}
        for key in ("monarch", "poison", "energy", "experience")
    }
    activation_correlation = {
        key: {"activated_games_with_capability": 0, "games_with_capability": 0}
        for key in ("monarch", "poison")
    }

    for gp in scoped_participants:
        mechanics = deck_mechanics_by_id.get(gp.deck_id)
        if not mechanics:
            continue
        gp.deck_mechanics = mechanics
        participants_by_game_id.setdefault(gp.game_id, []).append(gp)
        for key in capability_uses_wins:
            if not mechanics[key]:
                continue
            capability_uses_wins[key]["uses"] += 1
            if gp.game and gp.game.winner_id == gp.player_id:
                capability_uses_wins[key]["wins"] += 1

    for participants_in_game in participants_by_game_id.values():
        game_activation = compute_game_mechanic_activation(participants_in_game)
        if game_activation["monarch_capable_present"]:
            activation_correlation["monarch"]["games_with_capability"] += 1
            if game_activation["monarch_activated"]:
                activation_correlation["monarch"]["activated_games_with_capability"] += 1
        if game_activation["poison_capable_present"]:
            activation_correlation["poison"]["games_with_capability"] += 1
            if game_activation["poison_activated"]:
                activation_correlation["poison"]["activated_games_with_capability"] += 1

    mechanic_stats = []
    for key, counts in capability_uses_wins.items():
        uses = int(counts["uses"])
        wins = int(counts["wins"])
        stat = {
            "mechanic": key,
            "uses": uses,
            "wins": wins,
            "winrate": round((wins / uses) * 100, 1) if uses else None,
        }
        if key in activation_correlation:
            capability_games = int(activation_correlation[key]["games_with_capability"])
            activated_games = int(activation_correlation[key]["activated_games_with_capability"])
            stat["games_with_capability"] = capability_games
            stat["activated_games"] = activated_games
            stat["activation_rate"] = round((activated_games / capability_games) * 100, 1) if capability_games else None
        mechanic_stats.append(stat)

    return jsonify({
        "scope": scope,
        "pod_name": active_pod.name if active_pod else None,
        "starting_player": {
            "games": start_games,
            "wins": start_wins,
            "winrate": start_winrate,
            "seat_winrates": seat_winrates,
        },
        "salty_players": salty_players,
        "salty_decks": salty_decks,
        "salty_games": [
            {
                "game_id": game.id,
                "date": game.date.isoformat(),
                "winner_name": game.winner.name,
                "win_type": game.win_type,
                "salted_players": int(game_salt_stats[game.id]["salted_players"]),
                "participants": int(game_salt_stats[game.id]["participants"]),
                "salt_clicks": int(game_salt_stats[game.id]["salt_clicks"]),
                "legacy_salt_rating": game_salt_stats[game.id]["legacy_salt_rating"],
                "starting_player_name": game.starting_player.name if game.starting_player else None,
            }
            for game in salty_games
        ],
        "mechanic_stats": mechanic_stats,
    })


@app.route("/api/admin/users")
@api_login_required
def api_admin_users():
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403

    pod_id = request.args.get("pod_id", type=int)
    available_pods = Pod.query.order_by(Pod.is_active.desc(), Pod.name.asc()).all()

    pending_requests = (
        RegistrationRequest.query
        .join(User, RegistrationRequest.user_id == User.id)
        .filter(
            RegistrationRequest.status == "pending",
            User.is_active == False,  # noqa: E712
        )
        .order_by(RegistrationRequest.created_at.asc())
        .all()
    )
    if pod_id:
        pending_requests = [request_item for request_item in pending_requests if request_item.requested_pod_id == pod_id]
    pending_requests_by_user_id = {request_item.user_id: request_item for request_item in pending_requests}
    pending_users = [
        _serialize_admin_user(request_item.user, pending_request=request_item)
        for request_item in pending_requests
        if request_item.user
    ]
    pending_user_ids = {row["user_id"] for row in pending_users}

    active_users = User.query.filter_by(is_active=True).order_by(User.created_at.desc()).all()
    if pod_id:
        active_users = [
            user for user in active_users
            if user.player and PodMembership.query.filter_by(player_id=user.player.id, pod_id=pod_id).first()
        ]

    inactive_users = User.query.filter_by(is_active=False).order_by(User.created_at.desc()).all()
    if pod_id:
        inactive_users = [
            user for user in inactive_users
            if (
                user.id in pending_requests_by_user_id or
                (user.player and PodMembership.query.filter_by(player_id=user.player.id, pod_id=pod_id).first())
            )
        ]

    return jsonify({
        "selected_pod_id": pod_id,
        "pods": [{"id": pod.id, "name": pod.name, "is_active": bool(pod.is_active)} for pod in available_pods],
        "pending_users": pending_users,
        "active_users": [_serialize_admin_user(user) for user in active_users],
        "inactive_users": [
            _serialize_admin_user(user, pending_request=pending_requests_by_user_id.get(user.id))
            for user in inactive_users
            if user.id not in pending_user_ids
        ],
    })


@app.route("/api/admin/users/<int:user_id>/approve", methods=["POST"])
@api_login_required
def api_admin_approve_user(user_id):
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    registration_request = RegistrationRequest.query.filter_by(user_id=user_id).first()
    if not registration_request:
        return jsonify({"error": "No pending registration request found for that user."}), 404

    status, approved_user = approve_user_from_registration_request(registration_request, current_user.id)
    if status in {"missing_request", "missing_user"}:
        return jsonify({"error": "No pending registration request found for that user."}), 404
    if status == "not_pending":
        return jsonify({"error": "Registration request is no longer pending."}), 409
    if status == "name_collision":
        return jsonify({"error": f"Can't approve: display name '{approved_user.display_name}' is already used by a Player."}), 409
    if status == "inactive_pod":
        return jsonify({"error": "Can't approve: requested pod is inactive."}), 409
    if status == "missing_pod":
        return jsonify({"error": "Can't approve: no valid pod is available for this registration request."}), 409

    return jsonify({"ok": True}), 200


@app.route("/api/admin/users/<int:user_id>/deny", methods=["POST"])
@api_login_required
def api_admin_deny_user(user_id):
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    registration_request = RegistrationRequest.query.filter_by(user_id=user_id).first()
    if not registration_request:
        return jsonify({"error": "No pending registration request found for that user."}), 404
    if not can_deny_registration_request(current_user, registration_request):
        return jsonify({"error": deny_registration_request_permission_message(current_user, registration_request)}), 403

    status, denied_user = deny_user_from_registration_request(registration_request, current_user.id)
    if status == "missing_user":
        return jsonify({"error": "No pending registration request found for that user."}), 404
    if status == "not_pending":
        return jsonify({"error": "Only pending users can be denied."}), 409

    return jsonify({"ok": True, "username": denied_user.username}), 200


@app.route("/api/admin/users/<int:user_id>/deactivate", methods=["POST"])
@api_login_required
def api_admin_deactivate_user(user_id):
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Not found"}), 404
    if current_user.id == user.id:
        return jsonify({"error": "You can't deactivate your own account."}), 409

    user.is_active = False
    db.session.commit()
    return jsonify({"ok": True}), 200


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@api_login_required
def api_admin_delete_user(user_id):
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Not found"}), 404
    if current_user.id == user.id:
        return jsonify({"error": "You can't delete your own account."}), 409

    linked_player = user.player
    if linked_player:
        linked_player.user_id = None

    RegistrationRequest.query.filter_by(reviewed_by_user_id=user.id).update(
        {RegistrationRequest.reviewed_by_user_id: None},
        synchronize_session=False,
    )
    RegistrationRequest.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True}), 200


@app.route("/api/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
@api_login_required
def api_admin_toggle_admin(user_id):
    current_user = get_current_user()
    if not current_user or not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "Not found"}), 404
    if current_user.id == user.id:
        return jsonify({"error": "You can't change your own admin status here."}), 409

    user.is_admin = not user.is_admin
    db.session.commit()
    return jsonify({"ok": True, "is_admin": bool(user.is_admin)}), 200


@app.route("/api/registration-requests")
@api_login_required
def api_registration_requests():
    current_user = get_current_user()
    if not can_access_registration_request_queue(current_user):
        return jsonify({"error": "Forbidden"}), 403

    pending_query = (
        RegistrationRequest.query
        .join(User, RegistrationRequest.user_id == User.id)
        .filter(
            RegistrationRequest.status == "pending",
            User.is_active == False,  # noqa: E712
        )
    )
    if not current_user.is_admin:
        manageable_pod_ids = {
            membership.pod_id
            for membership in PodMembership.query.filter_by(player_id=current_user.player.id, role="podmaster").all()
        }
        pending_query = pending_query.filter(
            RegistrationRequest.requested_pod_id.in_(manageable_pod_ids if manageable_pod_ids else [-1])
        )

    pending = pending_query.order_by(RegistrationRequest.created_at.asc()).all()
    return jsonify({
        "requests": [_serialize_registration_request(registration_request) for registration_request in pending]
    })


@app.route("/api/registration-requests/<int:request_id>/approve", methods=["POST"])
@api_login_required
def api_approve_registration_request(request_id):
    current_user = get_current_user()
    registration_request = db.session.get(RegistrationRequest, request_id)
    if not registration_request:
        return jsonify({"error": "Not found"}), 404
    if not can_approve_registration_request(current_user, registration_request):
        return jsonify({"error": "Forbidden"}), 403

    status, approved_user = approve_user_from_registration_request(registration_request, current_user.id if current_user else None)
    if status in {"missing_request", "missing_user"}:
        return jsonify({"error": "Not found"}), 404
    if status == "not_pending":
        return jsonify({"error": "Registration request is no longer pending."}), 409
    if status == "name_collision":
        return jsonify({"error": f"Can't approve: display name '{approved_user.display_name}' is already used by a Player."}), 409
    if status == "inactive_pod":
        return jsonify({"error": "Can't approve: requested pod is inactive."}), 409
    if status == "missing_pod":
        return jsonify({"error": "Can't approve: no valid pod is available for this registration request."}), 409

    return jsonify({"ok": True}), 200


@app.route("/api/registration-requests/<int:request_id>/deny", methods=["POST"])
@api_login_required
def api_deny_registration_request(request_id):
    current_user = get_current_user()
    registration_request = db.session.get(RegistrationRequest, request_id)
    if not registration_request:
        return jsonify({"error": "Not found"}), 404
    if not can_deny_registration_request(current_user, registration_request):
        return jsonify({"error": deny_registration_request_permission_message(current_user, registration_request)}), 403

    status, denied_user = deny_user_from_registration_request(registration_request, current_user.id if current_user else None)
    if status == "missing_user":
        return jsonify({"error": "Not found"}), 404
    if status == "not_pending":
        return jsonify({"error": "Only pending users can be denied."}), 409

    return jsonify({"ok": True, "username": denied_user.username}), 200


@app.route("/api/players", methods=["GET", "POST"])
@api_login_required
def api_players():
    if request.method == "POST":
        payload = _api_json_payload()
        if not payload:
            return jsonify({"error": "Invalid request body"}), 400
        name = (payload.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        if Player.query.filter_by(name=name).first():
            return jsonify({"error": "A player with that name already exists"}), 409
        player = Player(name=name)
        db.session.add(player)
        db.session.flush()
        default_pod = Pod.query.filter_by(slug=DEFAULT_POD_SLUG).first()
        if default_pod:
            ensure_membership(default_pod.id, player.id)
        db.session.commit()
        return jsonify({"id": player.id, "name": player.name, "wins": 0, "played": 0, "winrate": 0.0, "deck_count": 0}), 201

    players_list = Player.query.order_by(Player.name.asc()).all()
    result = []
    for p in players_list:
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        won = Game.query.filter_by(winner_id=p.id).count()
        deck_count = Deck.query.filter_by(player_id=p.id).count()
        winrate = round((won / played) * 100, 1) if played else 0.0
        result.append({
            "id": p.id,
            "name": p.name,
            "wins": won,
            "played": played,
            "winrate": winrate,
            "deck_count": deck_count,
        })
    return jsonify(result)


@app.route("/api/pods", methods=["GET", "POST"])
@api_login_required
def api_pods():
    current_user = get_current_user()
    active_pod = get_active_pod()

    if request.method == "POST":
        if not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403

        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400

        name = (payload.get("name") or "").strip()
        slug_input = (payload.get("slug") or "").strip().lower()
        slug = slug_input or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        if not name:
            return jsonify({"error": "name is required"}), 400
        if not slug:
            return jsonify({"error": "slug is required"}), 400
        if Pod.query.filter((Pod.name == name) | (Pod.slug == slug)).first():
            return jsonify({"error": "Pod with same name or slug already exists."}), 409

        pod = Pod(name=name, slug=slug, is_active=True)
        db.session.add(pod)
        db.session.flush()
        for player in Player.query.all():
            ensure_membership(pod.id, player.id, role="member")
        db.session.commit()
        return jsonify(_serialize_pod_detail(pod, current_user, active_pod_id=active_pod.id if active_pod else None)), 201

    pods_list = get_accessible_pods(current_user)
    return jsonify({
        "active_pod_id": active_pod.id if active_pod else None,
        "can_create_pod": bool(current_user and current_user.is_admin),
        "pods": [
            _serialize_pod_summary(pod, current_user, active_pod_id=active_pod.id if active_pod else None)
            for pod in pods_list
        ],
    })


@app.route("/api/pods/<int:pod_id>", methods=["GET", "PATCH", "DELETE"])
@api_login_required
def api_pod_detail(pod_id):
    current_user = get_current_user()
    active_pod = get_active_pod()
    pod = db.session.get(Pod, pod_id)
    if not pod:
        return jsonify({"error": "Not found"}), 404

    allowed_ids = {candidate.id for candidate in get_accessible_pods(current_user)}
    if request.method == "GET":
        if pod.id not in allowed_ids and not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(_serialize_pod_detail(pod, current_user, active_pod_id=active_pod.id if active_pod else None))

    if request.method == "PATCH":
        if not can_manage_pod(current_user, pod_id):
            return jsonify({"error": "Forbidden"}), 403

        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400

        new_name = (payload.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "name is required"}), 400
        duplicate = Pod.query.filter(Pod.id != pod_id, Pod.name == new_name).first()
        if duplicate:
            return jsonify({"error": "A pod with that name already exists."}), 409

        pod.name = new_name
        db.session.commit()
        return jsonify(_serialize_pod_detail(pod, current_user, active_pod_id=active_pod.id if active_pod else None))

    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    if pod.slug == DEFAULT_POD_SLUG:
        return jsonify({"error": "The default pod cannot be deleted."}), 409

    games_count = Game.query.filter_by(pod_id=pod_id).count()
    if games_count > 0:
        return jsonify({"error": "Cannot delete pod with recorded games. Retire it instead."}), 409

    PodMembership.query.filter_by(pod_id=pod_id).delete()
    if session.get("active_pod_id") == pod_id:
        session.pop("active_pod_id", None)
        session.modified = True
    db.session.delete(pod)
    db.session.commit()
    return jsonify({"ok": True}), 200


@app.route("/api/pods/<int:pod_id>/switch", methods=["POST"])
@api_login_required
def api_switch_pod(pod_id):
    current_user = get_current_user()
    pod = db.session.get(Pod, pod_id)
    if not pod or not pod.is_active:
        return jsonify({"error": "Not found"}), 404

    allowed_ids = {candidate.id for candidate in get_accessible_pods(current_user)}
    if pod.id not in allowed_ids:
        return jsonify({"error": "Forbidden"}), 403

    session["active_pod_id"] = pod.id
    session.modified = True
    return jsonify({
        "ok": True,
        "active_pod_id": pod.id,
        "pod": _serialize_pod_summary(pod, current_user, active_pod_id=pod.id),
    })


@app.route("/api/pods/<int:pod_id>/retire", methods=["POST"])
@api_login_required
def api_retire_pod(pod_id):
    current_user = get_current_user()
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403

    pod = db.session.get(Pod, pod_id)
    if not pod:
        return jsonify({"error": "Not found"}), 404
    if pod.slug == DEFAULT_POD_SLUG:
        return jsonify({"error": "The default pod cannot be retired."}), 409

    pod.is_active = False
    db.session.commit()
    if session.get("active_pod_id") == pod_id:
        session.pop("active_pod_id", None)
        session.modified = True
    return jsonify(_serialize_pod_summary(pod, current_user, active_pod_id=None))


@app.route("/api/pods/<int:pod_id>/restore", methods=["POST"])
@api_login_required
def api_restore_pod(pod_id):
    current_user = get_current_user()
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403

    pod = db.session.get(Pod, pod_id)
    if not pod:
        return jsonify({"error": "Not found"}), 404

    pod.is_active = True
    db.session.commit()
    active_pod = get_active_pod()
    return jsonify(_serialize_pod_summary(pod, current_user, active_pod_id=active_pod.id if active_pod else None))


@app.route("/api/pods/<int:pod_id>/members", methods=["POST"])
@api_login_required
def api_add_pod_member(pod_id):
    current_user = get_current_user()
    if not can_manage_pod(current_user, pod_id):
        return jsonify({"error": "Forbidden"}), 403

    pod = db.session.get(Pod, pod_id)
    if not pod or not pod.is_active:
        return jsonify({"error": "Not found"}), 404

    payload = _api_json_payload()
    if payload is None:
        return jsonify({"error": "Invalid request body"}), 400

    player_id = payload.get("player_id")
    if not isinstance(player_id, int):
        return jsonify({"error": "player_id must be an integer"}), 400

    role = (payload.get("role") or "member").strip().lower()
    if role not in {"member", "podmaster"}:
        role = "member"
    if role == "podmaster" and not current_user.is_admin:
        role = "member"

    player = db.session.get(Player, player_id)
    if not player:
        return jsonify({"error": "Player not found."}), 404

    ensure_membership(pod_id, player_id, role=role)
    db.session.commit()
    active_pod = get_active_pod()
    return jsonify(_serialize_pod_detail(pod, current_user, active_pod_id=active_pod.id if active_pod else None)), 200


@app.route("/api/pods/<int:pod_id>/members/<int:player_id>", methods=["PATCH", "DELETE"])
@api_login_required
def api_pod_member_detail(pod_id, player_id):
    current_user = get_current_user()
    if not can_manage_pod(current_user, pod_id):
        return jsonify({"error": "Forbidden"}), 403

    membership = PodMembership.query.filter_by(pod_id=pod_id, player_id=player_id).first()
    if not membership:
        return jsonify({"error": "Not found"}), 404

    if request.method == "PATCH":
        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400

        role = (payload.get("role") or "member").strip().lower()
        if role not in {"member", "podmaster"}:
            role = "member"
        if role == "podmaster" and not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        membership.role = role
        db.session.commit()
    else:
        if membership.role == "podmaster":
            podmasters_left = PodMembership.query.filter_by(pod_id=pod_id, role="podmaster").count()
            if podmasters_left <= 1 and not current_user.is_admin:
                return jsonify({"error": "At least one podmaster must remain. Ask an admin."}), 409
        db.session.delete(membership)
        db.session.commit()
        if session.get("active_pod_id") == pod_id:
            session.pop("active_pod_id", None)
            session.modified = True

    pod = db.session.get(Pod, pod_id)
    if not pod:
        return jsonify({"ok": True}), 200
    active_pod = get_active_pod()
    return jsonify(_serialize_pod_detail(pod, current_user, active_pod_id=active_pod.id if active_pod else None)), 200


@app.route("/api/players/<int:player_id>", methods=["GET", "PATCH", "DELETE"])
@api_login_required
def api_player_detail(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        return jsonify({"error": "Not found"}), 404

    if request.method == "PATCH":
        if not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        payload = _api_json_payload()
        if not payload:
            return jsonify({"error": "Invalid request body"}), 400
        new_name = (payload.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "name is required"}), 400
        if new_name != player.name and Player.query.filter_by(name=new_name).first():
            return jsonify({"error": "A player with that name already exists"}), 409
        player.name = new_name
        db.session.commit()
        return jsonify({"id": player.id, "name": player.name}), 200

    if request.method == "DELETE":
        if not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        if player.user_id is not None:
            return jsonify({"error": "Cannot delete a user-linked player"}), 409
        played = GameParticipant.query.filter_by(player_id=player_id).count()
        won = Game.query.filter_by(winner_id=player_id).count()
        started = Game.query.filter_by(starting_player_id=player_id).count()
        if played > 0 or won > 0 or started > 0:
            return jsonify({"error": "Cannot delete a player who appears in recorded games"}), 409
        for d in player.decks:
            used = GameParticipant.query.filter_by(deck_id=d.id).count()
            if used > 0:
                return jsonify({"error": f"Cannot delete player: deck '{d.name}' has recorded games"}), 409
        for d in list(player.decks):
            db.session.delete(d)
        PodMembership.query.filter_by(player_id=player_id).delete()
        db.session.delete(player)
        db.session.commit()
        return jsonify({"ok": True}), 200

    decks = Deck.query.filter_by(player_id=player.id).order_by(Deck.name.asc()).all()
    games_played = GameParticipant.query.filter_by(player_id=player.id).count()
    games_won = Game.query.filter_by(winner_id=player.id).count()
    winrate = round((games_won / games_played) * 100, 1) if games_played else 0.0
    deck_list = []
    for d in decks:
        deck_wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id)
            .count()
        )
        deck_games = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_winrate = round((deck_wins / deck_games) * 100, 1) if deck_games else 0.0
        deck_list.append({
            "id": d.id,
            "name": d.name,
            "commander": d.commander_name or d.commander,
            "retired": d.retired,
            "planned": d.planned,
            "wins": deck_wins,
            "games": deck_games,
            "winrate": deck_winrate,
            "art_url": d.commander_art_url,
        })
    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.player_id == player.id)
        .order_by(Game.date.desc())
        .limit(20)
        .all()
    )
    recent_games = []
    for gp in participations:
        game = gp.game
        participant_count = GameParticipant.query.filter_by(game_id=game.id).count()
        recent_games.append({
            "game_id": game.id,
            "date": game.date.isoformat(),
            "won": game.winner_id == player.id,
            "deck_id": gp.deck_id,
            "deck_name": gp.deck.name,
            "win_type": game.win_type,
            "participant_count": participant_count,
        })
    return jsonify({
        "id": player.id,
        "name": player.name,
        "games_played": games_played,
        "games_won": games_won,
        "winrate": winrate,
        "decks": deck_list,
        "recent_games": recent_games,
    })


@app.route("/api/players/<int:player_id>/export")
@api_login_required
def api_player_export(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        return jsonify({"error": "Not found"}), 404
    decks = Deck.query.filter_by(player_id=player.id).order_by(Deck.name.asc()).all()
    games_played = GameParticipant.query.filter_by(player_id=player.id).count()
    games_won = Game.query.filter_by(winner_id=player.id).count()
    games_started = Game.query.filter_by(starting_player_id=player.id).count()
    winrate = round((games_won / games_played) * 100, 1) if games_played else 0.0
    decks_data = []
    for d in decks:
        deck_wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id)
            .count()
        )
        deck_games = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_losses = max(0, deck_games - deck_wins)
        deck_winrate = round((deck_wins / deck_games) * 100, 1) if deck_games else 0.0
        tags = {}
        try:
            tags = json.loads(d.tags_json or "{}")
        except (ValueError, TypeError):
            pass
        decks_data.append({
            "id": d.id,
            "name": d.name,
            "commander": d.commander_name or d.commander,
            "color_identity": d.color_identity,
            "retired": d.retired,
            "planned": d.planned,
            "decklist": d.decklist_text or "",
            "stats": {
                "games": deck_games,
                "wins": deck_wins,
                "losses": deck_losses,
                "winrate": deck_winrate,
            },
            "tags": tags,
        })
    participations = (
        GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.player_id == player.id)
        .order_by(Game.date.desc())
        .all()
    )
    games_data = []
    for gp in participations:
        game = gp.game
        participant_count = GameParticipant.query.filter_by(game_id=game.id).count()
        games_data.append({
            "game_id": game.id,
            "date": game.date.isoformat() if game.date else None,
            "won": game.winner_id == player.id,
            "deck_id": gp.deck_id,
            "deck_name": gp.deck.name if gp.deck else None,
            "commander": (gp.deck.commander_name or gp.deck.commander) if gp.deck else None,
            "participant_count": participant_count,
            "win_type": canonicalize_win_type(game.win_type) if game.win_type else None,
            "salt_count": gp.salt_count,
            "mana_fucked": gp.mana_fucked,
            "misplayed": gp.misplayed,
            "life_delta": gp.life_delta_total,
        })
    return jsonify({
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "player": {
            "id": player.id,
            "name": player.name,
            "linked_account": player.user_id is not None,
        },
        "stats": {
            "games_played": games_played,
            "games_won": games_won,
            "games_started": games_started,
            "winrate": winrate,
        },
        "decks": decks_data,
        "games": games_data,
    })


@app.route("/api/games", methods=["GET", "POST"])
@api_login_required
def api_games_list():
    current_user = get_current_user()
    if request.method == "POST":
        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400
        parsed_payload, error = _api_parse_manual_game_payload(payload, current_user)
        if error is not None:
            return error

        active_pod = get_active_pod()
        if not active_pod:
            return jsonify({"error": "No active pod available"}), 400

        game = Game(
            winner_id=parsed_payload["winner_id"],
            starting_player_id=parsed_payload["starting_player_id"],
            win_type=parsed_payload["win_type"],
            ending_turn=parsed_payload["ending_turn"],
            note=parsed_payload["note"],
            date=parsed_payload["date"],
            pod_id=active_pod.id,
            duration_seconds=parsed_payload["duration_seconds"],
        )
        db.session.add(game)
        db.session.flush()

        for participant in parsed_payload["participants"]:
            participant_flags_json = parsed_payload["participant_flags_by_player"].get(participant["player_id"])
            hot_fields = participant_hot_fields_from_flags(participant_flags_json)
            db.session.add(
                GameParticipant(
                    game_id=game.id,
                    player_id=participant["player_id"],
                    deck_id=participant["deck_id"],
                    seat_position=participant["seat_position"],
                    flags_json=participant_flags_json,
                    salt_count=int(hot_fields["salt_count"]),
                    mana_fucked=bool(hot_fields["mana_fucked"]),
                    misplayed=bool(hot_fields["misplayed"]),
                    life_delta_total=int(hot_fields["life_delta_total"]),
                )
            )

        db.session.commit()
        return api_game_detail(game.id)

    game_q, scope, active_pod = game_query_for_scope()
    player_id = request.args.get("player_id", type=int)
    deck_id = request.args.get("deck_id", type=int)
    winner_id = request.args.get("winner_id", type=int)
    date_from_raw = request.args.get("date_from", "").strip()
    date_to_raw = request.args.get("date_to", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 25, type=int), 100)
    date_from = None
    date_to = None
    try:
        if date_from_raw:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
    except ValueError:
        date_from = None
    try:
        if date_to_raw:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        date_to = None
    q = game_q
    if winner_id:
        q = q.filter(Game.winner_id == winner_id)
    if date_from:
        q = q.filter(Game.date >= date_from)
    if date_to:
        q = q.filter(Game.date <= date_to)
    if player_id:
        gp_player = aliased(GameParticipant)
        q = q.join(gp_player, gp_player.game_id == Game.id).filter(gp_player.player_id == player_id)
    if deck_id:
        gp_deck = aliased(GameParticipant)
        q = q.join(gp_deck, gp_deck.game_id == Game.id).filter(gp_deck.deck_id == deck_id)
    q = q.distinct().order_by(Game.date.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    games_page = pagination.items
    game_ids = [g.id for g in games_page]
    parts = GameParticipant.query.filter(
        GameParticipant.game_id.in_(game_ids if game_ids else [-1])
    ).all()
    game_parts_map: dict = {}
    for gp in parts:
        game_parts_map.setdefault(gp.game_id, []).append(gp)
    games_data = []
    for g in games_page:
        gps = game_parts_map.get(g.id, [])
        games_data.append({
            "id": g.id,
            "date": g.date.isoformat(),
            "winner": {"id": g.winner_id, "name": g.winner.name},
            "win_type": g.win_type,
            "ending_turn": g.ending_turn,
            "participants": [
                {
                    "player_id": gp.player_id,
                    "player_name": gp.player.name,
                    "deck_id": gp.deck_id,
                    "deck_name": gp.deck.name,
                    "commander": gp.deck.commander_name or gp.deck.commander,
                    "art_url": gp.deck.commander_art_url,
                    "won": g.winner_id == gp.player_id,
                }
                for gp in gps
            ],
        })
    return jsonify({
        "games": games_data,
        "page": pagination.page,
        "pages": pagination.pages or 1,
        "total": pagination.total,
        "per_page": per_page,
    })


@app.route("/api/games/<int:game_id>", methods=["GET", "DELETE"])
@api_login_required
def api_game_detail(game_id):
    current_user = get_current_user()
    game = db.session.get(Game, game_id)
    if not game:
        return jsonify({"error": "Not found"}), 404
    if request.method == "DELETE":
        if not current_user.is_admin:
            return jsonify({"error": "Forbidden"}), 403
        GameParticipant.query.filter_by(game_id=game_id).delete()
        db.session.delete(game)
        db.session.commit()
        return jsonify({"ok": True}), 200
    parts = GameParticipant.query.filter_by(game_id=game.id).all()
    valid_player_ids = {part.player_id for part in parts}

    def commander_damage_for(participant: GameParticipant) -> dict[str, int]:
        payload = {}
        if participant.flags_json:
            try:
                loaded = json.loads(participant.flags_json)
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                payload = loaded
        sanitized_card_state = sanitize_card_state_payload(payload.get("card_state", {}), valid_player_ids) or {}
        return sanitized_card_state.get("commander_damage", {})

    return jsonify({
        "id": game.id,
        "date": game.date.isoformat(),
        "winner": {"id": game.winner_id, "name": game.winner.name},
        "win_type": game.win_type,
        "ending_turn": game.ending_turn,
        "note": game.note,
        "starting_player": {"id": game.starting_player_id, "name": game.starting_player.name} if game.starting_player else None,
        "participants": [
            {
                "player_id": gp.player_id,
                "player_name": gp.player.name,
                "deck_id": gp.deck_id,
                "deck_name": gp.deck.name,
                "commander": gp.deck.commander_name or gp.deck.commander,
                "art_url": gp.deck.commander_art_url,
                "won": game.winner_id == gp.player_id,
                "seat_position": gp.seat_position,
                "salt_count": gp.salt_count,
                "mana_fucked": gp.mana_fucked,
                "misplayed": gp.misplayed,
                "commander_damage": commander_damage_for(gp),
            }
            for gp in parts
        ],
    })


@app.route("/api/decks", methods=["GET", "POST"])
@api_login_required
def api_decks():
    current_user = get_current_user()
    if request.method == "POST":
        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400

        if not current_user.is_admin and "player_id" in payload:
            requested_player_id = payload.get("player_id")
            current_player_id = current_user.player.id if current_user.player else None
            if requested_player_id != current_player_id:
                return jsonify({"error": "Forbidden"}), 403

        raw_import = payload.get("raw_import")
        if raw_import is None:
            raw_import = payload.get("decklist_text")
        if raw_import is not None and not isinstance(raw_import, str):
            return jsonify({"error": "raw_import/decklist_text must be a string"}), 400

        player_id = _api_deck_owner_id_from_payload(payload, current_user)
        if player_id is None:
            if current_user.is_admin:
                return jsonify({"error": "player_id is required and must reference an existing player"}), 400
            return jsonify({"error": "No player profile found for your account."}), 400

        try:
            deck, diagnostics = _create_deck_from_payload(
                {
                    "name": payload.get("name") or "",
                    "commander": payload.get("commander") or "",
                    "raw_import": raw_import or "",
                    "imported_from": "text",
                    "retired": payload.get("retired", False),
                    "planned": payload.get("planned", False),
                },
                player_id=player_id,
                is_admin=bool(current_user.is_admin),
            )
        except DeckParserError as exc:
            return jsonify({"error": f"Deck setup failed: {exc}"}), 400
        except DeckPayloadError as exc:
            message = str(exc)
            if message == "Deck name is required.":
                return jsonify({"error": "name is required"}), 400
            if message == "Commander is required, or include one in the imported list.":
                return jsonify({"error": "commander is required, or include one in imported deck data"}), 400
            return jsonify({"error": message}), 400

        db.session.add(deck)
        db.session.commit()

        response_payload = _serialize_deck_summary(deck)
        if diagnostics["tag_diagnostics"].get("unresolved_count", 0) > 0:
            response_payload["tag_diagnostics"] = diagnostics["tag_diagnostics"]
        return jsonify(response_payload), 201

    player_id = request.args.get("player_id", type=int)
    show_retired = request.args.get("show_retired", "0") == "1"
    q = Deck.query
    if not current_user.is_admin:
        if current_user.player:
            q = q.filter_by(player_id=current_user.player.id)
    elif player_id:
        q = q.filter_by(player_id=player_id)
    if not show_retired:
        q = q.filter(Deck.retired == False, Deck.planned == False)
    decks = q.order_by(Deck.name.asc()).all()
    result = []
    deck_tags_cache: dict[int, dict[str, bool]] = {}
    for d in decks:
        result.append(_serialize_deck_summary(d, deck_tags_cache=deck_tags_cache))
    return jsonify(result)


@app.route("/api/decks/<int:deck_id>", methods=["GET", "PATCH", "PUT", "DELETE"])
@api_login_required
def api_deck_detail(deck_id):
    current_user = get_current_user()
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return jsonify({"error": "Not found"}), 404

    if request.method == "DELETE":
        if not current_user.is_admin and (not current_user.player or deck.player_id != current_user.player.id):
            return jsonify({"error": "Forbidden"}), 403
        used = GameParticipant.query.filter_by(deck_id=deck.id).count()
        if used > 0:
            return jsonify({"error": "Cannot delete a deck that has been used in recorded games."}), 409
        db.session.delete(deck)
        db.session.commit()
        return jsonify({"ok": True}), 200

    if request.method in {"PATCH", "PUT"}:
        if not current_user.is_admin and (not current_user.player or deck.player_id != current_user.player.id):
            return jsonify({"error": "Forbidden"}), 403

        payload = _api_json_payload()
        if payload is None:
            return jsonify({"error": "Invalid request body"}), 400

        if request.method == "PUT":
            name = payload.get("name")
        elif "name" in payload:
            name = payload.get("name")
            if not (isinstance(name, str) and name.strip()):
                return jsonify({"error": "name must be a non-empty string"}), 400
        else:
            name = deck.name

        raw_import = payload.get("raw_import")
        if raw_import is None and "decklist_text" in payload:
            raw_import = payload.get("decklist_text")
        if raw_import is not None and not isinstance(raw_import, str):
            return jsonify({"error": "raw_import/decklist_text must be a string"}), 400

        if current_user.is_admin and "player_id" in payload:
            owner_id = payload.get("player_id")
            if not isinstance(owner_id, int):
                return jsonify({"error": "player_id must be an integer"}), 400
            owner = db.session.get(Player, owner_id)
            if not owner:
                return jsonify({"error": "player_id must reference an existing player"}), 400

        try:
            deck, diagnostics = _update_deck_from_payload(
                deck,
                {
                    "name": name,
                    "commander": payload.get("commander") if "commander" in payload else deck.commander,
                    "raw_import": raw_import if raw_import is not None else "",
                    "imported_from": "text",
                    "player_id": payload.get("player_id"),
                    "retired": payload.get("retired") if "retired" in payload else deck.retired,
                    "planned": payload.get("planned") if "planned" in payload else deck.planned,
                },
                is_admin=bool(current_user.is_admin),
                allow_owner_update=(current_user.is_admin and "player_id" in payload),
                require_commander_input=False,
            )
        except DeckParserError as exc:
            return jsonify({"error": f"Deck setup failed: {exc}"}), 400
        except DeckPayloadError as exc:
            message = str(exc)
            if request.method == "PUT" and message == "Deck name is required.":
                return jsonify({"error": "name is required"}), 400
            if message == "Commander is required, or include one in the imported list.":
                return jsonify({"error": "commander is required, or include one in imported deck data"}), 400
            return jsonify({"error": message}), 400

        db.session.commit()
        response_payload = _serialize_deck_detail(deck)
        if diagnostics["tag_diagnostics"].get("unresolved_count", 0) > 0:
            response_payload["tag_diagnostics"] = diagnostics["tag_diagnostics"]
        return jsonify(response_payload), 200

    if not current_user.is_admin and (not current_user.player or deck.player_id != current_user.player.id):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(_serialize_deck_detail(deck))


@app.route("/api/join/<token>")
def api_join_get(token):
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return jsonify({"error": "Game not found"}), 404
    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []
    return jsonify({"participants": participants, "token": token})


@app.route("/api/join/<token>", methods=["POST"])
def api_join_claim(token):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400
    active_game_rec = ActiveGame.query.filter_by(token=token).first()
    if not active_game_rec:
        return jsonify({"error": "Game not found"}), 404
    try:
        participants = json.loads(active_game_rec.participants_json)
    except (json.JSONDecodeError, Exception):
        participants = []
    player_id_raw = data.get("player_id")
    try:
        player_id = int(player_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid player_id"}), 400
    valid_player_ids = {p["player_id"] for p in participants}
    if player_id not in valid_player_ids:
        return jsonify({"error": "Invalid player selection"}), 400
    session[f"game_join_{token}"] = player_id
    session.modified = True
    try:
        state = json.loads(active_game_rec.state_json)
    except (json.JSONDecodeError, Exception):
        state = {}
    return jsonify({
        "token": token,
        "player_id": player_id,
        "participants": participants,
        "state": state,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
