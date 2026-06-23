from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sts2_fast_cli as cli_module
from sts2_fast_cli import (
    DELAYED_CARD_SETTLE_POLLS,
    DEFAULT_INITIAL_POLL_DELAY,
    DEFAULT_MAX_POLLS,
    DEFAULT_MENU_MAX_POLLS,
    DEFAULT_POLL_DELAY,
    DEFAULT_START_RUN_MAX_POLLS,
    FAST_CARD_SETTLE_POLLS,
    JsonlLogger,
    MAP_COMBAT_SETTLE_POLLS,
    SELECTION_CARD_SETTLE_POLLS,
    START_TURN_SETTLE_POLLS,
    RunStats,
    action_body_from_plan,
    act_map_data,
    analyze_log,
    analyze_logs,
    build_parser,
    choose_character_option,
    decision_point,
    drain_trivial,
    emit_result,
    execute_menu_option,
    execute_actions,
    expand_action_macros,
    expand_log_path_args,
    normalize_action,
    output_log_path,
    run,
    start_run,
    next_trivial_action,
    summarize_state,
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


class FakeNeverStableDelayedPlayClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._after_post = False
        self._reads_after_post = 0

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
        self._reads_after_post += 1
        return self._state(hand=[], enemy_hp=40 - self._reads_after_post)

    def post(self, body):
        self.posts.append(body)
        self._after_post = True
        self._reads_after_post = 0
        return {}


class FakeDelayedAutoRewardClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._after_post = False
        self._reads_after_post = 0

    def _combat_state(self):
        return {
            "state_type": "boss",
            "player": {
                "hand": [
                    {
                        "index": 0,
                        "name": "Thinking Ahead",
                        "description": "Draw 2 cards. Put 1 card from your Hand on top of your Draw Pile. Exhaust.",
                    }
                ],
                "potions": [],
            },
            "battle": {
                "enemies": [{"entity_id": "BOSS_0", "name": "Boss", "hp": 13}],
                "turn": "player",
                "is_play_phase": True,
            },
        }

    def state(self):
        self.state_calls += 1
        if not self._after_post:
            return self._combat_state()
        self._reads_after_post += 1
        if self._reads_after_post <= 4:
            state = self._combat_state()
            state["player"]["hand"] = []
            return state
        return {
            "state_type": "rewards",
            "player": {"potions": []},
            "rewards": {"items": [{"index": 0, "type": "gold"}], "can_proceed": True},
        }

    def post(self, body):
        self.posts.append(body)
        self._after_post = True
        self._reads_after_post = 0
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
        self._current = self._state
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
            self._current = self._states.pop(0)
        return self._current

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
        self._current = self._state
        self._states = []

    def state(self):
        self.state_calls += 1
        if self._states:
            self._current = self._states.pop(0)
        return self._current

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

    def _combat_state(self, *, round_number, turn, hand, energy=3):
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


class FakeEndTurnEarlyRetainedCardClient(FakeStaleEndTurnClient):
    def post(self, body):
        self.posts.append(body)
        if body["action"] == "end_turn":
            early = self._combat_state(
                round_number=2,
                turn="player",
                hand=[{"index": 0, "name": "Demon Form", "cost": "3"}],
                energy=0,
            )
            ready = self._combat_state(
                round_number=2,
                turn="player",
                hand=[
                    {"index": 0, "name": "Demon Form", "cost": "3"},
                    {"index": 1, "name": "Battle Trance+", "cost": "0"},
                    {"index": 2, "name": "Shrug It Off+", "cost": "1"},
                ],
                energy=4,
            )
            self._states = [early, early, ready, ready]
        return {}


class FakeEndTurnEarlyPlayableRetainedCardClient(FakeStaleEndTurnClient):
    def post(self, body):
        self.posts.append(body)
        if body["action"] == "end_turn":
            early = self._combat_state(
                round_number=2,
                turn="player",
                hand=[{"index": 0, "name": "Shrug It Off", "cost": "1"}],
                energy=4,
            )
            ready = self._combat_state(
                round_number=2,
                turn="player",
                hand=[
                    {"index": 0, "name": "Shrug It Off", "cost": "1"},
                    {"index": 1, "name": "Battle Trance+", "cost": "0"},
                    {"index": 2, "name": "Strike", "cost": "1"},
                    {"index": 3, "name": "Defend", "cost": "1"},
                ],
                energy=4,
            )
            self._states = [early] * 5 + [ready] * 3
        return {}


class FakeNeverStableEndTurnClient(FakeStaleEndTurnClient):
    def post(self, body):
        self.posts.append(body)
        if body["action"] == "end_turn":
            self._states = [
                self._combat_state(
                    round_number=2,
                    turn="player",
                    hand=[{"index": 0, "name": f"Strike {index}", "cost": "1"}],
                    energy=3,
                )
                for index in range(8)
            ]
        return {}


class FakeMapChoiceDelayedModalClient(FakeMapChoiceEarlyCombatClient):
    def _card_select(self):
        return {
            "state_type": "card_select",
            "player": {"hp": 50, "block": 0, "energy": 3, "hand": []},
            "card_select": {
                "prompt": "Choose one.",
                "can_confirm": False,
                "cards": [
                    {"index": 0, "name": "Choice A"},
                    {"index": 1, "name": "Choice B"},
                ],
            },
        }

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "choose_map_node":
            early = self._combat_state(
                hand=[{"index": 0, "name": "Defend", "cost": "1"}],
                energy=3,
            )
            modal = self._card_select()
            self._states = [early] * 5 + [modal] * 3
        return {}


class FakeDelayedHandSelectAfterCardClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._states = []
        self._current = self._combat_state(
            hand=[
                {
                    "index": 0,
                    "name": "Thinking Ahead",
                    "cost": "0",
                    "description": "Draw 2 cards. Put 1 card from your Hand on top of your Draw Pile. Exhaust.",
                },
                {"index": 1, "name": "Strike", "cost": "1", "description": "Deal 6 damage."},
            ],
        )

    def _combat_state(self, *, hand):
        return {
            "state_type": "monster",
            "player": {"hp": 50, "block": 0, "energy": 3, "hand": hand},
            "battle": {
                "round": 2,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "OWL_MAGISTRATE_0", "name": "Owl Magistrate", "hp": 184}],
            },
        }

    def _hand_select(self):
        return {
            "state_type": "hand_select",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [
                    {"index": 0, "name": "Bash", "cost": "2"},
                    {"index": 1, "name": "Fisticuffs", "cost": "1"},
                ],
            },
            "battle": {
                "round": 2,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "OWL_MAGISTRATE_0", "name": "Owl Magistrate", "hp": 184}],
            },
            "hand_select": {
                "mode": "simple_select",
                "prompt": "Choose a card to put on top of your Draw Pile.",
                "cards": [
                    {"index": 0, "name": "Bash", "cost": "2"},
                    {"index": 1, "name": "Fisticuffs", "cost": "1"},
                ],
                "can_confirm": False,
            },
        }

    def state(self):
        self.state_calls += 1
        if self._states:
            self._current = self._states.pop(0)
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "play_card":
            settled_too_early = self._combat_state(
                hand=[
                    {"index": 0, "name": "Bash", "cost": "2"},
                    {"index": 1, "name": "Fisticuffs", "cost": "1"},
                ],
            )
            modal = self._hand_select()
            self._states = [settled_too_early] * 6 + [modal] * 3
        return {}


class FakePartialModalAfterCardClient(FakeDelayedHandSelectAfterCardClient):
    def _partial_hand_select(self):
        state = self._hand_select()
        state["hand_select"]["cards"] = []
        state["hand_select"]["prompt"] = "Choose a card..."
        return state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "play_card":
            full = self._hand_select()
            self._states = [self._partial_hand_select(), full, full]
        return {}


