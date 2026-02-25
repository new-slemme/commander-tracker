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
import requests
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, text, case
from sqlalchemy.orm import aliased
from functools import wraps
from uuid import uuid4

from deck_import import DeckParserError, parse_deck_input, parse_plaintext_decklist

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-default-change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/commander.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

DEFAULT_POD_NAME = "Der Keller – Die Salzmine"
DEFAULT_POD_SLUG = "der-keller-die-salzmine"

ART_DIR = Path("/data/art")
ART_DIR.mkdir(parents=True, exist_ok=True)
COMMANDER_ART_DIR = ART_DIR / "commander_art"
COMMANDER_ART_DIR.mkdir(parents=True, exist_ok=True)
CARD_ART_DIR = ART_DIR / "card_art"
CARD_ART_DIR.mkdir(parents=True, exist_ok=True)

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

MAX_PARTICIPANT_FLAGS_PAYLOAD_BYTES = 4096
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

        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            continue
        if not isinstance(life_delta, int) or isinstance(life_delta, bool):
            continue
        if not isinstance(mana_fucked, bool):
            continue
        if not isinstance(misplayed, bool):
            continue

        parsed_stats.append(
            {
                "turn": turn,
                "life_delta": life_delta,
                "mana_fucked": mana_fucked,
                "misplayed": misplayed,
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

    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)

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

    winner_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
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
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
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
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey("deck.id"), nullable=False)
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
        "poison": "poison" in oracle_text,
        "proliferate": "proliferate" in oracle_text,
        "energy": "{e}" in oracle_text,
        "experience": "experience counter" in oracle_text,
    }


def compute_deck_tags(decklist_cards: list[str]) -> dict:
    tags = {
        "monarch": False,
        "poison": False,
        "proliferate": False,
        "energy": False,
        "experience": False,
    }
    seen_names: set[str] = set()

    for raw_name in decklist_cards:
        name = (raw_name or "").strip()
        if not name:
            continue

        lowered = name.lower()
        if lowered in seen_names:
            continue
        seen_names.add(lowered)

        card_json = scryfall_named_exact(name)
        card_tags = analyze_scryfall_card(card_json)
        for key in tags:
            tags[key] = tags[key] or card_tags.get(key, False)

    return tags


def extract_decklist_card_names(parsed: dict) -> list[str]:
    parsed_sections = parsed.get("sections", {}) or {}
    return [
        entry.get("name", "")
        for entries in parsed_sections.values()
        for entry in (entries or [])
    ]


def compute_deck_tags_from_text(decklist_text: str | None) -> dict:
    raw_text = (decklist_text or "").strip()
    if not raw_text:
        return {}

    parsed = parse_plaintext_decklist(raw_text).as_dict()
    return compute_deck_tags(extract_decklist_card_names(parsed))


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


