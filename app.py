from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
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
from sqlalchemy import func, text, inspect
from functools import wraps


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-default-change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/commander.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
ART_DIR = Path("/data/art")
ART_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------
# Models
# -------------------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(100), unique=True, nullable=False)

    password_hash = db.Column(db.String(128), nullable=False)

    is_active = db.Column(db.Boolean, default=False, nullable=False)
    is_admin  = db.Column(db.Boolean, default=False, nullable=False)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=True)

    player = db.relationship("Player", backref="user", uselist=False)

class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # keep name for stats display, but it now comes from user.display_name at creation
    name = db.Column(db.String(100), unique=True, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=True)

    decks = db.relationship("Deck", backref="owner", lazy=True)


class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    retired = db.Column(db.Boolean, nullable=False, default=False)

    # legacy / user-entered fallback
    commander = db.Column(db.String(100), nullable=False)

    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)

    # Robust commander support (best-effort filled via Scryfall)
    commander_name = db.Column(db.String(120))
    commander_scryfall_id = db.Column(db.String(40), index=True)
    commander_art_crop_url = db.Column(db.String(300))
    commander_local_art = db.Column(db.String(300))
    color_identity = db.Column(db.String(10))  # e.g. "WUBRG"


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    winner_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
    winner = db.relationship("Player", backref="won_games", lazy=True)


class GameParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
    deck_id = db.Column(db.Integer, db.ForeignKey("deck.id"), nullable=False)
    player = db.relationship("Player", backref="participations", lazy=True)
    deck = db.relationship("Deck", backref="deck_participations", lazy=True)
    game = db.relationship("Game", backref="participants", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("game_id", "player_id", name="unique_player_per_game"),
    )


# Create DB tables (note: this won't add columns to an existing SQLite table; use ALTER TABLE for that)
with app.app_context():
    db.create_all()
    
    # -------------------------
    # Bootstrap admin (5a)
    # -------------------------
    admin_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME")
    if admin_username:
        u = User.query.filter_by(username=admin_username).first()
        if u:
            changed = False
            if not getattr(u, "is_admin", False):
                u.is_admin = True
                changed = True
            if not getattr(u, "is_active", False):
                u.is_active = True
                u.approved_at = datetime.utcnow()
                changed = True
            if not getattr(u, "display_name", None):
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


def scryfall_named_exact(name: str):
    """
    Best-effort exact-name lookup.
    Returns dict or None.
    """
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
    """
    Returns art_crop URL or None.
    Handles DFC cards (card_faces).
    """
    if not card:
        return None
    if card.get("image_uris") and card["image_uris"].get("art_crop"):
        return card["image_uris"]["art_crop"]
    faces = card.get("card_faces") or []
    if faces and faces[0].get("image_uris") and faces[0]["image_uris"].get("art_crop"):
        return faces[0]["image_uris"]["art_crop"]
    return None


def download_art_crop(art_url: str, scryfall_id: str, commander_name: str) -> str | None:
    """
    Downloads art_crop into /data/art (persistent).
    Returns web path like '/art/<file>.jpg' or None.
    """
    if not (art_url and scryfall_id and commander_name):
        return None

    filename = f"{_safe_filename(commander_name)}_{scryfall_id}.jpg"
    out_path = ART_DIR / filename
    web_path = f"/art/{filename}"

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

# -------------------------
# Login Required
# -------------------------
@app.before_request
def require_login():
    if "user_id" not in session:
        # Allow auth routes + static assets
        if request.endpoint not in ("login", "register", "static"):
            return redirect(url_for("login") + "?next=" + quote(request.full_path))


# -------------------------
# Auth Routes
# -------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        display_name = request.form['display_name'].strip()
        password = request.form['password']
        confirm = request.form['confirm']

        if not username or not display_name or not password:
            flash('Username, display name, and password required')
        elif password != confirm:
            flash('Passwords do not match')
        elif User.query.filter_by(username=username).first():
            flash('Username already taken')
        elif User.query.filter_by(display_name=display_name).first() or Player.query.filter_by(name=display_name).first():
            flash('Display name already taken')
        else:
            hashed = generate_password_hash(password)
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hashed,
                is_active=False,
                is_admin=False
            )

            # Create the player via relationship (cleaner)
            user.player = Player(name=display_name)

            db.session.add(user)
            db.session.commit()

            flash('Registration submitted! Your account is pending admin approval.')
            return redirect(url_for('login'))


    return render_template('register.html')



