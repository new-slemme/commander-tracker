import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


def deck(text="1 Test Card", **kwargs):
    return SimpleNamespace(decklist_text=text, tags_json="{}", tags_version=None,
                           tags_computed_at=None, **kwargs)


def mechanic_tags(**values):
    return {key: bool(values.get(key)) for key in app.KNOWN_DECK_TAG_KEYS}


class DeckTaggingTests(unittest.TestCase):
    def test_total_lookup_failure_remains_incomplete(self):
        with patch("app.scryfall_collection", return_value={}), patch("app.custommtg_gallery_named_exact", return_value=None):
            tags, diagnostic = app.compute_deck_tags(["Test Card"])
        d = deck()
        app.apply_deck_tags(d, tags, diagnostic)
        self.assertTrue(app.is_deck_tags_stale(d))
        self.assertEqual(app.get_deck_tag_summary(d)["status"], "partial")
        self.assertEqual(app.get_deck_tag_summary(d)["unresolved_cards"], ["Test Card"])

    def test_partial_refresh_preserves_positive_mechanics_only_for_same_list(self):
        d = deck()
        app.apply_deck_tags(d, mechanic_tags(monarch=True))
        app.apply_deck_tags(d, mechanic_tags(), {"unresolved_count": 1}, preserve_existing=True)
        self.assertTrue(app.get_deck_parsed_tags(d)["monarch"])
        d.decklist_text = "1 Replacement Card"
        app.apply_deck_tags(d, mechanic_tags(), {"unresolved_count": 1}, preserve_existing=True)
        self.assertFalse(app.get_deck_parsed_tags(d)["monarch"])

    def test_complete_refresh_removes_obsolete_positive_and_invalidates_cache(self):
        d = deck()
        app.apply_deck_tags(d, mechanic_tags(monarch=True))
        self.assertTrue(app.get_deck_parsed_tags(d)["monarch"])
        app.apply_deck_tags(d, mechanic_tags())
        self.assertFalse(app.get_deck_parsed_tags(d)["monarch"])
        self.assertFalse(app.is_deck_tags_stale(d))

    def test_changed_list_invalidates_analysis(self):
        d = deck()
        app.apply_deck_tags(d, mechanic_tags(), {"strategies": {"tokens": {"cards": ["Test Card"]}}})
        d.decklist_text = "1 Other Card"
        self.assertTrue(app.is_deck_tags_stale(d))
        self.assertEqual(app.get_deck_tag_summary(d)["strategies"], [])

    def test_empty_or_invalid_tags_cannot_be_current(self):
        for raw in ("{}", "invalid", "[]", '{"_analysis": []}'):
            d = deck(); d.tags_version = app.DECK_TAGS_VERSION; d.tags_json = raw
            self.assertTrue(app.is_deck_tags_stale(d))

    def test_no_list_and_short_list_have_distinct_states(self):
        self.assertEqual(app.get_deck_tag_summary(deck(""))["status"], "no_list")
        d = deck(); app.apply_deck_tags(d, mechanic_tags())
        self.assertEqual(app.get_deck_tag_summary(d)["status"], "limited_list")
        self.assertEqual(app.get_deck_tag_summary(d)["card_count"], 1)

    def test_sideboard_cards_do_not_enable_mechanics(self):
        parsed = app.parse_plaintext_decklist("Commander:\n1 Leader\nMainboard:\n1 Island\nSideboard:\n1 Court of Ire\nMaybeboard:\n1 Guide of Souls").as_dict()
        self.assertEqual(app.extract_decklist_card_names(parsed), ["Leader", "Island"])

    def test_keywords_without_reminder_text_and_back_face_are_detected(self):
        flags = app.analyze_scryfall_card({"keywords": ["Infect", "Ascend"], "card_faces": [{"oracle_text": ""}, {"oracle_text": "You get {E}."}]})
        self.assertTrue(flags["poison"])
        self.assertTrue(flags["citys_blessing"])
        self.assertTrue(flags["energy"])

    def test_batch_resolves_full_dfc_name_and_front_face_alias(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"name": "Front // Back", "card_faces": [{"name": "Front"}, {"name": "Back"}]}]}
        with patch("app.requests.post", return_value=response) as post:
            results = app.scryfall_collection(["Front // Back", "Front", "Back"])
        self.assertEqual(set(results), {"front // back", "front", "back"})
        self.assertEqual(post.call_args.kwargs["json"]["identifiers"][0], {"name": "Front"})

    def test_strategy_requires_multiple_distinct_cards(self):
        card = {"name": "Token Maker", "oracle_text": "Create a 1/1 creature token."}
        self.assertNotIn("tokens", app.infer_deck_strategies([card] * 20))
        cards = [{**card, "name": f"Maker {i}"} for i in range(5)]
        result = app.infer_deck_strategies(cards)
        self.assertEqual(len(result["tokens"]["cards"]), 5)

    def test_tribal_needs_a_large_shared_creature_type(self):
        cards = [{"name": f"Goblin {i}", "type_line": "Creature — Goblin Warrior"} for i in range(10)]
        self.assertIn("tribal", app.infer_deck_strategies(cards))
        self.assertNotIn("tribal", app.infer_deck_strategies(cards[:3]))

    def test_mana_rocks_alone_do_not_imply_artifact_strategy(self):
        cards = [{"name": f"Rock {i}", "type_line": "Artifact"} for i in range(5)]
        self.assertNotIn("artifacts", app.infer_deck_strategies(cards))

    def test_manual_overrides_survive_refresh_and_are_separate_from_mechanics(self):
        d = deck()
        d.tags_json = json.dumps({"_strategy_overrides": {"tokens": False, "mill": True}})
        app.apply_deck_tags(d, mechanic_tags(), {"strategies": {"tokens": {"cards": ["Test Card"]}}})
        rows = app.get_deck_tag_summary(d)["strategies"]
        self.assertEqual([(row["key"], row["source"]) for row in rows], [("mill", "manual")])
        self.assertNotIn("mill", app.get_deck_parsed_tags(d))


class StrategyTagRouteTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with app.app.app_context():
            app.db.session.remove(); app.db.drop_all(); app.db.create_all()
            user = app.User(username="tag-owner", display_name="Tag Owner", password_hash="unused", is_active=True, is_admin=True)
            app.db.session.add(user); app.db.session.flush()
            player = app.Player(name="Tag Owner", user_id=user.id)
            app.db.session.add(player); app.db.session.flush()
            d = app.Deck(name="Tag Deck", commander="Test Commander", player_id=player.id, decklist_text="1 Island")
            app.db.session.add(d); app.db.session.commit()
            self.user_id, self.deck_id = user.id, d.id
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["_csrf_token"] = "test-tag-csrf"

    def test_owner_can_correct_and_reset_tags(self):
        url = f"/deck/{self.deck_id}/strategy-tags"
        response = self.client.post(url, data={"tokens": "yes", "mill": "no", "_csrf_token": "test-tag-csrf"})
        self.assertEqual(response.status_code, 302)
        with app.app.app_context():
            d = app.db.session.get(app.Deck, self.deck_id)
            self.assertEqual(d.tag_summary["overrides"], {"tokens": True, "mill": False})
        self.assertEqual(self.client.post(url, data={"_csrf_token": "test-tag-csrf"}).status_code, 302)
        with app.app.app_context():
            self.assertEqual(app.db.session.get(app.Deck, self.deck_id).tag_summary["overrides"], {})

    def test_invalid_override_rejected(self):
        response = self.client.post(f"/deck/{self.deck_id}/strategy-tags", data={"tokens": "sometimes", "_csrf_token": "test-tag-csrf"})
        self.assertEqual(response.status_code, 400)

    def test_anonymous_cannot_write(self):
        response = app.app.test_client().post(f"/deck/{self.deck_id}/strategy-tags", data={"tokens": "yes"})
        self.assertIn(response.status_code, (302, 400, 403))

    def test_other_user_cannot_override_strategy(self):
        with app.app.app_context():
            other = app.User(username="tag-other", display_name="Other", password_hash="unused", is_active=True)
            app.db.session.add(other); app.db.session.commit()
            other_id = other.id
        with self.client.session_transaction() as session:
            session["user_id"] = other_id
            session["is_admin"] = False
        response = self.client.post(f"/deck/{self.deck_id}/strategy-tags", data={"tokens": "yes", "_csrf_token": "test-tag-csrf"})
        self.assertIn(response.status_code, (403, 404))
        with app.app.app_context():
            self.assertEqual(app.db.session.get(app.Deck, self.deck_id).tag_summary["overrides"], {})