def cache_card_art_by_name(card_name: str) -> str | None:
    normalized_name = (card_name or "").strip()
    if not normalized_name:
        return None

    card = scryfall_named_exact(normalized_name)
    if not card:
        return None

    image_url = extract_normal_image(card)
    if not image_url:
        return None

    card_id = card.get("id") or _safe_filename(normalized_name)
    safe_name = _safe_filename(card.get("name") or normalized_name) or "card"
    filename = f"{safe_name}_{card_id}.jpg"
    out_path = CARD_ART_DIR / filename
    web_path = f"/art/card_art/{filename}"

    if out_path.exists() and out_path.stat().st_size > 0:
        return web_path

    try:
        req = Request(
            image_url,
            headers={
                "User-Agent": "CommanderTracker/1.0 (https://edh.figurensohn.de)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=20) as resp:
            data = resp.read()

        if not data:
            print("cache_card_art_by_name: empty response", image_url)
            return None

        out_path.write_bytes(data)
        return web_path

    except (HTTPError, URLError) as e:
        print("cache_card_art_by_name failed:", e, image_url)
        return None
    except Exception as e:
        print("cache_card_art_by_name unexpected error:", e, image_url)
        return None


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


# -------------------------
# Auth helpers / guards
# -------------------------


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


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


@app.context_processor
def inject_pod_context():
    user = get_current_user()
    if not user:
        return {
            "nav_active_pod": None,
            "nav_available_pods": [],
            "use_sigtaara": False,
        }

    return {
        "nav_active_pod": get_active_pod(),
        "nav_available_pods": get_accessible_pods(user),
        "use_sigtaara": bool(user.use_sigtaara),
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
                get_active_pod()
                return None

        # Allow auth routes + static assets + art
        if request.endpoint not in ("login", "register", "static", "art"):
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
            u.mana_fucked_salt_value = mana_fucked_salt_value
            u.misplayed_salt_value = misplayed_salt_value
            if u.player:
                u.player.name = new_display  # keep in sync with game tracking

            db.session.commit()
            session["display_name"] = new_display
            session["use_sigtaara"] = use_sigtaara
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


@app.route("/admin/users")
@admin_required
def admin_users():
    pending = User.query.filter_by(is_active=False).order_by(User.created_at.asc()).all()
    active = User.query.filter_by(is_active=True).order_by(User.created_at.desc()).all()
    pending_requests_by_user_id = {
        req.user_id: req
        for req in RegistrationRequest.query.filter_by(status="pending").all()
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
    if not can_approve_registration_request(me, registration_request):
        abort(403)

    u = registration_request.user
    if not u:
        abort(404)

    if registration_request.status != "pending" or u.is_active:
        flash("Only pending users can be denied.")
        return redirect(url_for("registration_requests"))

    registration_request.status = "denied"
    registration_request.reviewed_at = datetime.utcnow()
    registration_request.reviewed_by_user_id = me.id if me else None

    if u.player:
        db.session.delete(u.player)
    db.session.delete(u)
    db.session.commit()

    flash(f"Denied and removed user '{u.username}'.")
    return redirect(url_for("registration_requests"))




@app.route("/admin/users/<int:user_id>/deny", methods=["POST"])
@admin_required
def admin_deny_user(user_id):
    registration_request = RegistrationRequest.query.filter_by(user_id=user_id).first()
    if not registration_request:
        flash("No pending registration request found for that user.")
        return redirect(url_for("admin_users"))
    return deny_registration_request(registration_request.id)

@app.route("/")
def index():
    game_q, scope, active_pod = game_query_for_scope()
    game_ids_subquery = game_q.with_entities(Game.id)
    current_user = get_current_user()
    available_pods = get_accessible_pods(current_user)

    # Player stats
    players = Player.query.all()
    player_stats = []
    for p in players:
        wins = game_q.filter_by(winner_id=p.id).count()
        played = GameParticipant.query.join(Game).filter(GameParticipant.player_id == p.id, Game.id.in_(game_ids_subquery)).count()
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

    # Deck stats
    decks = Deck.query.all()
    deck_stats = []
    for d in decks:
        wins = (
            GameParticipant.query.join(Game)
            .filter(GameParticipant.deck_id == d.id, Game.winner_id == GameParticipant.player_id, Game.id.in_(game_ids_subquery))
            .count()
        )
        uses = GameParticipant.query.join(Game).filter(GameParticipant.deck_id == d.id, Game.id.in_(game_ids_subquery)).count()
        winrate = round(wins / uses * 100, 1) if uses > 0 else 0.0
        deck_stats.append({"deck": d, "wins": wins, "uses": uses, "winrate": winrate})

    deck_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))
    top_decks = deck_stats[:6]

    # Recent games
    recent_games = game_q.order_by(Game.date.desc()).limit(10).all()
    game_parts = {g.id: GameParticipant.query.filter_by(game_id=g.id).all() for g in recent_games}

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

    return render_template(
        "index.html",
        player_stats=player_stats,
        deck_stats=top_decks,
        recent_games=recent_games,
        game_parts=game_parts,
        top_players=top_players,
        best_deck=best_deck,
        last_winning_deck=last_winning_deck,
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
    for d in decks_list:
        used = GameParticipant.query.filter_by(deck_id=d.id).count()
        deck_can_delete[d.id] = (used == 0)

    return render_template(
        "decks.html",
        decks=decks_list,
        players=players_list,
        selected_player_id=player_id,
        deck_stats=stats,
        deck_can_delete=deck_can_delete,
        show_retired=show_retired,
        is_admin=bool(u and u.is_admin),
    )


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


@app.route("/add_deck", methods=["POST"])
@login_required
def add_deck():
    u = get_current_user()

    name = request.form.get("name", "").strip()
    commander_input = request.form.get("commander", "").strip()
    custom_commander_art_url = request.form.get("custom_commander_art_url", "").strip()
    custom_card_art_url = request.form.get("custom_card_art_url", "").strip()
    custom_commander_art_upload = request.files.get("custom_commander_art_file")
    custom_card_art_upload = request.files.get("custom_card_art_file")

    if not (name and commander_input):
        flash("Deck name and commander are required.")
        return redirect(url_for("decks"))

    commander_url_invalid = (
        not has_uploaded_custom_art(custom_commander_art_upload)
        and not is_valid_custom_art_url(custom_commander_art_url)
    )
    card_url_invalid = (
        not has_uploaded_custom_art(custom_card_art_upload)
        and not is_valid_custom_art_url(custom_card_art_url)
    )
    if commander_url_invalid or card_url_invalid:
        flash("Custom art URLs must be valid http(s) links up to 500 characters.")
        return redirect(url_for("decks"))

    # Decide owner:
    if u.is_admin:
        player_id = request.form.get("player_id", type=int)  # admin can choose
        if not player_id:
            flash("Owner is required.")
            return redirect(url_for("decks"))
    else:
        if not u.player:
            flash("No player profile found for your account.")
            return redirect(url_for("decks"))
        player_id = u.player.id

    parsed_import = None
    imported_from = None
    try:
        raw_import, imported_from = _extract_deck_import_text()
        if raw_import:
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
    except DeckParserError as exc:
        flash(f"Deck setup failed: {exc}")
        return redirect(url_for("decks"))

    resolved_commander = parsed_import.get("commander") if parsed_import else None
    commander_to_set = (resolved_commander or commander_input).strip()

    if not commander_to_set:
        flash("Commander is required, or include one in the imported list.")
        return redirect(url_for("decks"))

    deck = Deck(name=name, commander=commander_to_set, player_id=player_id)
    deck.custom_commander_art_url = custom_commander_art_url_value
    deck.commander_local_art_custom = custom_commander_art_local
    deck.custom_card_art_url = custom_card_art_url_value
    deck.custom_card_art_local = custom_card_art_local
    deck.planned = bool(request.form.get("planned"))
    if parsed_import:
        deck.decklist_text = _render_decklist_text(parsed_import)
        tags = compute_deck_tags(extract_decklist_card_names(parsed_import))
        deck.tags_json = json.dumps(tags, separators=(",", ":"), sort_keys=True)

    commander_meta = resolve_commander_metadata(commander_to_set)
    deck.commander = commander_meta["commander"] or commander_to_set
    deck.commander_name = commander_meta["commander_name"]
    deck.commander_scryfall_id = commander_meta["commander_scryfall_id"]
    deck.commander_art_crop_url = commander_meta["commander_art_crop_url"]
    deck.commander_local_art_crop = commander_meta["commander_local_art_crop"]
    deck.color_identity = commander_meta["color_identity"]

    db.session.add(deck)
    db.session.commit()

    if parsed_import:
        imported_cards = _count_imported_cards(parsed_import)
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
            f"Deck added. {imported_cards} cards imported from {imported_from}; {commander_msg}{warning_msg}."
        )
    else:
        flash("Deck added.")
    return redirect(url_for("decks"))


@app.route("/api/card-art")
def api_card_art():
    card_name = (request.args.get("name") or "").strip()
    if not card_name:
        return jsonify({"image": None})

    image = cache_card_art_by_name(card_name)
    return jsonify({"image": image})


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
        tags = compute_deck_tags_from_text(deck.decklist_text)
    except DeckParserError as exc:
        flash(f"Retag failed: {exc}")
        return redirect(next_url)

    deck.tags_json = json.dumps(tags, separators=(",", ":"), sort_keys=True)
    db.session.commit()
    flash(f"Retagged deck: {deck.name}")
    return redirect(next_url)


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

    name = request.form.get("name", "").strip()
    commander_input = request.form.get("commander", "").strip()
    custom_commander_art_url = request.form.get("custom_commander_art_url", "").strip()
    custom_card_art_url = request.form.get("custom_card_art_url", "").strip()
    custom_commander_art_upload = request.files.get("custom_commander_art_file")
    custom_card_art_upload = request.files.get("custom_card_art_file")
    if not name:
        flash("Deck name is required.")
        return redirect(request.form.get("next") or url_for("decks"))
    if not commander_input:
        flash("Commander is required.")
        return redirect(request.form.get("next") or url_for("decks"))

    commander_url_invalid = (
        not has_uploaded_custom_art(custom_commander_art_upload)
        and not is_valid_custom_art_url(custom_commander_art_url)
    )
    card_url_invalid = (
        not has_uploaded_custom_art(custom_card_art_upload)
        and not is_valid_custom_art_url(custom_card_art_url)
    )
    if commander_url_invalid or card_url_invalid:
        flash("Custom art URLs must be valid http(s) links up to 500 characters.")
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

    parsed_import = None
    imported_from = None
    try:
        raw_import, imported_from = _extract_deck_import_text()
        if raw_import:
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
    except DeckParserError as exc:
        flash(f"Deck setup failed: {exc}")
        return redirect(request.form.get("next") or url_for("decks"))

    resolved_commander = parsed_import.get("commander") if parsed_import else None
    commander_to_set = (resolved_commander or commander_input).strip()
    if not commander_to_set:
        flash("Commander is required, or include one in the imported list.")
        return redirect(request.form.get("next") or url_for("decks"))

    deck.name = name
    deck.commander = commander_to_set
    if not has_uploaded_custom_art(custom_commander_art_upload) and not custom_commander_art_url:
        custom_commander_art_local = deck.commander_local_art_custom
    if not has_uploaded_custom_art(custom_card_art_upload) and not custom_card_art_url:
        custom_card_art_local = deck.custom_card_art_local

    deck.custom_commander_art_url = custom_commander_art_url_value
    deck.commander_local_art_custom = custom_commander_art_local
    deck.custom_card_art_url = custom_card_art_url_value
    deck.custom_card_art_local = custom_card_art_local
    if parsed_import:
        deck.decklist_text = _render_decklist_text(parsed_import)
        tags = compute_deck_tags(extract_decklist_card_names(parsed_import))
        deck.tags_json = json.dumps(tags, separators=(",", ":"), sort_keys=True)

    if u.is_admin:
        owner_id = request.form.get("player_id", type=int)
        if owner_id:
            owner = db.session.get(Player, owner_id)
            if owner:
                deck.player_id = owner.id
        deck.retired = bool(request.form.get("retired"))
        deck.planned = bool(request.form.get("planned"))
        if deck.retired:
            deck.planned = False

    commander_meta = resolve_commander_metadata(commander_to_set)
    deck.commander = commander_meta["commander"] or commander_to_set
    deck.commander_name = commander_meta["commander_name"]
    deck.commander_scryfall_id = commander_meta["commander_scryfall_id"]
    deck.commander_art_crop_url = commander_meta["commander_art_crop_url"]
    deck.commander_local_art_crop = commander_meta["commander_local_art_crop"]
    deck.color_identity = commander_meta["color_identity"]

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

        flash(f"Failed to update deck: {exc}")
        return redirect(request.form.get("next") or url_for("decks"))

    if parsed_import:
        imported_cards = _count_imported_cards(parsed_import)
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
            f"Updated deck: {deck.name}. {imported_cards} cards imported from {imported_from}; "
            f"{commander_msg}{warning_msg}."
        )
    else:
        flash(f"Updated deck: {deck.name}")
    return redirect(request.form.get("next") or url_for("decks"))


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

            deck = db.session.get(Deck, d_id)
            if not deck or deck.player_id != p_id or deck.retired or deck.planned:
                return "Invalid deck for player", 400

            participants.append({
                "player_id": p_id,
                "deck_id": d_id,
                "seat_position": len(participants) + 1,
                "player_name": deck.owner.name,
                "deck_name": deck.name,
                "commander_art": deck.commander_art_url,
                "commander_art_scale": deck.commander_art_scale,
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

    return redirect(url_for("life_counter"))



@app.route("/cancel_game", methods=["POST"])
@login_required
def cancel_game():
    # Just drop the active game from session
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

    active_mechanics = {"monarch": False, "poison": False}

    for i, p in enumerate(participants, 1):
        p["index"] = i
        p["color"] = colors[(i - 1) % len(colors)]

        deck = db.session.get(Deck, p["deck_id"])
        p["commander_art"] = deck.commander_art_url if deck else None
        p["commander_art_scale"] = deck.commander_art_scale if deck else "cover"

        tags = {}
        if deck and deck.tags_json:
            try:
                loaded_tags = json.loads(deck.tags_json)
                if isinstance(loaded_tags, dict):
                    tags = loaded_tags
            except (TypeError, ValueError):
                tags = {}

        mechanics = {
            "monarch": bool(tags.get("monarch")),
            "poison": bool(tags.get("poison")) or bool(tags.get("proliferate")),
            "energy": bool(tags.get("energy")),
            "experience": bool(tags.get("experience")),
        }
        p["mechanics"] = mechanics

        active_mechanics["monarch"] = active_mechanics["monarch"] or mechanics["monarch"]
        active_mechanics["poison"] = active_mechanics["poison"] or mechanics["poison"]

    current_user = get_current_user()
    debug_ui_enabled = app.debug and request.args.get("debug_ui") == "1"
    salt_action_values = {
        "mana_fucked": max(0, int(getattr(current_user, "mana_fucked_salt_value", 1) or 0)),
        "misplayed": max(0, int(getattr(current_user, "misplayed_salt_value", 1) or 0)),
    }

    card_logic_catalog = {
        "statuses": [
            {"id": "monarch", "label": "Monarch", "icon": "👑", "kind": "exclusive"},
            {"id": "initiative", "label": "Initiative", "icon": "⚔️", "kind": "exclusive"},
            {"id": "citys_blessing", "label": "City's Blessing", "icon": "🏙️", "kind": "toggle"},
            {"id": "shroud", "label": "Shroud", "icon": "🛡️", "kind": "toggle"},
        ],
        "counters": [
            {"id": "energy", "label": "Energy", "icon": "⚡", "step": 1, "min": 0},
            {"id": "experience", "label": "Experience", "icon": "✨", "step": 1, "min": 0},
            {"id": "poison", "label": "Poison", "icon": "☠️", "step": 1, "min": 0, "max": 10},
        ],
        "commander_damage_threshold": 21,
    }

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

                        sanitized_turn_stats.append(
                            {
                                "turn": turn,
                                "life_delta": life_delta,
                                "mana_fucked": mana_fucked,
                                "misplayed": misplayed,
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
    session.pop("game_participants", None)
    session.pop("active_player_id", None)
    session.pop("timer_config", None)
    session.pop("game_started_at", None)
    session.pop("turn_number", None)
    return redirect(url_for("index"))



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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