class FakeLongDelayedAutoRewardClient(FakeDelayedAutoRewardClient):
    def _combat_state(self):
        return {
            "state_type": "monster",
            "player": {
                "hand": [
                    {
                        "index": 0,
                        "name": "Battle Trance",
                        "description": "Draw 3 cards. You cannot draw additional cards this turn.",
                    }
                ],
                "potions": [],
            },
            "battle": {
                "enemies": [{"entity_id": "OWL_MAGISTRATE_0", "name": "Owl Magistrate", "hp": 53}],
                "turn": "player",
                "is_play_phase": True,
            },
        }

    def state(self):
        self.state_calls += 1
        if not self._after_post:
            return self._combat_state()
        self._reads_after_post += 1
        if self._reads_after_post <= 8:
            state = self._combat_state()
            state["player"]["hand"] = [
                {"index": 0, "name": "Thunderclap", "description": "Deal 4 damage to ALL enemies."},
                {"index": 1, "name": "Flame Barrier", "description": "Gain 12 Block."},
            ]
            return state
        return {
            "state_type": "rewards",
            "player": {"potions": []},
            "rewards": {"items": [{"index": 0, "type": "gold"}], "can_proceed": True},
        }


class FakeDelayedPotionModalClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._states = []
        self._current = {
            "state_type": "monster",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "potions": [{"slot": 0, "name": "Skill Potion", "target_type": "Self"}],
                "hand": [{"index": 0, "name": "Strike", "cost": "1"}],
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
            },
        }

    def _combat_without_potion(self):
        state = deepcopy(self._current)
        state["player"]["potions"] = []
        state["player"]["hand"] = [
            {"index": 0, "name": "Strike", "cost": "1"},
            {"index": 1, "name": "Defend", "cost": "1"},
        ]
        return state

    def _card_select(self):
        return {
            "state_type": "card_select",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "potions": [],
                "hand": [
                    {"index": 0, "name": "Strike", "cost": "1"},
                    {"index": 1, "name": "Defend", "cost": "1"},
                ],
            },
            "card_select": {
                "prompt": "Choose a card.",
                "can_cancel": True,
                "cards": [
                    {"index": 0, "name": "Taunt"},
                    {"index": 1, "name": "Impervious"},
                    {"index": 2, "name": "Rage"},
                ],
            },
        }

    def state(self):
        self.state_calls += 1
        if self._states:
            self._current = self._states.pop(0)
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "use_potion":
            stale_combat = self._combat_without_potion()
            modal = self._card_select()
            self._states = [stale_combat] * 8 + [modal, modal]
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
        elif body["action"] == "confirm_selection":
            self._state = {
                "state_type": "map",
                "player": {"potions": []},
                "map": {"next_options": [{"index": 0, "type": "Monster"}]},
            }
        return {}


class FakeDelayedHandSelectClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "hand_select",
            "player": {
                "hp": 80,
                "max_hp": 80,
                "block": 0,
                "energy": 4,
                "gold": 114,
                "potions": [],
                "hand": [
                    {"index": 0, "name": "Strike"},
                    {"index": 1, "name": "Flame Barrier+"},
                ],
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "DEVOTED_SCULPTOR_0", "name": "Devoted Sculptor", "hp": 162}],
            },
            "hand_select": {
                "mode": "simple_select",
                "prompt": "Choose a card to put on top of your Draw Pile.",
                "cards": [
                    {"index": 0, "name": "Strike", "cost": "1", "description": "Deal 6 damage."},
                    {"index": 1, "name": "Flame Barrier+", "cost": "2", "description": "Gain 16 Block."},
                ],
                "can_confirm": False,
            },
        }

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "combat_select_card":
            self._state = {
                "state_type": "monster",
                "player": {
                    "hp": 80,
                    "max_hp": 80,
                    "block": 0,
                    "energy": 3,
                    "gold": 114,
                    "potions": [],
                    "hand": [{"index": 0, "name": "Strike"}],
                },
                "battle": {
                    "round": 1,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [
                        {"entity_id": "DEVOTED_SCULPTOR_0", "name": "Devoted Sculptor", "hp": 162}
                    ],
                },
            }
        return {}


class FakeConfirmHandSelectionDelayedRewardClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._states = []
        self._current = {
            "state_type": "hand_select",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [{"index": 0, "name": "Bash"}],
            },
            "hand_select": {
                "mode": "simple_select",
                "prompt": "Choose a card to put on top of your Draw Pile.",
                "selected_cards": [{"index": 0, "name": "Bash"}],
                "cards": [{"index": 0, "name": "Bash"}],
                "can_confirm": True,
            },
        }

    def _combat_state(self):
        return {
            "state_type": "monster",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [{"index": 0, "name": "Thunderclap"}],
            },
            "battle": {
                "round": 2,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "OWL_MAGISTRATE_0", "name": "Owl Magistrate", "hp": 12}],
            },
        }

    def _rewards_state(self):
        return {
            "state_type": "rewards",
            "player": {"potions": []},
            "rewards": {"items": [{"index": 0, "type": "gold"}], "can_proceed": True},
        }

    def state(self):
        self.state_calls += 1
        if self._states:
            self._current = self._states.pop(0)
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "combat_confirm_selection":
            self._states = [self._combat_state()] * 6 + [self._rewards_state()] * 3
        return {}


class FakeHandPickConfirmClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._current = {
            "state_type": "hand_select",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [
                    {"index": 0, "name": "Defend"},
                    {"index": 1, "name": "Bash"},
                ],
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
            },
            "hand_select": {
                "mode": "simple_select",
                "prompt": "Choose a card to put on top of your Draw Pile.",
                "selected_cards": [],
                "cards": [
                    {"index": 0, "name": "Defend"},
                    {"index": 1, "name": "Bash"},
                ],
                "can_confirm": False,
            },
        }

    def _selected_state(self):
        state = deepcopy(self._current)
        state["hand_select"]["selected_cards"] = [{"index": 1, "name": "Bash"}]
        state["hand_select"]["cards"] = [{"index": 0, "name": "Defend"}]
        state["hand_select"]["can_confirm"] = True
        return state

    def _combat_state(self):
        return {
            "state_type": "monster",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [{"index": 0, "name": "Strike", "cost": "1"}],
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
            },
        }

    def state(self):
        self.state_calls += 1
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "combat_select_card":
            self._current = self._selected_state()
        elif body["action"] == "combat_confirm_selection":
            self._current = self._combat_state()
        return {}


class FakeSimpleStableCardPlayClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._current = {
            "state_type": "monster",
            "player": {
                "hp": 50,
                "block": 0,
                "energy": 3,
                "hand": [
                    {"index": 0, "name": "Strike", "cost": "1", "description": "Deal 6 damage."},
                ],
            },
            "battle": {
                "round": 1,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
            },
        }

    def state(self):
        self.state_calls += 1
        return self._current

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "play_card":
            self._current = {
                "state_type": "monster",
                "player": {
                    "hp": 50,
                    "block": 0,
                    "energy": 2,
                    "hand": [],
                },
                "battle": {
                    "round": 1,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 14}],
                },
            }
        return {}


class FakeBundlePickClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = self._bundle_select(can_confirm=False)
        self._stale_reads = 0

    def _bundle_select(self, *, can_confirm):
        return {
            "state_type": "bundle_select",
            "player": {"potions": []},
            "bundle_select": {
                "prompt": "Choose a bundle.",
                "can_confirm": can_confirm,
                "can_cancel": True,
                "selected_bundle": {"index": 0, "name": "Bundle A"} if can_confirm else None,
                "bundles": [
                    {"index": 0, "name": "Bundle A"},
                    {"index": 1, "name": "Bundle B"},
                ],
            },
        }

    def state(self):
        self.state_calls += 1
        if self._stale_reads > 0:
            self._stale_reads -= 1
            return self._bundle_select(can_confirm=False)
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] == "select_bundle":
            self._stale_reads = 1
            self._state = self._bundle_select(can_confirm=True)
        elif body["action"] == "confirm_bundle_selection":
            self._state = {
                "state_type": "map",
                "player": {"potions": []},
                "map": {"next_options": [{"index": 0, "type": "Monster"}]},
            }
        return {}


class FakeMenuStartRunClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self.endpoint = "singleplayer"
        self.active_wait_id = None
        self._state = self._main_menu()

    def _main_menu(self):
        return {
            "state_type": "menu",
            "menu_screen": "main",
            "player": {"potions": []},
            "options": ["singleplayer", "settings", "quit"],
        }

    def _singleplayer_menu(self):
        return {
            "state_type": "menu",
            "menu_screen": "singleplayer",
            "player": {"potions": []},
            "options": [
                {"name": "standard", "enabled": True},
                {"name": "daily", "enabled": False},
                {"name": "back", "enabled": True},
            ],
        }

    def _character_select(self, *, confirm_enabled):
        return {
            "state_type": "menu",
            "menu_screen": "character_select",
            "player": {"potions": []},
            "characters": [
                {"id": "ironclad", "name": "Ironclad", "locked": False},
                {"id": "silent", "name": "Silent", "locked": True},
            ],
            "options": [
                {"name": "ironclad", "enabled": True},
                {"name": "confirm", "enabled": confirm_enabled},
                {"name": "back", "enabled": True},
            ],
        }

    def _map_state(self):
        return {
            "state_type": "map",
            "player": {"potions": [], "hp": 80, "max_hp": 80},
            "run": {"act": 1, "floor": 0, "ascension": 0},
            "map": {"next_options": [{"index": 0, "type": "Monster"}]},
        }

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        if body["action"] != "menu_select":
            return {}
        option = body["option"]
        if option == "singleplayer":
            self._state = self._singleplayer_menu()
        elif option == "standard":
            self._state = self._character_select(confirm_enabled=False)
        elif option == "ironclad":
            self._state = self._character_select(confirm_enabled=True)
        elif option in {"confirm", "embark"}:
            self._state = self._map_state()
        return {"status": "ok"}


class FakeBlockedTimelineStartRunClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "menu",
            "menu_screen": "main",
            "player": {"potions": []},
            "options": ["settings", "quit"],
            "blocked_options": [
                {
                    "name": "timeline",
                    "reason": "manual_epoch_reveal_required",
                    "pending_epoch_ids": ["epoch_a", "epoch_b"],
                }
            ],
        }

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        return {"status": "error", "manual_action_required": True}


class FakeGameOverToBlockedTimelineStartRunClient(FakeBlockedTimelineStartRunClient):
    def __init__(self):
        super().__init__()
        self._blocked_state = self._state
        self._state = {
            "state_type": "game_over",
            "player": {"potions": []},
            "run": {"act": 3, "floor": 48},
        }

    def post(self, body):
        self.posts.append(body)
        if body == {"action": "menu_select", "option": "main_menu"}:
            self._state = self._blocked_state
            return {"status": "ok"}
        return {"status": "error", "manual_action_required": True}


class FakeSlotUnlockTimelineStartRunClient(FakeBlockedTimelineStartRunClient):
    def __init__(self):
        super().__init__()
        self.timeline_gets = []
        self.timeline_posts = []

    def get_json(self, path, *, params=None):
        self.timeline_gets.append({"path": path, "params": params})
        if path == "/api/v1/timeline":
            return {
                "status": "ok",
                "pending_epoch_ids": ["NEOW_EPOCH"],
                "pending_slot_unlock_epoch_ids": ["NEOW_EPOCH"],
            }
        return {"status": "error", "error": "unexpected get"}

    def post_json_unchecked(self, path, body):
        self.timeline_posts.append({"path": path, "body": body})
        return {"status": "error", "error": "unexpected mutation"}


class FakeAutoRevealTimelineStartRunClient(FakeMenuStartRunClient):
    def __init__(self):
        super().__init__()
        self.timeline_posts = []
        self._state = {
            "state_type": "menu",
            "menu_screen": "main",
            "player": {"potions": []},
            "options": ["settings", "quit"],
            "blocked_options": [
                {
                    "name": "timeline",
                    "reason": "manual_epoch_reveal_required",
                    "pending_epoch_ids": ["epoch_a", "epoch_b"],
                }
            ],
        }

    def post_json(self, path, body):
        self.timeline_posts.append({"path": path, "body": body})
        if path == "/api/v1/timeline" and body == {"action": "reveal_pending", "dry_run": False}:
            self._state = self._main_menu()
            return {
                "status": "ok",
                "pending_epoch_ids": ["epoch_a", "epoch_b"],
                "revealed_epoch_ids": ["epoch_a", "epoch_b"],
                "changed": True,
            }
        return {"status": "error", "error": "unexpected timeline request"}


class FakeStuckTimelineStartRunClient(FakeAutoRevealTimelineStartRunClient):
    def post_json(self, path, body):
        self.timeline_posts.append({"path": path, "body": body})
        if path == "/api/v1/timeline" and body == {"action": "reveal_pending", "dry_run": False}:
            return {
                "status": "ok",
                "pending_epoch_ids": ["epoch_a", "epoch_b"],
                "revealed_epoch_ids": ["epoch_a", "epoch_b"],
                "changed": True,
            }
        return {"status": "error", "error": "unexpected timeline request"}


class FakeTimelineCliClient:
    def __init__(self):
        self.gets = []
        self.posts = []

    def close(self):
        pass

    def get_json(self, path, *, params=None):
        self.gets.append({"path": path, "params": params})
        if path == "/api/v1/timeline":
            return {"status": "ok", "pending_epoch_ids": ["epoch_a"], "pending_count": 1}
        return {"status": "error", "error": "unexpected get"}

    def post_json(self, path, body):
        self.posts.append({"path": path, "body": body})
        if path == "/api/v1/timeline":
            return {
                "status": "ok",
                "dry_run": body.get("dry_run"),
                "pending_epoch_ids": ["epoch_a"],
                "revealed_epoch_ids": [] if body.get("dry_run") else ["epoch_a"],
            }
        return {"status": "error", "error": "unexpected post"}


class FakeTimelineErrorCliClient(FakeTimelineCliClient):
    def post_json(self, path, body):
        raise RuntimeError("validated post_json should not be used for timeline reveal")

    def post_json_unchecked(self, path, body):
        self.posts.append({"path": path, "body": body})
        if path == "/api/v1/timeline":
            return {
                "status": "error",
                "error": "Timeline blocker includes epochs in ObtainedNoSlot state",
                "pending_epoch_ids": ["NEOW_EPOCH"],
                "pending_slot_unlock_epoch_ids": ["NEOW_EPOCH"],
                "manual_action_required": True,
            }
        return {"status": "error", "error": "unexpected post"}


class FakeNoChangeMenuClient:
    def __init__(self):
        self.posts = []
        self.state_calls = 0
        self._state = {
            "state_type": "menu",
            "menu_screen": "timeline",
            "player": {"potions": []},
            "options": [{"name": "advance", "enabled": True}],
        }

    def state(self):
        self.state_calls += 1
        return self._state

    def post(self, body):
        self.posts.append(body)
        return {"status": "ok", "done": True, "message": "No more epochs to advance"}


