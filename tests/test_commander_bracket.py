import unittest
from unittest.mock import Mock, patch

import requests
import app
import commander_brackets as brackets


def card(name, **flags):
    return {'card': {'name': name}, 'quantity': 1, 'banned': False,
            'gameChanger': name.casefold() in brackets.GAME_CHANGERS,
            'massLandDenial': False, 'extraTurn': False, **flags}


def combo(names, **flags):
    return {'combo': {'id': 'test-combo', 'uses': [{'card': {'name': n}, 'quantity': 1} for n in names], 'requires': []},
            'relevant': True, 'definitelyTwoCard': True, 'speed': 4,
            'massLandDenial': False, 'extraTurn': False, **flags}


class CommanderBracketTests(unittest.TestCase):
    def result(self, names, *, combos=(), overrides=None, commanders=(), declared=None):
        data = {'cards': [card(n, **(overrides or {}).get(n, {})) for n in set(names + list(commanders))], 'combos': list(combos)}
        with patch.object(brackets, '_classifications', return_value=data):
            return brackets.estimate_bracket(names, commanders=commanders, declared_bracket=declared)

    def test_game_changer_boundaries(self):
        names = ['Rhystic Study', 'Smothering Tithe', 'Cyclonic Rift', 'The One Ring']
        for count, expected in [(0, 2), (1, 3), (3, 3), (4, 4)]:
            with self.subTest(count=count):
                r = self.result(['Island'] + names[:count])
                self.assertEqual(r['bracket'], expected)
                self.assertEqual(r['signals']['game_changer_count'], count)

    def test_current_snapshot_and_removed_cards(self):
        self.assertEqual(len(brackets.GAME_CHANGERS), 53)
        with patch.object(brackets, '_classifications', side_effect=ValueError):
            for name in ['Farewell', 'Biorhythm', 'Demonic Tutor']:
                self.assertEqual(brackets.estimate_bracket([name])['bracket'], 3)
            for name in ['Food Chain', 'Deflecting Swat', 'Mox Opal']:
                self.assertEqual(brackets.estimate_bracket([name])['bracket'], 2)

    def test_empty_deck_unknown_and_no_network(self):
        with patch.object(brackets, '_classifications') as lookup:
            r = brackets.estimate_bracket([], declared_bracket=5)
        lookup.assert_not_called()
        self.assertIsNone(r['bracket'])
        self.assertEqual(r['declared_bracket'], 5)

    def test_unrelated_combo_pieces_do_not_make_cedh(self):
        r = self.result(["Thassa's Oracle", 'Protean Hulk', 'Chrome Mox'])
        self.assertEqual(r['bracket'], 3)
        self.assertEqual(r['combos'], [])

    def test_actual_combo_and_missing_piece(self):
        names = ["Thassa's Oracle", 'Demonic Consultation']
        self.assertEqual(self.result(names, combos=[combo(names)])['bracket'], 4)
        r = self.result(names[:1], combos=[combo(names)])
        self.assertEqual(r['bracket'], 3)
        self.assertFalse(r['combos'])

    def test_late_combo_and_prerequisites(self):
        names = ['A', 'B']
        self.assertEqual(self.result(names, combos=[combo(names, speed=2)])['bracket'], 3)
        self.assertEqual(self.result(names, combos=[combo(names, definitelyTwoCard=False)])['bracket'], 2)
        c = combo(names)
        c['combo']['uses'][0]['mustBeCommander'] = True
        self.assertEqual(self.result(names, combos=[c])['bracket'], 2)
        self.assertEqual(self.result(['B'], commanders=['A'], combos=[c])['bracket'], 4)
        c['combo']['uses'][1]['quantity'] = 2
        self.assertEqual(self.result(['B'], commanders=['A'], combos=[c])['bracket'], 2)

    def test_land_denial_and_extra_turn_chain(self):
        self.assertEqual(self.result(['Armageddon'], overrides={'Armageddon': {'massLandDenial': True}})['bracket'], 4)
        names = ['Time Warp', 'Time Stretch']
        flags = {n: {'extraTurn': True} for n in names}
        r = self.result(names, overrides=flags)
        self.assertEqual(r['bracket'], 2)
        self.assertTrue(any('Chaining' in w for w in r['warnings']))
        self.assertEqual(self.result(names, combos=[combo(names, extraTurn=True)])['bracket'], 4)

    def test_banned_and_unresolved_cards(self):
        r = self.result(['Mana Crypt'], overrides={'Mana Crypt': {'banned': True}})
        self.assertIsNone(r['bracket'])
        self.assertEqual(r['status'], 'invalid')
        with patch.object(brackets, '_classifications', return_value={'cards': [], 'combos': []}):
            r = brackets.estimate_bracket(['Custom card'])
        self.assertEqual(r['status'], 'partial')
        self.assertEqual(r['unresolved_cards'], ['custom card'])

    def test_outage_is_explicit_and_declared_does_not_override_estimate(self):
        with patch.object(brackets, '_classifications', side_effect=ValueError):
            r = brackets.estimate_bracket(['Demonic Tutor'], declared_bracket=1)
        self.assertEqual((r['bracket'], r['declared_bracket'], r['status']), (3, 1, 'partial'))
        self.assertTrue(any('below' in w for w in r['warnings']))
        self.assertTrue(any('unchecked' in w for w in r['warnings']))

    def test_live_card_removal_supersedes_snapshot(self):
        r = self.result(['Demonic Tutor'], overrides={'Demonic Tutor': {'gameChanger': False}})
        self.assertEqual(r['bracket'], 2)

    def test_text_zones_and_commander_metadata(self):
        with patch.object(brackets, '_classifications', side_effect=ValueError) as lookup:
            r = app.compute_commander_bracket_from_text('Commander\n1 Rhystic Study\nMainboard\n1 Island\nSideboard\n1 The One Ring\nMaybeboard\n1 Cyclonic Rift')
            self.assertEqual(r['signals']['game_changer_count'], 1)
            self.assertEqual(dict(lookup.call_args.args[1]), {'rhystic study': 1})
            r = app.compute_commander_bracket_from_text('1 Island', commanders=['Demonic Tutor'])
            self.assertEqual(r['bracket'], 3)

    def test_endpoint_validation(self):
        client = app.app.test_client()
        for payload in [[], {'cards': [None]}, {'cards': 'x'}, {'cards': ['x'] * 601}, {'cards': [], 'declared_bracket': True}, {'cards': [], 'declared_bracket': 3.5}, {'decklist_text': '1 Island', 'commanders': 'x'}]:
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual(client.post('/api/commander-bracket', json=payload).status_code, 400)


class SpellbookTransportTests(unittest.TestCase):
    def setUp(self):
        brackets._cache.clear()
        brackets._failure_until = 0

    def tearDown(self):
        self.setUp()

    def test_cache_key_includes_command_zone_and_results_are_copied(self):
        response = Mock()
        response.json.return_value = {'cards': [card('Island')], 'combos': []}
        with patch.object(brackets.requests, 'post', return_value=response) as post:
            r = brackets._classifications({'island': 1}, {})
            r['cards'].clear()
            self.assertTrue(brackets._classifications({'island': 1}, {})['cards'])
            self.assertEqual(post.call_count, 1)
            brackets._classifications({}, {'island': 1})
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs['timeout'], (2, 6))

    def test_failure_backoff_and_malformed_response(self):
        with patch.object(brackets.requests, 'post', side_effect=requests.Timeout) as post:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    brackets._classifications({'island': 1}, {})
            self.assertEqual(post.call_count, 1)
        brackets._failure_until = 0
        response = Mock()
        response.json.return_value = {'cards': [{'card': {'name': 'Island'}}], 'combos': []}
        with patch.object(brackets.requests, 'post', return_value=response):
            r = brackets.estimate_bracket(['Island'])
        self.assertEqual(r['status'], 'partial')
