import re
from pathlib import Path


def test_service_worker_does_not_cache_home_page_shell():
    sw = Path("static/sw.js").read_text(encoding="utf-8")

    static_assets_match = re.search(r"const STATIC_ASSETS = \[(.*?)\];", sw, re.S)
    assert static_assets_match, "STATIC_ASSETS list should be present"
    static_assets = static_assets_match.group(1)

    assert '"/"' not in static_assets
    assert "event.request.mode === \"navigate\"" in sw
    assert "fetch(event.request).catch(() => caches.match(event.request))" in sw


def test_home_recent_games_order_breaks_same_timestamp_ties_by_newest_id():
    app_py = Path("app.py").read_text(encoding="utf-8-sig")

    assert "recent_games = game_q.order_by(Game.date.desc(), Game.id.desc()).limit(10).all()" in app_py
    assert "recent_games_list = game_q.order_by(Game.date.desc(), Game.id.desc()).limit(10).all()" in app_py
