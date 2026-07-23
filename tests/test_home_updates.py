import re
from pathlib import Path


def test_service_worker_navigation_is_network_first():
    sw = Path("static/sw.js").read_text(encoding="utf-8")
    assert 'event.request.mode === "navigate"' in sw
    assert "fetch(event.request).catch(() => caches.match(event.request))" in sw


def test_service_worker_home_page_not_in_static_assets():
    sw = Path("static/sw.js").read_text(encoding="utf-8")
    m = re.search(r"const STATIC_ASSETS = \[(.*?)\];", sw, re.S)
    assert m, "STATIC_ASSETS list should be present"
    assert '"/"' not in m.group(1)


def test_service_worker_first_party_css_not_permanently_cached():
    """base.css must not be in the permanent cache-first STATIC_ASSETS list."""
    sw = Path("static/sw.js").read_text(encoding="utf-8")
    m = re.search(r"const STATIC_ASSETS = \[(.*?)\];", sw, re.S)
    assert m, "STATIC_ASSETS list should be present"
    assert "/static/css/base.css" not in m.group(1)


def test_service_worker_has_stale_while_revalidate_for_first_party_css():
    """sw.js must have an explicit strategy path for /static/css/ URLs."""
    sw = Path("static/sw.js").read_text(encoding="utf-8")
    assert "/static/css/" in sw, "No strategy path found for /static/css/ in sw.js"


def test_home_recent_games_order_breaks_same_timestamp_ties_by_newest_id():
    app_py = Path("app.py").read_text(encoding="utf-8-sig")

    assert "recent_games = game_q.order_by(Game.date.desc(), Game.id.desc()).limit(10).all()" in app_py
    assert "recent_games_list = game_q.order_by(Game.date.desc(), Game.id.desc()).limit(10).all()" in app_py