@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            if not user.is_active:
                flash('Account pending approval. Please contact an admin.')
                return render_template('login.html')
                # safety net: ensure player exists
            if not user.player:
                # should not happen, but keeps prod stable
                user.player = Player(name=user.display_name)
            db.session.commit()
            session['user_id'] = user.id
            session['username'] = user.username
            session['display_name'] = user.display_name
            session['is_admin'] = user.is_admin
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
        new_display = request.form.get("display_name", "").strip()

        if not new_display:
            flash("Display name cannot be empty.")
            return redirect(url_for("profile"))

        # Uniqueness checks: both User.display_name and Player.name (since games use Player)
        existing_user = User.query.filter(User.display_name == new_display, User.id != u.id).first()
        existing_player = Player.query.filter(Player.name == new_display).first()

        if existing_user or (existing_player and (not u.player or existing_player.id != u.player.id)):
            flash("That display name is already taken.")
            return redirect(url_for("profile"))

        u.display_name = new_display
        if u.player:
            u.player.name = new_display  # keep in sync with game tracking
        db.session.commit()
        session["display_name"] = new_display
        flash("Display name updated.")
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
    return render_template("admin_users.html", pending=pending, active=active)

@app.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@admin_required
def admin_approve_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)
    u.is_active = True
    u.approved_at = datetime.utcnow()
    db.session.commit()
    flash(f"Approved {u.display_name}")
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/deactivate", methods=["POST"])
@admin_required
def admin_deactivate_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        abort(404)

    # prevent self-lockout
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
def fix_art_paths():
    decks = Deck.query.all()
    changed = 0
    for d in decks:
        if d.commander_local_art and d.commander_local_art.startswith("/static/commander_art/"):
            d.commander_local_art = None
            changed += 1
    db.session.commit()
    return f"Fixed {changed} decks"


@app.route("/")
def index():
    # Player stats
    players = Player.query.all()
    player_stats = []
    for p in players:
        wins = Game.query.filter_by(winner_id=p.id).count()
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        winrate = round(wins / played * 100, 1) if played > 0 else 0
        player_stats.append({"player": p, "wins": wins, "played": played, "winrate": winrate})
    player_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))
        # --- Enrich top players with most-played deck art ---
    top_players = player_stats[:3]
    
    for row in top_players:
        p = row["player"]
    
        # Find this player's most-played deck (by participations)
        #most_played = (
        #    db.session.query(Deck, func.count(GameParticipant.id).label("plays"))
        #    .join(GameParticipant, GameParticipant.deck_id == Deck.id)
        #    .filter(GameParticipant.player_id == p.id)
        #    .group_by(Deck.id)
        #    .order_by(text("plays DESC"))
        #    .first()
        #)
    
        if most_played:
            deck = most_played[0]
            row["most_played_deck"] = deck
            row["bg_art"] = deck.commander_local_art or deck.commander_art_crop_url
        else:
            row["most_played_deck"] = None
            row["bg_art"] = None

    # Deck stats
    decks = Deck.query.all()
    deck_stats = []
    for d in decks:
        wins = (
            GameParticipant.query.join(Game)
            .filter(
                GameParticipant.deck_id == d.id,
                Game.winner_id == GameParticipant.player_id,
            )
            .count()
        )
        uses = GameParticipant.query.filter_by(deck_id=d.id).count()
        winrate = round(wins / uses * 100, 1) if uses > 0 else 0
        deck_stats.append({"deck": d, "wins": wins, "uses": uses, "winrate": winrate})
    deck_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))

    # Recent games
    recent_games = Game.query.order_by(Game.date.desc()).limit(10).all()
    game_parts = {}
    for g in recent_games:
        game_parts[g.id] = GameParticipant.query.filter_by(game_id=g.id).all()

    # --- Top 3 players for pyramid ---
    top_players = player_stats[:3]
    
    # --- Best deck by winrate (prefer decks with >= min_games) ---
    min_games = 3  # change to 1 if you want "any deck"
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
        # highest winrate, then most games as tiebreak
        best_candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        best_deck = best_candidates[0][3]

    most_played = (
        db.session.query(Deck, func.count(GameParticipant.id).label("plays"))
        .join(GameParticipant, GameParticipant.deck_id == Deck.id)
        .filter(GameParticipant.player_id == p.id)
        .group_by(Deck.id)
        .order_by(text("plays DESC"))
        .first()
    )
    
    return render_template(
        "index.html",
        player_stats=player_stats,
        deck_stats=deck_stats,
        recent_games=recent_games,
        game_parts=game_parts,
        top_players=top_players,
        best_deck=best_deck,
    )

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



@app.route("/delete_player/<int:player_id>", methods=["POST"])
def delete_player(player_id):
    player = db.session.get(Player, player_id)
    if not player:
        flash("Player not found.")
        return redirect(url_for("players"))
    if player.user_id is not None:
    flash("Can't delete a user-linked player.")
    return redirect(url_for("players"))
    played = GameParticipant.query.filter_by(player_id=player_id).count()
    won = Game.query.filter_by(winner_id=player_id).count()
    if played > 0 or won > 0:
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

    db.session.delete(player)
    db.session.commit()
    flash("Player deleted.")
    return redirect(url_for("players"))