class FakeActiveRunClient:
    def __init__(self):
        self.endpoint = "singleplayer"
        self.active_wait_id = None

    def state(self):
        return {
            "state_type": "monster",
            "player": {"potions": [], "hand": []},
            "battle": {"turn": "player", "is_play_phase": True, "enemies": []},
        }

    def post(self, body):
        return {"status": "ok"}


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

    def test_expand_action_macros_supports_pick_and_confirm_shorthands(self):
        self.assertEqual(
            expand_action_macros([{"hand_pick": 1}, {"deck_pick": 4}, {"bundle_pick": 0}]),
            [
                {
                    "action": "combat_select_card",
                    "card_index": 1,
                    "_macro": "hand_pick",
                    "_macro_select_state": "hand_select",
                },
                {
                    "action": "combat_confirm_selection",
                    "_macro": "hand_pick",
                    "_macro_confirm_state": "hand_select",
                },
                {
                    "action": "select_card",
                    "index": 4,
                    "_macro": "deck_pick",
                    "_macro_select_state": "card_select",
                },
                {
                    "action": "confirm_selection",
                    "_macro": "deck_pick",
                    "_macro_confirm_state": "card_select",
                },
                {
                    "action": "select_bundle",
                    "index": 0,
                    "_macro": "bundle_pick",
                    "_macro_select_state": "bundle_select",
                },
                {
                    "action": "confirm_bundle_selection",
                    "_macro": "bundle_pick",
                    "_macro_confirm_state": "bundle_select",
                },
            ],
        )

    def test_hand_pick_macro_selects_and_confirms_in_one_action_plan(self):
        fake = FakeHandPickConfirmClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"hand_pick": 1}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=20,
            poll_delay=0,
        )

        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(
            [post["action"] for post in fake.posts],
            ["combat_select_card", "combat_confirm_selection"],
        )
        self.assertEqual(
            [item["planned"].get("_macro") for item in executed],
            ["hand_pick", "hand_pick"],
        )

    def test_hand_pick_macro_skips_confirm_when_selection_resolves_modal(self):
        fake = FakeDelayedHandSelectClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"hand_pick": 1}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=20,
            poll_delay=0,
        )

        self.assertEqual(state["state_type"], "monster")
        self.assertEqual([post["action"] for post in fake.posts], ["combat_select_card"])
        self.assertTrue(executed[-1]["skipped"])

    def test_deck_pick_macro_waits_for_confirmable_card_select(self):
        fake = FakeDelayedCardSelectClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"deck_pick": 1}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=20,
            poll_delay=0,
        )

        self.assertEqual(state["state_type"], "map")
        self.assertEqual([post["action"] for post in fake.posts], ["select_card", "confirm_selection"])
        self.assertEqual(
            [item["planned"].get("_macro") for item in executed],
            ["deck_pick", "deck_pick"],
        )

    def test_bundle_pick_macro_waits_for_confirmable_bundle_select(self):
        fake = FakeBundlePickClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"bundle_pick": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=20,
            poll_delay=0,
        )

        self.assertEqual(state["state_type"], "map")
        self.assertEqual(
            [post["action"] for post in fake.posts],
            ["select_bundle", "confirm_bundle_selection"],
        )
        self.assertEqual(
            [item["planned"].get("_macro") for item in executed],
            ["bundle_pick", "bundle_pick"],
        )

    def test_parser_exposes_fast_action_waits_for_act_and_cards(self):
        parser = build_parser()

        act_args = parser.parse_args(
            ["--compact", "act", "[{\"play\":\"Strike\"}]", "--fast-action-waits"]
        )
        cards_args = parser.parse_args(["cards", "Strike", "--fast-action-waits"])

        self.assertTrue(act_args.fast_action_waits)
        self.assertTrue(cards_args.fast_action_waits)

    def test_fast_action_waits_reduce_simple_card_settle_poll_count(self):
        fake_default = FakeSimpleStableCardPlayClient()
        default_stats = RunStats()
        execute_actions(
            fake_default,
            [{"play": "Strike", "target": "first"}],
            logger=quiet_logger(),
            stats=default_stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=10,
            poll_delay=0,
        )

        fake_fast = FakeSimpleStableCardPlayClient()
        fast_stats = RunStats()
        execute_actions(
            fake_fast,
            [{"play": "Strike", "target": "first"}],
            logger=quiet_logger(),
            stats=fast_stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=10,
            poll_delay=0,
            fast_action_waits=True,
        )

        self.assertEqual(FAST_CARD_SETTLE_POLLS, 1)
        self.assertLess(fake_fast.state_calls, fake_default.state_calls)
        self.assertEqual(fake_fast.posts, fake_default.posts)

    def test_fast_action_waits_keep_intermediate_card_actions_conservative(self):
        fake = FakeClient(
            {
                "state_type": "monster",
                "player": {
                    "hp": 50,
                    "block": 0,
                    "energy": 3,
                    "potions": [],
                    "hand": [
                        {"index": 0, "name": "Strike", "cost": "1", "description": "Deal 6 damage."},
                        {"index": 1, "name": "Strike", "cost": "1", "description": "Deal 6 damage."},
                    ],
                },
                "battle": {
                    "round": 1,
                    "turn": "player",
                    "is_play_phase": True,
                    "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
                },
            }
        )
        stats = RunStats()

        execute_actions(
            fake,
            [
                {"play": "Strike", "target": "first"},
                {"play": "Strike", "target": "first"},
            ],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=10,
            poll_delay=0,
            fast_action_waits=True,
        )

        self.assertEqual(fake.state_calls, 4)
        self.assertEqual([post["card_index"] for post in fake.posts], [0, 0])

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

    def test_next_trivial_action_leaves_spent_shop(self):
        state = {
            "state_type": "shop",
            "player": {"potions": []},
            "shop": {
                "can_proceed": False,
                "items": [
                    {"index": 0, "is_stocked": True, "can_afford": False},
                    {"index": 1, "is_stocked": False, "can_afford": False},
                ],
            },
        }

        body, reason = next_trivial_action(state)

        self.assertEqual(body, {"action": "proceed"})
        self.assertEqual(reason, "leave spent shop")

    def test_drain_leaves_spent_shop_even_when_can_proceed_false(self):
        fake = FakeClient(
            {
                "state_type": "shop",
                "player": {"potions": []},
                "shop": {
                    "can_proceed": False,
                    "items": [
                        {"index": 0, "is_stocked": True, "can_afford": False},
                        {"index": 1, "is_stocked": False, "can_afford": False},
                    ],
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

        self.assertEqual([item["reason"] for item in drained], ["leave spent shop", "choose only map node"])
        self.assertEqual([post["action"] for post in fake.posts], ["proceed", "choose_map_node"])
        self.assertEqual(state["state_type"], "monster")

    def test_drain_keeps_shop_when_any_stocked_item_is_affordable(self):
        fake = FakeClient(
            {
                "state_type": "shop",
                "player": {"potions": []},
                "shop": {
                    "can_proceed": False,
                    "items": [
                        {"index": 0, "is_stocked": True, "can_afford": False},
                        {"index": 1, "is_stocked": True, "can_afford": True},
                    ],
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

        self.assertEqual(drained, [])
        self.assertEqual(fake.posts, [])
        self.assertEqual(state["state_type"], "shop")

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
            max_polls=2,
            poll_delay=0,
        )

        self.assertEqual([item["body"]["card_index"] for item in executed], [0, 1])
        self.assertTrue(all(item["body"]["target"] == "JAW_WORM_0" for item in executed))
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Defend"])
        self.assertEqual(fake.state_calls, 5)

    def test_combat_actions_are_rejected_on_card_select_even_with_stale_hand(self):
        fake = FakeClient(
            {
                "state_type": "card_select",
                "player": {
                    "hp": 50,
                    "potions": [{"slot": 0, "name": "Skill Potion", "target_type": "Self"}],
                    "hand": [
                        {"index": 0, "name": "Shrug It Off+", "description": "Gain 11 Block. Draw 1 card."},
                        {"index": 1, "name": "Strike", "description": "Deal 6 damage."},
                    ],
                },
                "card_select": {
                    "prompt": "Choose a card.",
                    "can_cancel": True,
                    "cards": [{"index": 0, "name": "Taunt"}],
                },
            }
        )
        stats = RunStats()

        with self.assertRaisesRegex(RuntimeError, "resolve the current decision screen first"):
            execute_actions(
                fake,
                [{"play": "Shrug It Off+"}],
                logger=quiet_logger(),
                stats=stats,
                auto_target=True,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=1,
                poll_delay=0,
            )

        self.assertEqual(fake.posts, [])
        self.assertEqual(stats.actions, 0)

    def test_end_turn_is_rejected_on_card_select(self):
        fake = FakeClient(
            {
                "state_type": "card_select",
                "player": {"hp": 50, "potions": [], "hand": [{"index": 0, "name": "Strike"}]},
                "card_select": {
                    "prompt": "Choose a card.",
                    "can_cancel": True,
                    "cards": [{"index": 0, "name": "Taunt"}],
                },
            }
        )
        stats = RunStats()

        with self.assertRaisesRegex(RuntimeError, "Cannot end the turn"):
            execute_actions(
                fake,
                [{"end_turn": True}],
                logger=quiet_logger(),
                stats=stats,
                auto_target=True,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=1,
                poll_delay=0,
            )

        self.assertEqual(fake.posts, [])
        self.assertEqual(stats.actions, 0)

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
            max_polls=12,
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
            max_polls=12,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(state["player"]["energy"], 3)
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Strike"])

    def test_map_choice_does_not_return_early_combat_before_delayed_modal(self):
        fake = FakeMapChoiceDelayedModalClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"map": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=12,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "card_select")
        self.assertEqual(
            [card["name"] for card in state["card_select"]["cards"]],
            ["Choice A", "Choice B"],
        )

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

    def test_mp_action_alias_switches_client_endpoint(self):
        fake = FakeMapChoiceEarlyEventClient()
        fake.endpoint = "singleplayer"
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "mp_map_vote", "node_index": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(fake.endpoint, "multiplayer")
        self.assertEqual(executed[0]["body"], {"action": "choose_map_node", "index": 0})
        self.assertEqual(state["state_type"], "event")
        self.assertEqual(fake.state_calls, 2)

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
            max_polls=12,
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
            max_polls=16,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "end_turn"})
        self.assertEqual(state["state_type"], "rewards")

    def test_end_turn_ignores_energy_zero_retained_card_transient(self):
        fake = FakeEndTurnEarlyRetainedCardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"end_turn": True}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=16,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "end_turn"})
        self.assertEqual(state["player"]["energy"], 4)
        self.assertEqual(
            [card["name"] for card in state["player"]["hand"]],
            ["Demon Form", "Battle Trance+", "Shrug It Off+"],
        )

    def test_end_turn_ignores_playable_retained_card_until_full_turn_settles(self):
        fake = FakeEndTurnEarlyPlayableRetainedCardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"end_turn": True}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=18,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "end_turn"})
        self.assertEqual(state["player"]["energy"], 4)
        self.assertEqual(
            [card["name"] for card in state["player"]["hand"]],
            ["Shrug It Off", "Battle Trance+", "Strike", "Defend"],
        )

    def test_end_turn_times_out_instead_of_returning_unsettled_state(self):
        fake = FakeNeverStableEndTurnClient()
        stats = RunStats()

        with self.assertRaisesRegex(RuntimeError, "Timed out waiting for end turn"):
            execute_actions(
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

        self.assertEqual(fake.posts, [{"action": "end_turn"}])

    def test_skill_potion_waits_for_delayed_modal_selection(self):
        fake = FakeDelayedPotionModalClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"potion": 0}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=20,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "use_potion", "slot": 0})
        self.assertEqual(state["state_type"], "card_select")
        self.assertEqual(
            [card["name"] for card in state["card_select"]["cards"]],
            ["Taunt", "Impervious", "Rage"],
        )

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

    def test_play_card_times_out_instead_of_returning_unsettled_state(self):
        fake = FakeNeverStableDelayedPlayClient()
        stats = RunStats()

        with self.assertRaisesRegex(RuntimeError, "Timed out waiting for card play"):
            execute_actions(
                fake,
                [{"action": "play_card", "card": "Strike"}],
                logger=quiet_logger(),
                stats=stats,
                auto_target=True,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=4,
                poll_delay=0,
            )

        self.assertEqual(fake.posts[0]["action"], "play_card")

    def test_draw_or_auto_effect_card_waits_for_extra_settle_state_change(self):
        fake = FakeDelayedAutoRewardClient()
        stats = RunStats()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delayed-auto-reward.jsonl"
            state, executed = execute_actions(
                fake,
                [{"action": "play_card", "card": "Thinking Ahead"}],
                logger=JsonlLogger(path),
                stats=stats,
                auto_target=True,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=8,
                poll_delay=0,
            )
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(executed[0]["body"]["card_index"], 0)
        self.assertEqual(state["state_type"], "rewards")
        wait = next(event for event in events if event["kind"] == "wait")
        self.assertEqual(wait["reason"], "card_play_state_changed_settled")
        self.assertGreaterEqual(wait["polls"], 5)

    def test_card_play_waits_for_delayed_hand_select_after_combat_looking_state(self):
        fake = FakeDelayedHandSelectAfterCardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "play_card", "card": "Thinking Ahead"}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=14,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"]["card_index"], 0)
        self.assertEqual(state["state_type"], "hand_select")
        self.assertEqual(state["hand_select"]["prompt"], "Choose a card to put on top of your Draw Pile.")

    def test_card_play_waits_for_partial_modal_state_to_fill_in(self):
        fake = FakePartialModalAfterCardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "play_card", "card": "Thinking Ahead"}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=6,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"]["card_index"], 0)
        self.assertEqual(state["state_type"], "hand_select")
        self.assertEqual(
            [card["name"] for card in state["hand_select"]["cards"]],
            ["Bash", "Fisticuffs"],
        )

    def test_draw_auto_effect_card_waits_for_late_reward_transition(self):
        fake = FakeLongDelayedAutoRewardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "play_card", "card": "Battle Trance"}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=16,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"]["card_index"], 0)
        self.assertEqual(state["state_type"], "rewards")

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

    def test_mcp_tool_action_aliases_normalize_to_http_actions(self):
        self.assertEqual(
            normalize_action({"action": "rewards_claim", "reward_index": 2}),
            {"reward_index": 2, "action": "claim_reward", "index": 2},
        )
        self.assertEqual(
            normalize_action({"action": "map_choose_node", "node_index": 1}),
            {"node_index": 1, "action": "choose_map_node", "index": 1},
        )
        self.assertEqual(
            normalize_action({"action": "mp_rewards_pick_card", "card_index": 2}),
            {
                "card_index": 2,
                "action": "select_card_reward",
                "_endpoint": "multiplayer",
                "_wait": False,
            },
        )
        self.assertEqual(
            normalize_action("mp_combat_undo_end_turn"),
            {"action": "undo_end_turn", "_endpoint": "multiplayer", "_wait": False},
        )

    def test_menu_shorthand_normalizes_to_menu_select(self):
        self.assertEqual(
            normalize_action({"menu": "singleplayer"}),
            {"action": "menu_select", "option": "singleplayer"},
        )

    def test_return_to_menu_action_posts_and_waits_for_menu(self):
        class FakeReturnMenuClient:
            def __init__(self):
                self.posts = []
                self.state_calls = 0
                self._state = {
                    "state_type": "monster",
                    "player": {
                        "potions": [],
                        "hand": [{"index": 0, "name": "Strike"}],
                    },
                    "battle": {
                        "turn": "player",
                        "is_play_phase": True,
                        "enemies": [{"entity_id": "NIBBIT_0", "hp": 44}],
                    },
                }
                self._states = []

            def state(self):
                self.state_calls += 1
                if self._states:
                    self._state = self._states.pop(0)
                return self._state

            def post(self, body):
                self.posts.append(body)
                if body["action"] == "return_to_menu":
                    unknown = {"state_type": "unknown", "player": {"potions": []}}
                    empty_menu = {
                        "state_type": "menu",
                        "menu_screen": "main",
                        "options": [],
                        "player": {"potions": []},
                    }
                    ready_menu = {
                        "state_type": "menu",
                        "menu_screen": "main",
                        "options": ["continue"],
                        "player": {"potions": []},
                    }
                    self._states = [unknown, unknown, empty_menu, empty_menu, ready_menu, ready_menu]
                return {}

        fake = FakeReturnMenuClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "return_to_menu"}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=False,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=8,
            poll_delay=0,
        )

        self.assertEqual(fake.posts, [{"action": "return_to_menu"}])
        self.assertEqual(executed[0]["body"], {"action": "return_to_menu"})
        self.assertEqual(state["state_type"], "menu")
        self.assertEqual(state["menu_screen"], "main")
        self.assertEqual(state["options"], ["continue"])

    def test_action_body_accepts_return_to_menu(self):
        body, state = action_body_from_plan(
            FakeClient({"state_type": "monster", "player": {"potions": []}}),
            {"action": "return_to_menu"},
            auto_target=False,
        )

        self.assertEqual(body, {"action": "return_to_menu"})
        self.assertIsNone(state)

    def test_cards_parser_accepts_wait_budget_flags(self):
        args = build_parser().parse_args(["cards", "Strike", "--max-polls", "80"])

        self.assertEqual(args.command, "cards")
        self.assertEqual(args.max_polls, 80)

    def test_parser_fast_polling_defaults_are_explicit(self):
        self.assertEqual(DEFAULT_POLL_DELAY, 0.04)
        self.assertEqual(DEFAULT_INITIAL_POLL_DELAY, 0.015)
        self.assertEqual(DEFAULT_MAX_POLLS, 120)
        self.assertEqual(DEFAULT_MENU_MAX_POLLS, 120)
        self.assertEqual(DEFAULT_START_RUN_MAX_POLLS, 160)
        self.assertEqual(DELAYED_CARD_SETTLE_POLLS, 8)
        self.assertEqual(SELECTION_CARD_SETTLE_POLLS, 6)
        self.assertEqual(START_TURN_SETTLE_POLLS, 8)
        self.assertEqual(MAP_COMBAT_SETTLE_POLLS, 8)

        args = build_parser().parse_args(["act", '[{"end_turn":true}]'])
        self.assertEqual(args.poll_delay, DEFAULT_POLL_DELAY)
        self.assertEqual(args.max_polls, DEFAULT_MAX_POLLS)

        args = build_parser().parse_args(["cards", "Strike"])
        self.assertEqual(args.poll_delay, DEFAULT_POLL_DELAY)
        self.assertEqual(args.max_polls, DEFAULT_MAX_POLLS)

        args = build_parser().parse_args(["menu", "main_menu"])
        self.assertEqual(args.poll_delay, DEFAULT_POLL_DELAY)
        self.assertEqual(args.max_polls, DEFAULT_MENU_MAX_POLLS)

        args = build_parser().parse_args(["return-menu"])
        self.assertEqual(args.poll_delay, DEFAULT_POLL_DELAY)
        self.assertEqual(args.max_polls, DEFAULT_MENU_MAX_POLLS)

        args = build_parser().parse_args(["start-run"])
        self.assertEqual(args.poll_delay, DEFAULT_POLL_DELAY)
        self.assertEqual(args.max_polls, DEFAULT_START_RUN_MAX_POLLS)

    def test_parser_accepts_menu_start_and_profile_commands(self):
        args = build_parser().parse_args(["menu", "singleplayer", "--no-wait"])
        self.assertEqual(args.command, "menu")
        self.assertTrue(args.no_wait)

        args = build_parser().parse_args(["return-menu", "--no-wait"])
        self.assertEqual(args.command, "return-menu")
        self.assertTrue(args.no_wait)

        args = build_parser().parse_args(["map"])
        self.assertEqual(args.command, "map")

        args = build_parser().parse_args(["start-run", "--character", "first"])
        self.assertEqual(args.command, "start-run")
        self.assertEqual(args.character, "first")

        args = build_parser().parse_args(["start-run", "--auto-reveal-timeline"])
        self.assertTrue(args.auto_reveal_timeline)

        args = build_parser().parse_args(["timeline", "reveal", "--dry-run"])
        self.assertEqual(args.command, "timeline")
        self.assertEqual(args.action, "reveal")
        self.assertTrue(args.dry_run)

        args = build_parser().parse_args(["wiki", "perfected strike", "--item-type", "card"])
        self.assertEqual(args.command, "wiki")
        self.assertEqual(args.item_type, "card")

        args = build_parser().parse_args(["--multiplayer", "state", "--raw-format", "markdown"])
        self.assertTrue(args.multiplayer)
        self.assertEqual(args.raw_format, "markdown")

    def test_timeline_status_command_dispatches_get(self):
        fake = FakeTimelineCliClient()
        captured = StringIO()

        with patch.object(cli_module, "make_client", return_value=fake), redirect_stdout(captured):
            code = run(["--compact", "--no-log", "timeline", "status"])

        output = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(output["ok"])
        self.assertEqual(fake.gets, [{"path": "/api/v1/timeline", "params": None}])
        self.assertEqual(fake.posts, [])
        self.assertEqual(output["data"]["pending_epoch_ids"], ["epoch_a"])

    def test_timeline_reveal_command_dispatches_post_dry_run(self):
        fake = FakeTimelineCliClient()
        captured = StringIO()

        with patch.object(cli_module, "make_client", return_value=fake), redirect_stdout(captured):
            code = run(["--compact", "--no-log", "timeline", "reveal", "--dry-run"])

        output = json.loads(captured.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(output["ok"])
        self.assertEqual(fake.gets, [])
        self.assertEqual(
            fake.posts,
            [
                {
                    "path": "/api/v1/timeline",
                    "body": {"action": "reveal_pending", "dry_run": True},
                }
            ],
        )
        self.assertTrue(output["data"]["dry_run"])

    def test_timeline_reveal_command_returns_nonzero_for_slot_unlock_epoch(self):
        fake = FakeTimelineErrorCliClient()
        captured = StringIO()

        with patch.object(cli_module, "make_client", return_value=fake), redirect_stdout(captured):
            code = run(["--compact", "--no-log", "timeline", "reveal"])

        output = json.loads(captured.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(output["ok"])
        self.assertEqual(output["data"]["pending_slot_unlock_epoch_ids"], ["NEOW_EPOCH"])
        self.assertIn("ObtainedNoSlot", output["data"]["error"])
        self.assertEqual(
            fake.posts,
            [
                {
                    "path": "/api/v1/timeline",
                    "body": {"action": "reveal_pending", "dry_run": False},
                }
            ],
        )

    def test_act_map_data_returns_whole_map_graph(self):
        data = act_map_data(
            {
                "state_type": "map",
                "run": {"act": 3, "floor": 34},
                "player": {
                    "hp": 80,
                    "max_hp": 80,
                    "gold": 114,
                    "potions": [{"slot": 0, "name": "Skill Potion", "target_type": "Self"}],
                    "relics": [{"name": "Lantern"}, {"name": "Choices Paradox"}],
                },
                "map": {
                    "current_position": {"col": 3, "row": 0, "type": "Ancient"},
                    "visited": [{"col": 3, "row": 0, "type": "Ancient"}],
                    "next_options": [{"index": 1, "col": 6, "row": 1, "type": "Monster"}],
                    "boss": {"col": 3, "row": 14, "id": "QUEEN_BOSS", "name": "Queen"},
                    "bosses": [{"col": 3, "row": 14, "id": "QUEEN_BOSS", "name": "Queen"}],
                    "nodes": [
                        {
                            "col": 3,
                            "row": 0,
                            "type": "Ancient",
                            "children": [[6, 1]],
                            "extra": "preserved",
                        },
                        {"col": 6, "row": 1, "type": "Monster", "children": []},
                    ],
                },
            }
        )

        self.assertEqual(data["run"]["act"], 3)
        self.assertEqual(data["player"]["relics"], ["Lantern", "Choices Paradox"])
        self.assertEqual(data["next_options"][0]["index"], 1)
        self.assertEqual(data["nodes"][0]["extra"], "preserved")
        self.assertEqual(data["boss"]["name"], "Queen")

    def test_act_map_data_requires_map_nodes(self):
        with self.assertRaisesRegex(RuntimeError, "Whole act map is not available"):
            act_map_data({"state_type": "monster", "player": {}, "battle": {}})

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

    def test_hand_select_state_is_summarized_and_classified(self):
        state = FakeDelayedHandSelectClient().state()

        summary = summarize_state(state)
        point = decision_point(state)

        self.assertEqual(summary["state_type"], "hand_select")
        self.assertEqual(summary["hand_select"]["prompt"], "Choose a card to put on top of your Draw Pile.")
        self.assertEqual(
            [card["name"] for card in summary["hand_select"]["cards"]],
            ["Strike", "Flame Barrier+"],
        )
        self.assertFalse(summary["hand_select"]["can_confirm"])
        self.assertEqual(point["kind"], "hand_select")
        self.assertEqual(point["option_count"], 2)
        self.assertFalse(point["can_confirm"])

    def test_combat_summary_includes_low_token_tactical_block(self):
        state = {
            "state_type": "boss",
            "player": {
                "hp": 41,
                "max_hp": 80,
                "block": 7,
                "energy": 3,
                "potions": [],
                "status": [
                    {"id": "WEAK", "name": "Weak", "amount": 2, "type": "Debuff"},
                    {
                        "id": "CHAINS_OF_BINDING",
                        "name": "Chains of Binding",
                        "amount": 1,
                        "description": "Only one Bound card can be played.",
                    },
                ],
                "hand": [
                    {
                        "index": 0,
                        "name": "Bound Strike",
                        "cost": "1",
                        "description": "Bound. Deal 6 damage.",
                        "keywords": [
                            {
                                "name": "Bound",
                                "description": "Only one Bound card can be played.",
                            }
                        ],
                    },
                    {"index": 1, "name": "Defend", "cost": "1", "description": "Gain 5 Block."},
                ],
            },
            "battle": {
                "round": 4,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [
                    {
                        "entity_id": "QUEEN_0",
                        "name": "Queen",
                        "hp": 180,
                        "intents": [
                            {
                                "type": "Attack",
                                "label": "6",
                                "title": "Attack",
                                "description": "Deals 6 damage 2 times.",
                            },
                            {
                                "type": "Debuff",
                                "title": "Debuff",
                                "description": "Applies Frail.",
                            },
                        ],
                    },
                    {
                        "entity_id": "MINION_0",
                        "name": "Minion",
                        "hp": 30,
                        "intents": [
                            {
                                "type": "Attack",
                                "label": "5",
                                "title": "Attack",
                                "description": "Deals 5 damage.",
                            }
                        ],
                    },
                    {
                        "entity_id": "SENTRY_0",
                        "name": "Sentry",
                        "hp": 20,
                        "intents": [{"type": "Sleep", "title": "Sleep"}],
                    },
                ],
            },
        }

        tactical = summarize_state(state)["combat"]["tactical"]

        self.assertEqual(tactical["incoming_damage"], 17)
        self.assertEqual(
            tactical["enemy_attack_damage"],
            [
                {"id": "QUEEN_0", "name": "Queen", "damage": 12},
                {"id": "MINION_0", "name": "Minion", "damage": 5},
                {"id": "SENTRY_0", "name": "Sentry", "damage": 0},
            ],
        )
        self.assertEqual(tactical["player_status"], ["Weak 2", "Chains of Binding 1"])
        self.assertEqual(
            tactical["constraints"],
            [
                "incoming>block:17>7",
                "player_constraint:Chains of Binding 1",
                "bound_cards:0:Bound Strike",
            ],
        )

    def test_combat_tactical_summary_handles_damage_variants_and_unknowns(self):
        state = {
            "state_type": "monster",
            "player": {
                "hp": 30,
                "block": 0,
                "energy": 3,
                "potions": [],
                "status": [],
                "hand": [],
            },
            "battle": {
                "round": 2,
                "turn": "player",
                "is_play_phase": True,
                "enemies": [
                    {
                        "entity_id": "MULTI_0",
                        "name": "Multi",
                        "intents": [{"type": "Attack", "label": "6×3", "title": "Attack"}],
                    },
                    {
                        "entity_id": "TEXT_0",
                        "name": "Text",
                        "intents": [
                            {
                                "type": "Intent",
                                "title": "Aggressive",
                                "description": "This enemy intends to Attack for 14 damage.",
                            }
                        ],
                    },
                    {
                        "entity_id": "MYSTERY_0",
                        "name": "Mystery",
                        "intents": [{"type": "Attack", "label": "?", "title": "Attack"}],
                    },
                    {
                        "entity_id": "BUFF_0",
                        "name": "Buff",
                        "intents": [
                            {
                                "type": "Buff",
                                "title": "Barrier",
                                "description": "This enemy intends to prevent damage.",
                            }
                        ],
                    },
                ],
            },
        }

        tactical = summarize_state(state)["combat"]["tactical"]

        self.assertEqual(tactical["incoming_damage"], 32)
        self.assertEqual(
            tactical["enemy_attack_damage"],
            [
                {"id": "MULTI_0", "name": "Multi", "damage": 18},
                {"id": "TEXT_0", "name": "Text", "damage": 14},
                {"id": "MYSTERY_0", "name": "Mystery", "damage": 0},
                {"id": "BUFF_0", "name": "Buff", "damage": 0},
            ],
        )
        self.assertEqual(
            tactical["constraints"],
            ["incoming>block:32>0", "unknown_attack_damage:MYSTERY_0"],
        )

    def test_non_combat_summary_does_not_add_tactical_block(self):
        summary = summarize_state(
            {
                "state_type": "map",
                "player": {"potions": []},
                "map": {"next_options": []},
            }
        )

        self.assertNotIn("combat", summary)

    def test_hand_selection_alias_uses_current_mod_compatible_http_action(self):
        body, _ = action_body_from_plan(
            FakeDelayedHandSelectClient(),
            {"action": "hand_select", "index": 1},
            auto_target=True,
        )

        self.assertEqual(body, {"action": "combat_select_card", "card_index": 1})

        body, _ = action_body_from_plan(
            FakeDelayedHandSelectClient(),
            {"action": "combat_select_card", "card_index": 1},
            auto_target=True,
        )

        self.assertEqual(body, {"action": "combat_select_card", "card_index": 1})

    def test_hand_selection_alias_waits_for_state_change(self):
        fake = FakeDelayedHandSelectClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"hand_select": 1}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=12,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "combat_select_card", "card_index": 1})
        self.assertEqual(state["state_type"], "monster")
        self.assertEqual([card["name"] for card in state["player"]["hand"]], ["Strike"])

    def test_hand_selection_confirm_waits_for_late_reward_transition(self):
        fake = FakeConfirmHandSelectionDelayedRewardClient()
        stats = RunStats()

        state, executed = execute_actions(
            fake,
            [{"action": "confirm_hand_selection"}],
            logger=quiet_logger(),
            stats=stats,
            auto_target=True,
            drain_after=False,
            wait_after_end_turn=True,
            max_polls=14,
            poll_delay=0,
        )

        self.assertEqual(executed[0]["body"], {"action": "combat_confirm_selection"})
        self.assertEqual(state["state_type"], "rewards")

    def test_choose_character_option_matches_unlocked_character(self):
        state = FakeMenuStartRunClient()._character_select(confirm_enabled=False)

        self.assertEqual(choose_character_option(state, "Ironclad"), "ironclad")
        self.assertEqual(choose_character_option(state, "first"), "ironclad")
        with self.assertRaisesRegex(RuntimeError, "not available"):
            choose_character_option(state, "silent")

    def test_start_run_walks_main_menu_to_run_state(self):
        fake = FakeMenuStartRunClient()
        stats = RunStats()

        state, executed = start_run(
            fake,
            logger=quiet_logger(),
            stats=stats,
            mode="standard",
            character="ironclad",
            seed=None,
            max_steps=8,
            max_polls=3,
            poll_delay=0,
        )

        self.assertEqual(state["state_type"], "map")
        self.assertEqual(
            [step["body"]["option"] for step in executed],
            ["singleplayer", "standard", "ironclad", "confirm"],
        )
        self.assertEqual(stats.actions, 4)

    def test_start_run_explains_manual_timeline_reveal_blocker(self):
        fake = FakeBlockedTimelineStartRunClient()

        with self.assertRaisesRegex(
            RuntimeError,
            "manual Timeline reveal.*pending_epoch_ids=.*epoch_a.*epoch_b",
        ):
            start_run(
                fake,
                logger=quiet_logger(),
                stats=RunStats(),
                mode="standard",
                character="ironclad",
                seed=None,
                max_steps=8,
                max_polls=3,
                poll_delay=0,
            )

        self.assertEqual(fake.posts, [])

    def test_start_run_explains_timeline_blocker_after_game_over_return(self):
        fake = FakeGameOverToBlockedTimelineStartRunClient()

        with self.assertRaisesRegex(
            RuntimeError,
            "manual Timeline reveal.*pending_epoch_ids=.*epoch_a.*epoch_b",
        ):
            start_run(
                fake,
                logger=quiet_logger(),
                stats=RunStats(),
                mode="standard",
                character="ironclad",
                seed=None,
                max_steps=8,
                max_polls=3,
                poll_delay=0,
            )

        self.assertEqual(fake.posts, [{"action": "menu_select", "option": "main_menu"}])

    def test_start_run_refuses_slot_unlock_timeline_blocker_before_auto_reveal(self):
        fake = FakeSlotUnlockTimelineStartRunClient()

        with self.assertRaisesRegex(
            RuntimeError,
            "slot-unlock epochs.*pending_slot_unlock_epoch_ids=.*NEOW_EPOCH",
        ):
            start_run(
                fake,
                logger=quiet_logger(),
                stats=RunStats(),
                mode="standard",
                character="ironclad",
                seed=None,
                max_steps=8,
                max_polls=3,
                poll_delay=0,
                auto_reveal_timeline=True,
            )

        self.assertEqual(fake.timeline_gets, [{"path": "/api/v1/timeline", "params": None}])
        self.assertEqual(fake.timeline_posts, [])
        self.assertEqual(fake.posts, [])

    def test_start_run_can_auto_reveal_timeline_blocker(self):
        fake = FakeAutoRevealTimelineStartRunClient()
        stats = RunStats()

        state, executed = start_run(
            fake,
            logger=quiet_logger(),
            stats=stats,
            mode="standard",
            character="ironclad",
            seed=None,
            max_steps=10,
            max_polls=3,
            poll_delay=0,
            auto_reveal_timeline=True,
        )

        self.assertEqual(state["state_type"], "map")
        self.assertEqual(
            fake.timeline_posts,
            [
                {
                    "path": "/api/v1/timeline",
                    "body": {"action": "reveal_pending", "dry_run": False},
                }
            ],
        )
        self.assertEqual(executed[0]["planned"], {"action": "timeline_reveal_pending"})
        self.assertEqual(
            [step["body"]["option"] for step in executed[1:]],
            ["singleplayer", "standard", "ironclad", "confirm"],
        )

    def test_start_run_auto_reveal_fails_if_blocker_remains(self):
        fake = FakeStuckTimelineStartRunClient()

        with self.assertRaisesRegex(RuntimeError, "auto reveal did not clear"):
            start_run(
                fake,
                logger=quiet_logger(),
                stats=RunStats(),
                mode="standard",
                character="ironclad",
                seed=None,
                max_steps=10,
                max_polls=3,
                poll_delay=0,
                auto_reveal_timeline=True,
            )

        self.assertEqual(
            fake.timeline_posts,
            [
                {
                    "path": "/api/v1/timeline",
                    "body": {"action": "reveal_pending", "dry_run": False},
                }
            ],
        )

    def test_start_run_refuses_active_run_state(self):
        with self.assertRaisesRegex(RuntimeError, "Cannot start a run"):
            start_run(
                FakeActiveRunClient(),
                logger=quiet_logger(),
                stats=RunStats(),
                mode="standard",
                character="ironclad",
                seed=None,
                max_steps=8,
                max_polls=3,
                poll_delay=0,
            )

    def test_menu_option_can_return_valid_no_change_status(self):
        fake = FakeNoChangeMenuClient()

        state, executed = execute_menu_option(
            fake,
            "advance",
            logger=quiet_logger(),
            stats=RunStats(),
            max_polls=2,
            poll_delay=0,
        )

        self.assertEqual(state["menu_screen"], "timeline")
        self.assertEqual(executed["result"]["done"], True)

    def test_menu_option_require_change_preserves_startup_strictness(self):
        fake = FakeNoChangeMenuClient()

        with self.assertRaisesRegex(RuntimeError, "Timed out waiting"):
            execute_menu_option(
                fake,
                "advance",
                logger=quiet_logger(),
                stats=RunStats(),
                require_change=True,
                max_polls=2,
                poll_delay=0,
            )

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

    def test_crystal_sphere_state_is_summarized_and_classified(self):
        state = {
            "state_type": "crystal_sphere",
            "player": {"potions": []},
            "crystal_sphere": {
                "grid_width": 3,
                "grid_height": 2,
                "tool": "small",
                "can_use_big_tool": True,
                "can_use_small_tool": True,
                "can_proceed": False,
                "clickable_cells": [{"x": 1, "y": 0}],
                "revealed_items": [{"item_type": "GoodThing", "x": 0, "y": 0, "is_good": True}],
            },
        }

        summary = summarize_state(state)
        point = decision_point(state)
        body, reason = next_trivial_action(state)

        self.assertEqual(summary["crystal_sphere"]["tool"], "small")
        self.assertEqual(summary["crystal_sphere"]["clickable_cells"], [{"x": 1, "y": 0}])
        self.assertEqual(point["kind"], "crystal_sphere")
        self.assertEqual(point["clickable_count"], 1)
        self.assertIsNone(body)
        self.assertIsNone(reason)

    def test_completed_crystal_sphere_is_drainable(self):
        body, reason = next_trivial_action(
            {
                "state_type": "crystal_sphere",
                "player": {"potions": []},
                "crystal_sphere": {"can_proceed": True, "clickable_cells": []},
            }
        )

        self.assertEqual(body, {"action": "crystal_sphere_proceed"})
        self.assertEqual(reason, "leave completed crystal sphere")

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
                    "wait_id": "wait-0001",
                    "status_code": 200,
                    "elapsed_ms": 12.4,
                },
                {
                    "seq": 3,
                    "ts": "2026-06-22T10:00:00.120-07:00",
                    "kind": "wait",
                    "wait_id": "wait-0001",
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
                {
                    "seq": 6,
                    "ts": "2026-06-22T10:00:00.250-07:00",
                    "kind": "stdout",
                    "bytes": 123,
                    "lines": 3,
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            summary = analyze_log(path)

        self.assertEqual(summary["event_kinds"]["action_result"], 1)
        self.assertEqual(summary["waits"]["reasons"], {"ready_state": 1})
        self.assertEqual(summary["waits"]["elapsed_ms"]["total_ms"], 87.5)
        self.assertEqual(
            summary["waits"]["http_by_wait_id"],
            {"wait-0001": {"count": 1, "elapsed_ms": 12.4}},
        )
        self.assertEqual(summary["timing_breakdown"]["wait_http_overlap_ms"], 12.4)
        self.assertEqual(summary["timing_breakdown"]["wait_non_http_ms"], 75.1)
        self.assertEqual(summary["timing_breakdown"]["local_overhead_ms"], 162.5)
        self.assertEqual(summary["stdout"], {"writes": 1, "bytes": 123, "lines": 3})
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

    def test_expand_log_path_args_dedupes_relative_and_repo_root_globs(self):
        path = output_log_path("logs/sts2-fast/test-dedupe.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        try:
            self.assertEqual(
                expand_log_path_args(["logs/sts2-fast/test-dedupe*.jsonl"]),
                [path],
            )
        finally:
            path.unlink(missing_ok=True)

    def test_expand_log_path_args_rejects_empty_globs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "matched no files"):
                expand_log_path_args([str(Path(tmp) / "*.jsonl")])

    def test_output_log_path_resolves_relative_paths_from_repo_root(self):
        path = output_log_path("logs/sts2-fast/test.jsonl")

        self.assertTrue(path.is_absolute())
        self.assertTrue(str(path).endswith("STS2MCP/logs/sts2-fast/test.jsonl"))

    def test_emit_result_logs_stdout_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "stdout.jsonl"
            logger = JsonlLogger(log_path)
            stats = RunStats()
            state = {
                "state_type": "menu",
                "player": {"hp": 80, "max_hp": 80, "potions": []},
            }

            captured = StringIO()
            with redirect_stdout(captured):
                emit_result(
                    ok=True,
                    stats=stats,
                    log_path=log_path,
                    logger=logger,
                    state=state,
                    indent=None,
                )

            payload = captured.getvalue()
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertGreater(stats.stdout_bytes, 0)
        self.assertEqual(stats.stdout_writes, 1)
        self.assertEqual(stats.stdout_bytes, len(payload.encode("utf-8")))
        self.assertEqual(rows[0]["kind"], "stdout")
        self.assertEqual(rows[0]["bytes"], stats.stdout_bytes)


if __name__ == "__main__":
    unittest.main()
