from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json
import os
import requests
from urllib.parse import quote
from urllib.parse import quote
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY", "super-secret-default-change-me-in-production"
)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/commander.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    decks = db.relationship("Deck", backref="owner", lazy=True)


class Deck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    commander = db.Column(db.String(100), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)
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
    __table_args__ = (
        db.UniqueConstraint("game_id", "player_id", name="unique_player_per_game"),
    )


# Create DB tables
with app.app_context():
    db.create_all()

# === Login Required ===
@app.before_request
def require_login():
    if "user_id" not in session:
        if request.endpoint not in ("login", "register", "static"):
            return redirect(url_for("login") + "?next=" + quote(request.full_path))


# === Auth Routes ===
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        confirm = request.form["confirm"]
        if not username or not password:
            flash("Username and password required")
        elif password != confirm:
            flash("Passwords do not match")
        elif User.query.filter_by(username=username).first():
            flash("Username already taken")
        else:
            hashed = generate_password_hash(password)
            db.session.add(User(username=username, password_hash=hashed))
            db.session.commit()
            flash("Registration successful! Please log in.")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            session["username"] = user.username
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))
        flash("Invalid username or password")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully")
    return redirect(url_for("login"))


# === Main App Routes ===
@app.route("/")
def index():
    # Player stats
    players = Player.query.all()
    player_stats = []
    for p in players:
        wins = Game.query.filter_by(winner_id=p.id).count()
        played = GameParticipant.query.filter_by(player_id=p.id).count()
        winrate = round(wins / played * 100, 1) if played > 0 else 0
        player_stats.append(
            {"player": p, "wins": wins, "played": played, "winrate": winrate}
        )
    player_stats.sort(key=lambda x: (-x["wins"], -x["winrate"]))

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

    return render_template(
        "index.html",
        player_stats=player_stats,
        deck_stats=deck_stats,
        recent_games=recent_games,
        game_parts=game_parts,
    )


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
    return render_template("players.html", players=players_list)


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

    # Filter by owner via query param: /decks?player_id=1
    player_id = request.args.get("player_id", type=int)

    q = Deck.query
    if player_id:
        q = q.filter(Deck.player_id == player_id)

    decks_list = q.order_by(Deck.name.asc()).all()

    # --- Deck stats (wins / uses / losses / winrate) ---
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

    return render_template(
        "decks.html",
        decks=decks_list,
        players=players_list,
        selected_player_id=player_id,
        deck_stats=stats,
    )


@app.route("/add_deck", methods=["POST"])
def add_deck():
    name = request.form["name"].strip()
    commander = request.form["commander"].strip()
    commander_input = request.form.get("commander", "").strip()
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

    player_id = request.form["player_id"]
    if name and commander and player_id:
        db.session.add(Deck(name=name, commander=commander, player_id=player_id))
        db.session.commit()
    return redirect(url_for("decks"))


@app.route("/add_game")
def add_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in p.decks]
    decks_json = json.dumps(decks_by_player)
    return render_template("add_game.html", players=players, decks_json=decks_json)


@app.route("/play_game")
def play_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in p.decks]
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
            if not deck or deck.player_id != p_id:
                return "Invalid deck for player", 400
            participants.append(
                {
                    "player_id": p_id,
                    "deck_id": d_id,
                    "player_name": deck.owner.name,
                    "deck_name": deck.name,
                }
            )

    if len(participants) < 2:
        return "Need at least 2 players", 400

    # Store in session for life counter
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
        db.session.add(
            GameParticipant(
                game_id=game.id, player_id=p["player_id"], deck_id=p["deck_id"]
            )
        )
    db.session.commit()

    session.pop("game_participants", None)
    return redirect(url_for("index"))


@app.route("/manual_game")
def manual_game():
    players = Player.query.all()
    decks_by_player = {}
    for p in players:
        decks_by_player[str(p.id)] = [{"id": d.id, "name": d.name} for d in p.decks]
    decks_json = json.dumps(decks_by_player)
    return render_template("manual_game.html", players=players, decks_json=decks_json)


# Update old record_game to manual_record_game or keep as is, but point form to /manual_record_game
@app.route("/manual_record_game", methods=["POST"])
def manual_record_game():
    # Same as original record_game
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
            deck = Deck.query.get(d_id)
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


@app.route("/record_game", methods=["POST"])
def record_game():
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
            deck = Deck.query.get(d_id)
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