@app.route("/games")
def games():
    all_games = Game.query.order_by(Game.date.desc()).all()
    game_parts = {}
    for g in all_games:
        game_parts[g.id] = GameParticipant.query.filter_by(game_id=g.id).all()
    return render_template("games.html", games=all_games, game_parts=game_parts)


@app.route("/players")
def players():
    players_list = Player.query.all()

    player_can_delete[p.id] = (
        p.user_id is None and played == 0 and won == 0 and not deck_used
    )
    for p in players_list:
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        won = Game.query.filter_by(winner_id=p.id).count()

        # Also block if any of their decks are used
        deck_used = (
            db.session.query(GameParticipant.id)
            .join(Deck, GameParticipant.deck_id == Deck.id)
            .filter(Deck.player_id == p.id)
            .first()
            is not None
        )

        player_can_delete[p.id] = (played == 0 and won == 0 and not deck_used)

    return render_template(
        "players.html",
        players=players_list,
        player_can_delete=player_can_delete,
    )



@app.route("/add_player", methods=["POST"])
def add_player():
    name = request.form["name"].strip()
    if name and not Player.query.filter_by(name=name).first():
        db.session.add(Player(name=name))
        db.session.commit()
    return redirect(url_for("players"))


@app.route("/decks")
def decks():
    players_list = Player.query.order_by(Player.name.asc()).all()

    player_id = request.args.get("player_id", type=int)
    show_retired = request.args.get("show_retired", type=int)

    q = Deck.query

    if not show_retired:
        q = q.filter(Deck.retired == False)

    if player_id:
        q = q.filter(Deck.player_id == player_id)

    decks_list = q.order_by(Deck.retired.asc(), Deck.name.asc()).all()


    # Deck stats (wins / uses / losses / winrate)
    stats = {}
    for d in decks_list:
        wins = (
            GameParticipant.query.join(Game, GameParticipant.game_id == Game.id)
            .filter(
                GameParticipant.deck_id == d.id,
                Game.winner_id == GameParticipant.player_id,
            )
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
    )


@app.route("/deck/<int:deck_id>/resync")


@app.route("/deck/<int:deck_id>")
def deck_detail(deck_id):
    deck = db.session.get(Deck, deck_id)
    if not deck:
        return "Deck not found", 404

    # --- Overall Stats ---
    wins = (
        GameParticipant.query.join(Game)
        .filter(
            GameParticipant.deck_id == deck.id,
            Game.winner_id == GameParticipant.player_id,
        )
        .count()
    )

    games = GameParticipant.query.filter_by(deck_id=deck.id).count()
    losses = max(0, games - wins)
    winrate = round((wins / games) * 100, 1) if games else 0

    # --- History + Matchups ---
    participations = (
        GameParticipant.query
        .join(Game, GameParticipant.game_id == Game.id)
        .filter(GameParticipant.deck_id == deck.id)
        .order_by(Game.date.desc())
        .all()
    )

    history = []
    matchups = {}

    for part in participations:
        game = part.game
        won = game.winner_id == part.player_id

        opponents = (
            GameParticipant.query
            .filter(
                GameParticipant.game_id == game.id,
                GameParticipant.player_id != part.player_id
            )
            .all()
        )

        opponent_names = []

        for o in opponents:
            name = o.player.name
            opponent_names.append(name)

            if name not in matchups:
                matchups[name] = {"wins": 0, "losses": 0}

            if won:
                matchups[name]["wins"] += 1
            else:
                matchups[name]["losses"] += 1

        history.append({
            "game_id": game.id,
            "date": game.date,
            "won": won,
            "opponents": opponent_names
        })

    # Compute winrate per opponent
    for name, data in matchups.items():
        total = data["wins"] + data["losses"]
        data["games"] = total
        data["winrate"] = round((data["wins"] / total) * 100, 1) if total else 0

    # Sort by most played
    matchups = dict(sorted(matchups.items(), key=lambda x: -x[1]["games"]))

    return render_template(
        "deck_detail.html",
        deck=deck,
        wins=wins,
        losses=losses,
        games=games,
        winrate=winrate,
        history=history,
        matchups=matchups,
    )



@app.route("/add_deck", methods=["POST"])
@login_required
def add_deck():
    u = get_current_user()

    name = request.form.get("name", "").strip()
    commander_input = request.form.get("commander", "").strip()

    if not (name and commander_input):
        flash("Deck name and commander are required.")
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

    deck = Deck(name=name, commander=commander_input, player_id=player_id)

    # Enrich from Scryfall (if you have these helpers)
    card = scryfall_named_exact(commander_input)
    if card:
        scry_id = card.get("id")
        canonical_name = card.get("name") or commander_input
        art_crop = extract_art_crop(card)
        color_identity = "".join(card.get("color_identity") or [])

        local_art = None
        if art_crop and scry_id:
            local_art = download_art_crop(art_crop, scry_id, canonical_name)

        deck.commander_name = canonical_name
        deck.commander_scryfall_id = scry_id
        deck.commander_art_crop_url = art_crop
        deck.commander_local_art = local_art
        deck.color_identity = color_identity

    db.session.add(deck)
    db.session.commit()

    flash("Deck added.")
    return redirect(url_for("decks"))


