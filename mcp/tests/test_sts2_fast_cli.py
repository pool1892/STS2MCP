from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sts2_fast_cli import (
    JsonlLogger,
    RunStats,
    action_body_from_plan,
    analyze_log,
    analyze_logs,
    build_parser,
    decision_point,
    drain_trivial,
    execute_actions,
    expand_log_path_args,
    normalize_action,
    next_trivial_action,
    state_delta,
    validate_post_response,
)


class FakeClient:
    def __init__(self, state):
        self._state = state
        self.posts = []
        self.state_calls = 0

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        action = body["action"]

        if action == "claim_reward":
            rewards = self._state["rewards"]
            rewards["items"] = [
                item for item in rewards["items"] if item["index"] != body["index"]
            ]
            for index, item in enumerate(rewards["items"]):
                item["index"] = index
            return {}

        if action == "claim_treasure_relic":
            self._state["treasure"] = {"can_proceed": True}
            return {}

        if action == "proceed":
            self._state = {
                "state_type": "map",
                "map": {"next_options": [{"index": 0, "type": "Monster"}]},
                "player": {"hp": 80, "max_hp": 80, "potions": []},
            }
            return {}

        if action == "choose_map_node":
            self._state = {
                "state_type": "monster",
                "player": {
                    "hp": 80,
                    "max_hp": 80,
                    "potions": [],
                    "energy": 3,
                    "hand": [{"index": 0, "name": "Strike"}],
                },
                "battle": {
                    "round": 1,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [{"entity_id": "CULTIST_0", "hp": 48}],
                },
            }
            return {}

        if action == "play_card":
            hand = self._state["player"]["hand"]
            hand[:] = [card for card in hand if card["index"] != body["card_index"]]
            for index, card in enumerate(hand):
                card["index"] = index
            return {}

        return {}


class FakeDelayedPlayClient:
    def __init__(self):
        battle = {
            "enemies": [{"entity_id": "JAW_WORM_0", "hp": 40}],
            "turn": "player",
            "is_play_phase": True,
        }
        self._state = {
            "state_type": "monster",
            "player": {
                "hand": [
                    {"index": 0, "name": "Strike", "description": "Deal 6 damage."},
                    {"index": 1, "name": "Strike", "description": "Deal 6 damage."},
                ]
            },
            "battle": battle,
        }
        self.posts = []
        self.state_calls = 0
        self._stale_state = None
        self.stale_reads_remaining = 0

    def state(self):
        self.state_calls += 1
        if self.stale_reads_remaining > 0:
            self.stale_reads_remaining -= 1
            return self._stale_state
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "play_card":
            self._stale_state = {
                "state_type": self._state["state_type"],
                "player": {"hand": [dict(card) for card in self._state["player"]["hand"]]},
                "battle": self._state["battle"],
            }
            hand = self._state["player"]["hand"]
            hand[:] = [card for card in hand if card["index"] != body["card_index"]]
            for index, card in enumerate(hand):
                card["index"] = index
            self.stale_reads_remaining = 1
        return {}


class FakeEffectDelayedPlayClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._after_post = False
        self._effect_reads = 0

    def _state(self, *, hand, enemy_hp):
        return {
            "state_type": "monster",
            "player": {"hand": hand},
            "battle": {
                "enemies": [{"entity_id": "JAW_WORM_0", "name": "Jaw Worm", "hp": enemy_hp}],
                "turn": "player",
                "is_play_phase": True,
            },
        }

    def state(self):
        self.state_calls += 1
        if not self._after_post:
            return self._state(
                hand=[{"index": 0, "name": "Strike", "description": "Deal 6 damage."}],
                enemy_hp=40,
            )
        self._effect_reads += 1
        return self._state(hand=[], enemy_hp=40 if self._effect_reads == 1 else 34)

    def post(self, body):
        self.posts.append(body)
        self._after_post = True
        self._effect_reads = 0
        return {}


class FakeStaleProceedClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "rewards",
            "player": {"potions": []},
            "rewards": {"items": [], "can_proceed": True},
        }
        self._stale_after_proceed = 0

    def state(self):
        self.state_calls += 1
        if self._stale_after_proceed > 0:
            self._stale_after_proceed -= 1
            return {
                "state_type": "rewards",
                "player": {"potions": []},
                "rewards": {"items": [], "can_proceed": True},
            }
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "proceed":
            self._stale_after_proceed = 1
            self._state = {
                "state_type": "map",
                "player": {"potions": []},
                "map": {"next_options": [{"index": 0, "type": "Monster"}]},
            }
        if body["action"] == "choose_map_node":
            self._state = {
                "state_type": "monster",
                "player": {
                    "potions": [],
                    "energy": 3,
                    "hand": [{"index": 0, "name": "Strike"}],
                },
                "battle": {
                    "round": 1,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [{"entity_id": "CULTIST_0", "hp": 48}],
                },
            }
        return {}


class FakeFrozenProceedClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "rewards",
            "player": {"potions": []},
            "rewards": {"items": [], "can_proceed": True},
        }

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        return {}


class FakeStaleMapChoiceClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "map",
            "player": {"potions": []},
            "map": {"next_options": [{"index": 0, "type": "Ancient"}]},
        }
        self._stale_after_map = 0

    def state(self):
        self.state_calls += 1
        if self._stale_after_map > 0:
            self._stale_after_map -= 1
            return {
                "state_type": "map",
                "player": {"potions": []},
                "map": {"next_options": []},
            }
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "choose_map_node":
            self._stale_after_map = 1
            self._state = {
                "state_type": "event",
                "player": {"potions": []},
                "event": {"event_name": "Pael", "options": [{"index": 0, "title": "A"}]},
            }
        return {}


class FakeMapChoiceEarlyCombatClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "map",
            "player": {"potions": []},
            "map": {"next_options": [{"index": 0, "type": "Monster"}]},
        }
        self._states = []

    def _combat_state(self, *, hand, energy):
        return {
            "state_type": "monster",
            "player": {"hp": 50, "block": 0, "energy": energy, "hand": hand},
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "BOWLBUG_0", "name": "Bowlbug", "hp": 20}],
            },
        }

    def state(self):
        self.state_calls += 1
        if self._states:
            return self._states.pop(0)
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "choose_map_node":
            self._states = [
                self._combat_state(hand=[], energy=0),
                self._combat_state(
                    hand=[{"index": 0, "name": "Strike", "description": "Deal 6 damage."}],
                    energy=3,
                ),
            ]
        return {}


class FakeMapChoiceEarlyEventClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "map",
            "player": {"potions": []},
            "map": {"next_options": [{"index": 0, "type": "Unknown"}]},
        }
        self._states = []

    def state(self):
        self.state_calls += 1
        if self._states:
            return self._states.pop(0)
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "choose_map_node":
            self._states = [
                {
                    "state_type": "event",
                    "player": {"potions": []},
                    "event": {
                        "event_name": "Brain Leech",
                        "in_dialogue": False,
                        "options": [],
                    },
                },
                {
                    "state_type": "event",
                    "player": {"potions": []},
                    "event": {
                        "event_name": "Brain Leech",
                        "in_dialogue": False,
                        "options": [{"index": 0, "title": "Share Knowledge"}],
                    },
                },
            ]
        return {}


class FakeStaleEndTurnClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._states = []
        self._current = self._combat_state(
            round_number=1,
            turn="player",
            hand=[{"index": 0, "name": "Strike", "description": "Deal 6 damage."}],
        )

    def _combat_state(self, *, round_number, turn, hand, energy=0):
        return {
            "state_type": "monster",
            "player": {"hp": 50, "block": 0, "energy": energy, "hand": hand},
            "battle": {
                "round": round_number,
                "turn": turn,
                "is_play_phase": turn == "player",
                "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
            },
        }

    def state(self):
        self.state_calls += 1
        if self._states:
            self._current = self._states.pop(0)
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "end_turn":
            self._states = [
                self._combat_state(round_number=1, turn="player", hand=[]),
                self._combat_state(round_number=1, turn="enemy", hand=[]),
                self._combat_state(
                    round_number=2,
                    turn="player",
                    hand=[{"index": 0, "name": "Strike", "description": "Deal 6 damage."}],
                ),
            ]
        return {}


class FakeEndTurnRewardsAfterEarlyReadyClient(FakeStaleEndTurnClient):
    def post(self, body):
        self.posts.append(body)
        if body["action"] == "end_turn":
            reward_state = {
                "state_type": "rewards",
                "player": {"hp": 50, "block": 0, "energy": None, "hand": []},
                "rewards": {"items": [{"index": 0, "type": "gold", "description": "15 Gold"}]},
            }
            self._states = [
                self._combat_state(round_number=1, turn="player", hand=[]),
                self._combat_state(round_number=1, turn="enemy", hand=[]),
                self._combat_state(round_number=2, turn="player", hand=[], energy=1),
                reward_state,
                reward_state,
            ]
        return {}


class FakeDelayedCardSelectClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._stale_reads = 0
        self._state = self._card_select(can_confirm=False)

    def _card_select(self, *, can_confirm):
        return {
            "state_type": "card_select",
            "player": {"potions": []},
            "card_select": {
                "prompt": "Choose a Card.",
                "can_confirm": can_confirm,
                "cards": [
                    {"index": 0, "name": "Strike"},
                    {"index": 1, "name": "Brand"},
                ],
            },
        }

    def state(self):
        self.state_calls += 1
        if self._stale_reads > 0:
            self._stale_reads -= 1
            return self._card_select(can_confirm=False)
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "select_card":
            self._stale_reads = 1
            self._state = self._card_select(can_confirm=True)
        return {}


def quiet_logger():
    return JsonlLogger(None)


class FastCliTests(unittest.TestCase):
    def test_next_trivial_action_claims_gold_before_card_reward(self):
        state = {
            "state_type": "rewards",
            "player": {"potions": [], "max_potion_slots": 2},
            "rewards": {
                "items": [
                    {"index": 0, "type": "gold", "description": "12 Gold"},
                    {"index": 1, "type": "card", "description": "Add a card"},
                ],
                "can_proceed": True,
            },
        }

        body, reason = next_trivial_action(state)

        self.assertEqual(body, {"action": "claim_reward", "index": 0})
        self.assertEqual(reason, "claim gold")

    def test_next_trivial_action_chooses_only_map_node(self):
        state = {
            "state_type": "map",
            "player": {"potions": []},
            "map": {"next_options": [{"index": 3, "type": "Monster"}]},
        }

        body, reason = next_trivial_action(state)

        self.assertEqual(body, {"action": "choose_map_node", "index": 3})
        self.assertEqual(reason, "choose only map node")

    def test_next_trivial_action_keeps_multiple_map_nodes_as_decision(self):
        state = {
            "state_type": "map",
            "player": {"potions": []},
            "map": {"next_options": [{"index": 0}, {"index": 1}]},
        }

        body, reason = next_trivial_action(state)

        self.assertIsNone(body)
        self.assertIsNone(reason)

    def test_drain_stops_on_card_reward_after_claiming_gold(self):
        fake = FakeClient(
            {
                "state_type": "rewards",
                "player": {"potions": [], "max_potion_slots": 2},
                "rewards": {
                    "items": [
                        {"index": 0, "type": "gold", "description": "12 Gold"},
                        {"index": 1, "type": "card", "description": "Add a card"},
                    ],
                    "can_proceed": True,
                },
            }
        )
        stats = RunStats()

        state, drained = drain_trivial(
            fake,
            logger=quiet_logger(),
            stats=stats,
            poll_delay=0,
        )

        self.assertEqual(
            drained,
            [{"reason": "claim gold", "body": {"action": "claim_reward", "index": 0}}],
        )
        self.assertEqual(state["state_type"], "rewards")
        self.assertEqual(
            state["rewards"]["items"],
            [{"index": 0, "type": "card", "description": "Add a card"}],
        )

    def test_drain_claims_single_treasure_relic_and_proceeds(self):
        fake = FakeClient(
            {
                "state_type": "treasure",
                "player": {"potions": []},
                "treasure": {
                    "relics": [{"index": 0, "name": "Lantern"}],
                    "can_proceed": True,
                },
            }
        )
        stats = RunStats()

        state, drained = drain_trivial(
            fake,
            logger=quiet_logger(),
            stats=stats,
            poll_delay=0,
        )

        self.assertEqual(
            [item["body"]["action"] for item in drained],
            ["claim_treasure_relic", "proceed", "choose_map_node"],
        )
        self.assertEqual(state["state_type"], "monster")

    def test_drain_waits_through_stale_proceed_state(self):
        fake = FakeStaleProceedClient()
        stats = RunStats()

        state, drained = drain_trivial(
            fake,
            logger=quiet_logger(),
            stats=stats,
            poll_delay=0,
        )

        self.assertEqual([item["body"]["action"] for item in drained], ["proceed", "choose_map_node"])
        self.assertEqual(state["state_type"], "monster")

    def test_drain_chooses_only_map_node_and_waits_until_combat_ready(self):
        fake = FakeMapChoiceEarlyCombatClient()
        stats = RunStats()

        state, drained = drain_trivial(
            fake,
            logger=quiet_logger(),
            stats=stats,
            poll_delay=0,
        )

        self.assertEqual(
            drained,
            [{"reason": "choose only map node", "body": {"action": "choose_map_node", "index": 0}}],
        )
        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(state["player"]["energy"], 3)
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Strike"])

    def test_action_wait_timeout_raises_instead_of_returning_stale_state(self):
        fake = FakeFrozenProceedClient()
        stats = RunStats()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeout.jsonl"
            with self.assertRaisesRegex(RuntimeError, "Timed out waiting for action state change"):
                execute_actions(
                    fake,
                    [{"action": "proceed"}],
                    logger=JsonlLogger(path),
                    stats=stats,
                    auto_target=True,
                    drain_after=False,
                    wait_after_end_turn=True,
                    max_polls=2,
                    poll_delay=0,
                )
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        wait = next(event for event in events if event["kind"] == "wait")
        self.assertEqual(wait["reason"], "action_state_unchanged")

    def test_play_card_by_name_resolves_shifted_indices_between_cards(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hand": [
                        {"index": 0, "name": "Strike", "description": "Deal 6 damage."},
                        {"index": 1, "name": "Defend", "description": "Gain 5 Block."},
                        {"index": 2, "name": "Strike", "description": "Deal 6 damage."},
                    ]
                },
                "battle": {
                    "enemies": [{"entity_id": "JAW_WORM_0", "hp": 40}],
                    "turn": "player",
                    "is_play_phase": True,
                },
            }
        )
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [
                {"action": "play_card", "card": "Strike"},
                {"action": "play_card", "card": "Strike"},
            ],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=1,
            poll_delay=0,
        )

        self.assertEqual([item["body"]["card_index"] for item in executed], [0, 1])
        self.assertTrue(all(item["body"]["target"] == "JAW_WORM_0" for item in executed))
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Defend"])
        self.assertEqual(fake.state_calls, 3)

    def test_map_choice_waits_through_stale_transition_state(self):
        fake = FakeStaleMapChoiceClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"map": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "event")

    def test_map_choice_waits_until_combat_hand_and_energy_are_ready(self):
        fake = FakeMapChoiceEarlyCombatClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"map": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(state["player"]["energy"], 3)
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Strike"])

    def test_map_choice_waits_until_event_options_are_ready(self):
        fake = FakeMapChoiceEarlyEventClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"map": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "event")
        self.assertEqual(
            state["event"]["options"],
            [{"index": 0, "title": "Share Knowledge"}],
        )

    def test_end_turn_waits_for_actual_next_ready_state(self):
        fake = FakeStaleEndTurnClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"end_turn": True}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=5,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "end_turn"})
        self.assertEqual(state["battle"]["round"], 2)
        self.assertEqual(state["battle"]["turn"], "player")

    def test_end_turn_does_not_stop_on_early_new_turn_before_rewards(self):
        fake = FakeEndTurnRewardsAfterEarlyReadyClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"end_turn": True}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=6,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "end_turn"})
        self.assertEqual(state["state_type"], "rewards")

    def test_play_card_waits_for_delayed_state_application(self):
        fake = FakeDelayedPlayClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [
                {"action": "play_card", "card": "Strike"},
                {"action": "play_card", "card": "Strike"},
            ],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual([item["body"]["card_index"] for item in executed], [0, 0])
        self.assertEqual(state["player"]["hand"], [])

    def test_play_card_waits_for_effect_after_hand_changes(self):
        fake = FakeEffectDelayedPlayClient()
        stats = RunStats()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "effect-delay.jsonl"
            state, executed = execute_actions(
                fake,
                [{"action": "play_card", "card": "Strike"}],
                logger=JsonlLogger(path),
                stats=stats,
                auto_target=True,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=3,
                poll_delay=0,
            )
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(executed[0]["body"]["card_index"], 0)
        self.assertEqual(state["battle"]["enemies"][0]["hp"], 34)
        wait = next(event for event in events if event["kind"] == "wait")
        self.assertEqual(wait["reason"], "card_play_settled")

    def test_documented_play_shorthand_preserves_target_field(self):
        self.assertEqual(
            normalize_action({"play": "Uppercut+", "target": "first"}),
            {"action": "play_card", "card": "Uppercut+", "target": "first"},
        )

    def test_single_key_decision_shorthands_normalize_to_index_actions(self):
        self.assertEqual(normalize_action({"reward": 0}), {"action": "reward", "index": 0})
        self.assertEqual(normalize_action({"map": 2}), {"action": "map", "index": 2})
        self.assertEqual(normalize_action({"pick_card": 1}), {"action": "pick_card", "index": 1})
        self.assertEqual(
            normalize_action({"deck_select_card": 4}),
            {"action": "deck_select_card", "index": 4},
        )

    def test_cards_parser_accepts_wait_budget_flags(self):
        args = build_parser().parse_args(["cards", "Strike", "--max-polls", "80"])

        self.assertEqual(args.command, "cards")
        self.assertEqual(args.max_polls, 80)

    def test_post_error_body_raises(self):
        with self.assertRaisesRegex(RuntimeError, "Missing 'card_index'"):
            validate_post_response(
                {"status": "error", "error": "Missing 'card_index'"},
                {"action": "select_card_reward"},
            )

    def test_action_body_auto_targets_single_enemy_attack(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hand": [
                        {
                            "index": 0,
                            "name": "Uppercut",
                            "description": "Deal 13 damage. Apply 1 Weak.",
                        }
                    ]
                },
                "battle": {
                    "enemies": [{"entity_id": "MAWLER_0", "hp": 72}],
                    "turn": "player",
                    "is_play_phase": True,
                },
            }
        )

        body, _ = action_body_from_plan(
            fake,
            {"action": "play_card", "card": "Uppercut"},
            auto_target=True,
        )

        self.assertEqual(
            body,
            {
                "action": "play_card",
                "card_index": 0,
                "target": "MAWLER_0",
            },
        )

    def test_action_body_prefers_target_type_for_targeting(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hand": [
                        {
                            "index": 0,
                            "name": "Odd Target Card",
                            "description": "Do a mysterious thing.",
                            "target_type": "AnyEnemy",
                        }
                    ]
                },
                "battle": {
                    "enemies": [{"entity_id": "MAWLER_0", "hp": 72}],
                    "turn": "player",
                    "is_play_phase": True,
                },
            }
        )

        body, _ = action_body_from_plan(
            fake,
            {"action": "play_card", "card": "Odd Target Card"},
            auto_target=True,
        )

        self.assertEqual(body["target"], "MAWLER_0")

    def test_action_body_accepts_deck_selection_alias(self):
        body, _ = action_body_from_plan(
            FakeClient({"state_type": "card_select", "player": {"potions": []}}),
            {"action": "deck_select_card", "index": 1},
            auto_target=True,
        )

        self.assertEqual(body, {"action": "select_card", "index": 1})

    def test_deck_selection_alias_waits_for_selection_state_change(self):
        fake = FakeDelayedCardSelectClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"deck_select_card": 1}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "select_card", "index": 1})
        self.assertTrue(state["card_select"]["can_confirm"])

    def test_explicit_target_hint_is_ignored_for_non_target_card(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hand": [
                        {
                            "index": 0,
                            "name": "Flame Barrier+",
                            "description": "Gain 16 Block. Whenever you are attacked this turn, deal 6 damage back.",
                        }
                    ]
                },
                "battle": {
                    "enemies": [{"entity_id": "VANTOM_0", "hp": 72}],
                    "turn": "player",
                    "is_play_phase": True,
                },
            }
        )

        body, _ = action_body_from_plan(
            fake,
            {"action": "play_card", "card": "Flame Barrier+", "target": "first"},
            auto_target=True,
        )

        self.assertEqual(body, {"action": "play_card", "card_index": 0})

    def test_card_lookup_tolerates_missing_upgrade_suffix(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hand": [
                        {
                            "index": 0,
                            "name": "Shrug It Off+",
                            "description": "Gain 11 Block. Draw 1 card.",
                        }
                    ]
                },
                "battle": {
                    "enemies": [{"entity_id": "VANTOM_0", "hp": 72}],
                    "turn": "player",
                    "is_play_phase": True,
                },
            }
        )

        body, _ = action_body_from_plan(
            fake,
            {"action": "play_card", "card": "Shrug It Off"},
            auto_target=True,
        )

        self.assertEqual(body, {"action": "play_card", "card_index": 0})

    def test_state_delta_captures_combat_changes(self):
        before = {
            "state_type": "monster",
            "player": {
                "hp": 50,
                "block": 5,
                "energy": 3,
                "gold": 12,
                "hand": [
                    {"index": 0, "name": "Strike"},
                    {"index": 1, "name": "Defend"},
                ],
            },
            "battle": {
                "enemies": [
                    {"entity_id": "CULTIST_0", "name": "Cultist", "hp": 40, "block": 3}
                ]
            },
        }
        after = {
            "state_type": "monster",
            "player": {
                "hp": 48,
                "block": 0,
                "energy": 2,
                "gold": 12,
                "hand": [
                    {"index": 0, "name": "Defend"},
                    {"index": 1, "name": "Bash"},
                ],
            },
            "battle": {
                "enemies": [
                    {"entity_id": "CULTIST_0", "name": "Cultist", "hp": 31, "block": 0}
                ]
            },
        }

        delta = state_delta(before, after)

        self.assertEqual(delta["player"], {"hp": -2, "block": -5, "energy": -1})
        self.assertEqual(delta["combat"]["hand"], {"added": ["Bash"], "removed": ["Strike"]})
        self.assertEqual(
            delta["combat"]["enemies"],
            [{"id": "CULTIST_0", "name": "Cultist", "hp_delta": -9, "block_delta": -3}],
        )

    def test_decision_point_classifies_common_screens(self):
        self.assertEqual(
            decision_point(
                {
                    "state_type": "card_reward",
                    "card_reward": {"cards": [{"index": 0}, {"index": 1}, {"index": 2}]},
                }
            ),
            {"state_type": "card_reward", "kind": "card_reward", "option_count": 3},
        )
        self.assertEqual(
            decision_point(
                {
                    "state_type": "map",
                    "map": {"next_options": [{"index": 0}, {"index": 1}]},
                }
            ),
            {"state_type": "map", "kind": "map_choice", "option_count": 2},
        )
        self.assertEqual(
            decision_point(
                {
                    "state_type": "monster",
                    "battle": {
                        "turn": "player",
                        "is_play_phase": True,
                        "round": 2,
                        "enemies": [{"entity_id": "CULTIST_0"}],
                    },
                }
            ),
            {
                "state_type": "monster",
                "kind": "combat_turn",
                "round": 2,
                "turn": "player",
                "enemy_count": 1,
            },
        )

    def test_decision_point_marks_drainable_screens_as_trivial(self):
        self.assertEqual(
            decision_point(
                {
                    "state_type": "treasure",
                    "treasure": {"relics": [{"index": 0, "name": "Lantern"}]},
                }
            ),
            {
                "state_type": "treasure",
                "kind": "trivial",
                "trivial_reason": "claim only treasure relic",
            },
        )
        self.assertEqual(
            decision_point(
                {
                    "state_type": "event",
                    "event": {
                        "options": [
                            {"index": 0, "title": "Proceed", "is_proceed": True},
                        ]
                    },
                }
            ),
            {
                "state_type": "event",
                "kind": "trivial",
                "trivial_reason": "choose event proceed",
            },
        )
        self.assertEqual(
            decision_point(
                {
                    "state_type": "event",
                    "event": {"in_dialogue": True, "options": []},
                }
            ),
            {
                "state_type": "event",
                "kind": "trivial",
                "trivial_reason": "advance event dialogue",
            },
        )

    def test_state_delta_counts_removed_enemy_remaining_hp_as_damage(self):
        before = {
            "state_type": "monster",
            "player": {"hp": 50, "block": 0, "energy": 1, "gold": 12, "hand": []},
            "battle": {
                "enemies": [
                    {"entity_id": "CULTIST_0", "name": "Cultist", "hp": 6, "block": 2}
                ]
            },
        }
        after = {
            "state_type": "rewards",
            "player": {"hp": 50, "block": 0, "energy": None, "gold": 12, "hand": []},
            "rewards": {"items": []},
        }

        delta = state_delta(before, after)

        self.assertEqual(delta["state_type"], {"before": "monster", "after": "rewards"})
        self.assertEqual(
            delta["combat"]["enemies"],
            [
                {
                    "id": "CULTIST_0",
                    "name": "Cultist",
                    "hp_delta": -6,
                    "block_delta": -2,
                    "removed": True,
                }
            ],
        )

    def test_drain_logs_before_after_delta_and_next_decision(self):
        fake = FakeClient(
            {
                "state_type": "rewards",
                "player": {"potions": [], "max_potion_slots": 2},
                "rewards": {
                    "items": [
                        {"index": 0, "type": "gold", "description": "12 Gold"},
                        {"index": 1, "type": "card", "description": "Add a card"},
                    ],
                    "can_proceed": True,
                },
            }
        )
        stats = RunStats()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drain.jsonl"
            state, drained = drain_trivial(
                fake,
                logger=JsonlLogger(path),
                stats=stats,
                poll_delay=0,
            )
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(drained), 1)
        self.assertEqual(state["rewards"]["items"][0]["type"], "card")
        self.assertIn("drain_action", [event["kind"] for event in events])
        result = next(event for event in events if event["kind"] == "drain_result")
        self.assertEqual(result["delta"]["state_type"], {"before": "rewards", "after": "rewards"})
        self.assertEqual(result["decision_point"]["kind"], "reward_decision")

    def test_analyze_log_summarizes_waits_decisions_and_gameplay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.jsonl"
            rows = [
                {"seq": 1, "ts": "2026-06-22T10:00:00.000-07:00", "kind": "run_start"},
                {
                    "seq": 2,
                    "ts": "2026-06-22T10:00:00.020-07:00",
                    "kind": "http",
                    "method": "GET",
                    "path": "/api/v1/singleplayer",
                    "status_code": 200,
                    "elapsed_ms": 12.4,
                },
                {
                    "seq": 3,
                    "ts": "2026-06-22T10:00:00.120-07:00",
                    "kind": "wait",
                    "reason": "ready_state",
                    "polls": 2,
                    "elapsed_ms": 87.5,
                },
                {
                    "seq": 4,
                    "ts": "2026-06-22T10:00:00.200-07:00",
                    "kind": "action_result",
                    "decision_point": {"kind": "combat_turn"},
                    "delta": {
                        "state_type": {"before": "monster", "after": "monster"},
                        "player": {"hp": -1},
                        "combat": {
                            "enemies": [
                                {"id": "CULTIST_0", "hp_delta": -6, "block_delta": -2}
                            ]
                        },
                    },
                },
                {
                    "seq": 5,
                    "ts": "2026-06-22T10:00:00.240-07:00",
                    "kind": "state_result",
                    "decision_point": {"kind": "combat_turn"},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            summary = analyze_log(path)

        self.assertEqual(summary["event_kinds"]["action_result"], 1)
        self.assertEqual(summary["waits"]["reasons"], {"ready_state": 1})
        self.assertEqual(summary["waits"]["elapsed_ms"]["total_ms"], 87.5)
        self.assertEqual(summary["decision_points"], {"combat_turn": 1})
        self.assertEqual(summary["decision_point_events"], {"combat_turn": 2})
        self.assertEqual(summary["decision_points_by_phase"]["after"], {"combat_turn": 2})
        self.assertEqual(summary["decision_path"], [{"kind": "combat_turn"}])
        self.assertEqual(summary["final_decision_point"], {"kind": "combat_turn"})
        self.assertEqual(summary["state_transitions"], {"monster->monster": 1})
        self.assertEqual(summary["gameplay"]["player_hp_delta"], -1)
        self.assertEqual(summary["gameplay"]["enemy_hp_lost"], 6)
        self.assertEqual(summary["gameplay"]["enemy_block_lost"], 2)

    def test_analyze_logs_aggregates_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            first.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "seq": 1,
                            "ts": "2026-06-22T10:00:00.000-07:00",
                            "kind": "run_start",
                            "command": "state",
                            "argv": ["state"],
                        },
                        {
                            "seq": 2,
                            "ts": "2026-06-22T10:00:00.050-07:00",
                            "kind": "http",
                            "method": "GET",
                            "path": "/api/v1/singleplayer",
                            "elapsed_ms": 10,
                        },
                        {
                            "seq": 3,
                            "ts": "2026-06-22T10:00:00.100-07:00",
                            "kind": "run_end",
                            "http_calls": 1,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            second.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "seq": 1,
                            "ts": "2026-06-22T10:00:01.000-07:00",
                            "kind": "run_start",
                            "command": "cards",
                            "argv": ["cards", "Strike"],
                        },
                        {
                            "seq": 2,
                            "ts": "2026-06-22T10:00:01.050-07:00",
                            "kind": "http",
                            "method": "POST",
                            "path": "/api/v1/singleplayer",
                            "action": "play_card",
                            "elapsed_ms": 20,
                        },
                        {
                            "seq": 3,
                            "ts": "2026-06-22T10:00:01.200-07:00",
                            "kind": "run_end",
                            "actions": 1,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = analyze_logs([first, second])

        self.assertIsNone(summary["path"])
        self.assertEqual(summary["paths"], [str(first), str(second)])
        self.assertEqual(summary["http_calls"], 2)
        self.assertEqual(summary["http_total_ms"], 30)
        self.assertEqual(summary["active_wall_time_ms"], 300.0)
        self.assertEqual(summary["command_timing"]["inter_command_gaps"]["total_ms"], 900.0)
        self.assertEqual(summary["command_timing"]["inter_command_gaps"]["count"], 1)
        self.assertEqual(summary["command_timing"]["runs"][1]["gap_before_ms"], 900.0)
        self.assertEqual(summary["command_timing"]["runs"][1]["first_post_action"], "play_card")
        self.assertEqual(summary["command_timing"]["runs"][1]["first_post_delay_ms"], 30.0)

    def test_analyze_logs_reports_gap_until_next_post_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            post_one = Path(tmp) / "post-one.jsonl"
            state_read = Path(tmp) / "state-read.jsonl"
            post_two = Path(tmp) / "post-two.jsonl"

            post_one.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "seq": 1,
                            "ts": "2026-06-22T10:00:00.000-07:00",
                            "kind": "run_start",
                            "command": "cards",
                        },
                        {
                            "seq": 2,
                            "ts": "2026-06-22T10:00:00.050-07:00",
                            "kind": "http",
                            "method": "POST",
                            "path": "/api/v1/singleplayer",
                            "action": "play_card",
                            "elapsed_ms": 20,
                        },
                        {
                            "seq": 3,
                            "ts": "2026-06-22T10:00:00.200-07:00",
                            "kind": "run_end",
                            "actions": 1,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state_read.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "seq": 1,
                            "ts": "2026-06-22T10:00:01.000-07:00",
                            "kind": "run_start",
                            "command": "state",
                        },
                        {
                            "seq": 2,
                            "ts": "2026-06-22T10:00:01.100-07:00",
                            "kind": "run_end",
                            "actions": 0,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            post_two.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "seq": 1,
                            "ts": "2026-06-22T10:00:03.000-07:00",
                            "kind": "run_start",
                            "command": "cards",
                        },
                        {
                            "seq": 2,
                            "ts": "2026-06-22T10:00:03.050-07:00",
                            "kind": "http",
                            "method": "POST",
                            "path": "/api/v1/singleplayer",
                            "action": "end_turn",
                            "elapsed_ms": 25,
                        },
                        {
                            "seq": 3,
                            "ts": "2026-06-22T10:00:03.200-07:00",
                            "kind": "run_end",
                            "actions": 1,
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = analyze_logs([post_one, state_read, post_two])

        next_post_gaps = summary["command_timing"]["next_post_gaps"]
        self.assertEqual(next_post_gaps["command_start"]["count"], 1)
        self.assertEqual(next_post_gaps["command_start"]["total_ms"], 2800.0)
        self.assertEqual(next_post_gaps["first_post"]["total_ms"], 2825.0)
        self.assertEqual(next_post_gaps["pairs"][0]["to_first_post_action"], "end_turn")
        self.assertEqual(summary["command_timing"]["runs"][2]["first_post_delay_ms"], 25.0)
        self.assertEqual(
            summary["command_timing"]["runs"][2]["first_post_started_ts"],
            "2026-06-22T10:00:03.025-07:00",
        )

    def test_expand_log_path_args_expands_globs(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.jsonl"
            second = Path(tmp) / "b.jsonl"
            first.write_text("", encoding="utf-8")
            second.write_text("", encoding="utf-8")

            self.assertEqual(expand_log_path_args([str(Path(tmp) / "*.jsonl")]), [first, second])


if __name__ == "__main__":
    unittest.main()