@app.route("/add_game")
def add_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = Deck.query.filter_by(player_id=p.id, retired=False).order_by(Deck.name.asc()).all()
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in active_decks]
    decks_json = json.dumps(decks_by_player)
    return render_template("add_game.html", players=players, decks_json=decks_json)


@app.route("/play_game")
def play_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = Deck.query.filter_by(player_id=p.id, retired=False).order_by(Deck.name.asc()).all()
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in active_decks]
    decks_json = json.dumps(decks_by_player)
    return render_template("play_game.html", players=players, decks_json=decks_json)


@app.route("/start_game", methods=["POST"])
def start_game():
    participants = []
    seen = set()

    for i in range(1, 7):  # Up to 6 players
        p_id = request.form.get(f"player{i}")
        d_id = request.form.get(f"deck{i}")
        if p_id and d_id:
            p_id = int(p_id)
            d_id = int(d_id)

            if p_id in seen:
                return "Duplicate players not allowed", 400
            seen.add(p_id)

            deck = db.session.get(Deck, d_id)
            if not deck or deck.player_id != p_id or deck.retired:
                return "Invalid deck for player", 400

            participants.append(
                {
                    "player_id": p_id,
                    "deck_id": d_id,
                    "player_name": deck.owner.name,
                    "deck_name": deck.name,
                    # Useful for life_counter backgrounds later
                    "commander_art": deck.commander_local_art or deck.commander_art_crop_url,
                }
            )

    if len(participants) < 2:
        return "Need at least 2 players", 400

    session["game_participants"] = participants
    session.modified = True
    return redirect(url_for("life_counter"))


@app.route("/life_counter")
def life_counter():
    participants = session.get("game_participants")
    if not participants or len(participants) < 2:
        flash("No active game. Please start a new game.")
        return redirect(url_for("play_game"))

    colors = ["--blue", "--red", "--green", "--purple", "--orange", "--yellow"]

    for i, p in enumerate(participants, 1):
        p["index"] = i
        p["color"] = colors[(i - 1) % len(colors)]

        # Load deck art dynamically
        deck = db.session.get(Deck, p["deck_id"])
        if deck:
            p["commander_art"] = deck.commander_local_art or deck.commander_art_crop_url
        else:
            p["commander_art"] = None

    return render_template("life_counter.html", participants=participants)


@app.route("/end_game", methods=["POST"])
def end_game():
    winner_id = request.form.get("winner")
    if not winner_id:
        return "Must select a winner", 400

    participants = session.get("game_participants")
    if not participants:
        return "No game in session", 400

    seen = {p["player_id"] for p in participants}
    if int(winner_id) not in seen:
        return "Winner must be a participant", 400

    game = Game(winner_id=int(winner_id))
    db.session.add(game)
    db.session.flush()

    for p in participants:
        db.session.add(GameParticipant(game_id=game.id, player_id=p["player_id"], deck_id=p["deck_id"]))

    db.session.commit()
    session.pop("game_participants", None)
    return redirect(url_for("index"))


@app.route("/manual_game")
def manual_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        active_decks = Deck.query.filter_by(player_id=p.id, retired=False).order_by(Deck.name.asc()).all()
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
            if not deck or deck.player_id != p_id or deck.retired:
                return "Invalid deck for player", 400

            participants.append((p_id, d_id))

    if len(participants) < 2:
        return "Need at least 2 players", 400
    if int(winner_id) not in seen:
        return "Winner must be a participant", 400

    game = Game(winner_id=int(winner_id))
    db.session.add(game)
    db.session.flush()

    for p_id, d_id in participants:
        db.session.add(GameParticipant(game_id=game.id, player_id=p_id, deck_id=d_id))

    db.session.commit()
    return redirect(url_for("index"))


@app.route("/record_game", methods=["POST"])
def record_game():
    """
    Legacy 4-player record route (kept for compatibility with old forms).
    """
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

            participants.append((p_id, d_id))

    if len(participants) < 2:
        return "Need at least 2 players", 400
    if int(winner_id) not in seen:
        return "Winner must be a participant", 400

    game = Game(winner_id=int(winner_id))
    db.session.add(game)
    db.session.flush()

    for p_id, d_id in participants:
        db.session.add(GameParticipant(game_id=game.id, player_id=p_id, deck_id=d_id))

    db.session.commit()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
