"""Fast local CLI for driving Slay the Spire 2 with fewer agent round trips.

The game mod remains the HTTP adapter; this CLI is the sole agent-control
surface for this fork. It batches deterministic work, auto-drains no-decision
screens, polls transitions, and collects timing logs.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


COMBAT_STATES = {"monster", "elite", "boss"}
DEFAULT_BASE_URL = "http://127.0.0.1:15526"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return _repo_root() / "logs" / "sts2-fast" / f"{stamp}-{os.getpid()}.jsonl"


def _json_dumps(data: Any, *, indent: int | None = None) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent)


def _norm(value: str) -> str:
    return " ".join(value.casefold().replace("+", " +").split())


def _base_card_norm(value: str) -> str:
    return _norm(value).replace(" +", "")


class JsonlLogger:
    def __init__(self, path: Path | None):
        self.path = path
        self.seq = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **data: Any) -> None:
        if self.path is None:
            return
        self.seq += 1
        event = {
            "seq": self.seq,
            "ts": _now_iso(),
            "kind": kind,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(_json_dumps(event) + "\n")


@dataclass
class RunStats:
    http_calls: int = 0
    get_calls: int = 0
    post_calls: int = 0
    actions: int = 0
    drains: int = 0
    http_elapsed_ms: float = 0.0
    started_at: float = field(default_factory=time.perf_counter)

    def as_dict(self) -> dict[str, Any]:
        elapsed_ms = (time.perf_counter() - self.started_at) * 1000.0
        return {
            "elapsed_ms": round(elapsed_ms, 1),
            "http_elapsed_ms": round(self.http_elapsed_ms, 1),
            "http_calls": self.http_calls,
            "get_calls": self.get_calls,
            "post_calls": self.post_calls,
            "actions": self.actions,
            "drain_actions": self.drains,
        }


class STS2Client:
    def __init__(
        self,
        *,
        base_url: str,
        logger: JsonlLogger,
        stats: RunStats,
        timeout: float,
        trust_env: bool,
    ):
        self.base_url = base_url.rstrip("/")
        self.logger = logger
        self.stats = stats
        self.http = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=2.0),
            trust_env=trust_env,
        )

    def close(self) -> None:
        self.http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        started = time.perf_counter()
        started_ts = _now_iso()
        status_code: int | None = None
        response_text = ""
        error: str | None = None
        try:
            response = self.http.request(method, url, params=params, json=json_body)
            status_code = response.status_code
            response_text = response.text
            response.raise_for_status()
            if not response_text:
                return {}
            return response.json()
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.stats.http_calls += 1
            self.stats.http_elapsed_ms += elapsed_ms
            if method.upper() == "GET":
                self.stats.get_calls += 1
            elif method.upper() == "POST":
                self.stats.post_calls += 1
            self.logger.write(
                "http",
                method=method.upper(),
                path=path,
                started_ts=started_ts,
                params=params,
                action=json_body.get("action") if json_body else None,
                status_code=status_code,
                elapsed_ms=round(elapsed_ms, 1),
                response_bytes=len(response_text.encode("utf-8")),
                error=error,
            )

    def state(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v1/singleplayer", params={"format": "json"})
        if not isinstance(data, dict):
            raise RuntimeError("Expected JSON object from game state")
        return data

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", "/api/v1/singleplayer", json_body=body)
        result = data if isinstance(data, dict) else {}
        validate_post_response(result, body)
        return result


def validate_post_response(result: dict[str, Any], body: dict[str, Any]) -> None:
    status = result.get("status")
    error = result.get("error")
    ok = result.get("ok")
    if status == "error" or error or ok is False:
        action = body.get("action")
        detail = error or result.get("detail") or result.get("message") or "unknown error"
        raise RuntimeError(f"Action {action!r} failed: {detail}")


def is_combat_state(state: dict[str, Any]) -> bool:
    return state.get("state_type") in COMBAT_STATES


def enemies(state: dict[str, Any]) -> list[dict[str, Any]]:
    battle = state.get("battle") or {}
    return [e for e in battle.get("enemies") or [] if isinstance(e, dict)]


def hand(state: dict[str, Any]) -> list[dict[str, Any]]:
    player = state.get("player") or {}
    return [c for c in player.get("hand") or [] if isinstance(c, dict)]


def summarize_state(state: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    player = state.get("player") or {}
    summary: dict[str, Any] = {
        "state_type": state.get("state_type"),
        "run": state.get("run"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
            "gold": player.get("gold"),
            "potions": [
                {
                    "slot": p.get("slot"),
                    "name": p.get("name"),
                    "target_type": p.get("target_type"),
                }
                for p in player.get("potions") or []
                if isinstance(p, dict)
            ],
        },
    }

    if verbose:
        summary["player"]["relics"] = [
            r.get("name")
            for r in player.get("relics") or []
            if isinstance(r, dict)
        ]

    state_type = state.get("state_type")
    if state_type in COMBAT_STATES:
        battle = state.get("battle") or {}
        summary["combat"] = {
            "round": battle.get("round"),
            "turn": battle.get("turn"),
            "is_play_phase": battle.get("is_play_phase"),
            "hand": [
                {
                    "index": c.get("index"),
                    "name": c.get("name"),
                    "cost": c.get("cost"),
                    "description": c.get("description"),
                }
                for c in hand(state)
            ],
            "enemies": [
                {
                    "entity_id": e.get("entity_id"),
                    "name": e.get("name"),
                    "hp": e.get("hp"),
                    "max_hp": e.get("max_hp"),
                    "block": e.get("block"),
                    "status": [
                        {
                            "name": s.get("name"),
                            "amount": s.get("amount"),
                            "description": s.get("description"),
                        }
                        for s in e.get("status") or []
                        if isinstance(s, dict)
                    ],
                    "intents": e.get("intents") or [],
                }
                for e in enemies(state)
            ],
        }
    elif state_type == "map":
        m = state.get("map") or {}
        summary["map"] = {
            "current_position": m.get("current_position"),
            "next_options": m.get("next_options"),
            "boss": m.get("boss"),
        }
    elif state_type == "rewards":
        summary["rewards"] = (state.get("rewards") or {}).get("items") or []
    elif state_type == "card_reward":
        summary["card_reward"] = [
            {
                "index": c.get("index"),
                "name": c.get("name"),
                "cost": c.get("cost"),
                "type": c.get("type"),
                "rarity": c.get("rarity"),
                "description": c.get("description"),
            }
            for c in (state.get("card_reward") or {}).get("cards") or []
            if isinstance(c, dict)
        ]
    elif state_type == "event":
        event = state.get("event") or {}
        summary["event"] = {
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name"),
            "in_dialogue": event.get("in_dialogue"),
            "options": event.get("options") or [],
        }
    elif state_type == "rest_site":
        summary["rest_site"] = state.get("rest_site") or {}
    elif state_type == "treasure":
        summary["treasure"] = state.get("treasure") or {}
    elif state_type == "card_select":
        card_select = state.get("card_select") or {}
        summary["card_select"] = {
            "prompt": card_select.get("prompt"),
            "can_confirm": card_select.get("can_confirm"),
            "can_cancel": card_select.get("can_cancel"),
            "cards": [
                {
                    "index": c.get("index"),
                    "name": c.get("name"),
                    "cost": c.get("cost"),
                    "description": c.get("description"),
                }
                for c in card_select.get("cards") or []
                if isinstance(c, dict)
            ],
        }
    elif state_type == "bundle_select":
        summary["bundle_select"] = state.get("bundle_select") or {}
    elif state_type == "relic_select":
        summary["relic_select"] = state.get("relic_select") or {}
    elif state_type in {"shop", "fake_merchant"}:
        summary[state_type] = state.get("shop") or state.get("fake_merchant") or {}

    return summary


def state_digest(state: dict[str, Any]) -> dict[str, Any]:
    player = state.get("player") or {}
    digest: dict[str, Any] = {
        "state_type": state.get("state_type"),
        "run": state.get("run"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "block": player.get("block"),
            "energy": player.get("energy"),
            "gold": player.get("gold"),
            "potions": [
                {
                    "slot": p.get("slot"),
                    "name": p.get("name"),
                }
                for p in player.get("potions") or []
                if isinstance(p, dict)
            ],
        },
    }
    state_type = state.get("state_type")
    if state_type in COMBAT_STATES:
        battle = state.get("battle") or {}
        digest["combat"] = {
            "round": battle.get("round"),
            "turn": battle.get("turn"),
            "is_play_phase": battle.get("is_play_phase"),
            "hand": [c.get("name") for c in hand(state)],
            "enemies": [
                {
                    "id": e.get("entity_id"),
                    "name": e.get("name"),
                    "hp": e.get("hp"),
                    "block": e.get("block"),
                    "intents": [
                        {
                            "type": i.get("type"),
                            "label": i.get("label"),
                        }
                        for i in e.get("intents") or []
                        if isinstance(i, dict)
                    ],
                    "status": [
                        {
                            "name": s.get("name"),
                            "amount": s.get("amount"),
                        }
                        for s in e.get("status") or []
                        if isinstance(s, dict)
                    ],
                }
                for e in enemies(state)
            ],
        }
    elif state_type == "rewards":
        digest["rewards"] = [
            {
                "index": item.get("index"),
                "type": item.get("type"),
                "description": item.get("description"),
            }
            for item in (state.get("rewards") or {}).get("items") or []
            if isinstance(item, dict)
        ]
    elif state_type == "card_reward":
        digest["card_reward"] = [
            {
                "index": card.get("index"),
                "name": card.get("name"),
                "type": card.get("type"),
                "rarity": card.get("rarity"),
            }
            for card in (state.get("card_reward") or {}).get("cards") or []
            if isinstance(card, dict)
        ]
    elif state_type == "map":
        m = state.get("map") or {}
        digest["map"] = {
            "current_position": m.get("current_position"),
            "next_options": [
                {
                    "index": option.get("index"),
                    "type": option.get("type"),
                    "col": option.get("col"),
                    "row": option.get("row"),
                }
                for option in m.get("next_options") or []
                if isinstance(option, dict)
            ],
            "boss": m.get("boss"),
        }
    elif state_type == "event":
        event = state.get("event") or {}
        digest["event"] = {
            "event_id": event.get("event_id"),
            "event_name": event.get("event_name"),
            "in_dialogue": event.get("in_dialogue"),
            "options": [
                {
                    "index": option.get("index"),
                    "title": option.get("title"),
                    "is_locked": option.get("is_locked"),
                    "is_proceed": option.get("is_proceed"),
                }
                for option in event.get("options") or []
                if isinstance(option, dict)
            ],
        }
    elif state_type == "rest_site":
        digest["rest_site"] = state.get("rest_site") or {}
    elif state_type == "treasure":
        digest["treasure"] = state.get("treasure") or {}
    elif state_type == "card_select":
        card_select = state.get("card_select") or {}
        digest["card_select"] = {
            "prompt": card_select.get("prompt"),
            "can_confirm": card_select.get("can_confirm"),
            "can_cancel": card_select.get("can_cancel"),
            "cards": [
                {
                    "index": card.get("index"),
                    "name": card.get("name"),
                }
                for card in card_select.get("cards") or []
                if isinstance(card, dict)
            ],
        }
    return digest


def _number_delta(before: Any, after: Any) -> Any:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            return after - before
    return None


def _decision_point_key(point: dict[str, Any]) -> str:
    return json.dumps(point, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _counter_delta(before: list[Any], after: list[Any]) -> dict[str, Any]:
    before_counts: dict[Any, int] = {}
    after_counts: dict[Any, int] = {}
    for item in before:
        before_counts[item] = before_counts.get(item, 0) + 1
    for item in after:
        after_counts[item] = after_counts.get(item, 0) + 1
    keys = sorted(set(before_counts) | set(after_counts), key=str)
    return {
        "added": [
            item
            for item in keys
            for _ in range(max(0, after_counts.get(item, 0) - before_counts.get(item, 0)))
        ],
        "removed": [
            item
            for item in keys
            for _ in range(max(0, before_counts.get(item, 0) - after_counts.get(item, 0)))
        ],
    }


def state_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_digest = state_digest(before)
    after_digest = state_digest(after)
    delta: dict[str, Any] = {
        "state_type": {
            "before": before_digest.get("state_type"),
            "after": after_digest.get("state_type"),
        }
    }
    before_player = before_digest.get("player") or {}
    after_player = after_digest.get("player") or {}
    player_delta = {
        key: _number_delta(before_player.get(key), after_player.get(key))
        for key in ["hp", "block", "energy", "gold"]
    }
    player_delta = {key: value for key, value in player_delta.items() if value not in (None, 0)}
    if player_delta:
        delta["player"] = player_delta

    before_combat = before_digest.get("combat") or {}
    after_combat = after_digest.get("combat") or {}
    if before_combat or after_combat:
        combat_delta: dict[str, Any] = {}
        hand_change = _counter_delta(
            before_combat.get("hand") or [],
            after_combat.get("hand") or [],
        )
        if hand_change["added"] or hand_change["removed"]:
            combat_delta["hand"] = hand_change

        before_enemies = {
            enemy.get("id"): enemy
            for enemy in before_combat.get("enemies") or []
            if enemy.get("id")
        }
        after_enemies = {
            enemy.get("id"): enemy
            for enemy in after_combat.get("enemies") or []
            if enemy.get("id")
        }
        enemy_deltas = []
        for enemy_id in sorted(set(before_enemies) | set(after_enemies)):
            before_enemy = before_enemies.get(enemy_id) or {}
            after_enemy = after_enemies.get(enemy_id) or {}
            item = {
                "id": enemy_id,
                "name": after_enemy.get("name") or before_enemy.get("name"),
            }
            hp_delta = _number_delta(before_enemy.get("hp"), after_enemy.get("hp"))
            block_delta = _number_delta(before_enemy.get("block"), after_enemy.get("block"))
            if before_enemy and not after_enemy:
                hp_delta = -(before_enemy.get("hp") or 0)
                block_delta = -(before_enemy.get("block") or 0)
            if hp_delta not in (None, 0):
                item["hp_delta"] = hp_delta
            if block_delta not in (None, 0):
                item["block_delta"] = block_delta
            if before_enemy and not after_enemy:
                item["removed"] = True
            if item.keys() != {"id", "name"}:
                enemy_deltas.append(item)
        if enemy_deltas:
            combat_delta["enemies"] = enemy_deltas
        if combat_delta:
            delta["combat"] = combat_delta
    return delta


def decision_point(state: dict[str, Any]) -> dict[str, Any]:
    state_type = state.get("state_type")
    point: dict[str, Any] = {"state_type": state_type}
    trivial_action, trivial_reason = next_trivial_action(state)
    if trivial_action is not None:
        point["kind"] = "trivial"
        point["trivial_reason"] = trivial_reason
        return point

    if state_type in COMBAT_STATES:
        battle = state.get("battle") or {}
        point["kind"] = "combat_turn" if battle.get("turn") == "player" else "combat_wait"
        point["round"] = battle.get("round")
        point["turn"] = battle.get("turn")
        point["enemy_count"] = len(enemies(state))
    elif state_type == "map":
        options = (state.get("map") or {}).get("next_options") or []
        point["kind"] = "map_choice"
        point["option_count"] = len(options)
    elif state_type == "card_reward":
        cards = (state.get("card_reward") or {}).get("cards") or []
        point["kind"] = "card_reward"
        point["option_count"] = len(cards)
    elif state_type == "rewards":
        point["kind"] = "reward_decision"
    elif state_type == "event":
        options = (state.get("event") or {}).get("options") or []
        point["kind"] = "event_choice"
        point["option_count"] = len(options)
    elif state_type == "rest_site":
        options = (state.get("rest_site") or {}).get("options") or []
        point["kind"] = "rest_choice" if options else "completed_rest"
        point["option_count"] = len(options)
    else:
        point["kind"] = state_type or "unknown"
    return point


def has_empty_potion_slot(state: dict[str, Any]) -> bool:
    player = state.get("player") or {}
    potions = player.get("potions") or []
    max_slots = player.get("max_potion_slots")
    if isinstance(max_slots, int):
        return len(potions) < max_slots
    return len(potions) < 2


def is_transient_state(state: dict[str, Any]) -> bool:
    if state.get("state_type") == "map":
        m = state.get("map") or {}
        return not m.get("next_options") and m.get("current_position") is None
    if state.get("state_type") == "treasure":
        treasure = state.get("treasure") or {}
        return bool(treasure.get("message")) and not treasure.get("relics")
    if state.get("state_type") == "event":
        event = state.get("event") or {}
        return event.get("in_dialogue") is False and not event.get("options")
    if is_combat_state(state):
        battle = state.get("battle") or {}
        return battle.get("turn") != "player" or battle.get("is_play_phase") is False
    return False


def is_ready_combat_decision(state: dict[str, Any]) -> bool:
    if not is_combat_state(state):
        return True
    battle = state.get("battle") or {}
    if battle.get("turn") != "player" or battle.get("is_play_phase") is not True:
        return False
    return bool(hand(state))


def next_trivial_action(state: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    state_type = state.get("state_type")

    if state_type == "rewards":
        rewards = state.get("rewards") or {}
        items = [i for i in rewards.get("items") or [] if isinstance(i, dict)]
        for item in sorted(items, key=lambda i: i.get("index", -1), reverse=True):
            item_type = item.get("type")
            index = item.get("index")
            if item_type == "gold":
                return {"action": "claim_reward", "index": index}, "claim gold"
            if item_type == "potion" and has_empty_potion_slot(state):
                return {"action": "claim_reward", "index": index}, "claim potion into empty slot"
            if item_type == "relic":
                return {"action": "claim_reward", "index": index}, "claim relic"
        if not items and rewards.get("can_proceed"):
            return {"action": "proceed"}, "leave empty rewards screen"
        return None, None

    if state_type == "treasure":
        treasure = state.get("treasure") or {}
        relics = [r for r in treasure.get("relics") or [] if isinstance(r, dict)]
        if len(relics) == 1:
            return {"action": "claim_treasure_relic", "index": relics[0].get("index", 0)}, "claim only treasure relic"
        if treasure.get("can_proceed") and not relics:
            return {"action": "proceed"}, "leave treasure"
        return None, None

    if state_type == "rest_site":
        rest_site = state.get("rest_site") or {}
        if rest_site.get("can_proceed") and not rest_site.get("options"):
            return {"action": "proceed"}, "leave completed rest site"
        return None, None

    if state_type == "event":
        event = state.get("event") or {}
        if event.get("in_dialogue") and not event.get("options"):
            return {"action": "advance_dialogue"}, "advance event dialogue"
        options = [o for o in event.get("options") or [] if isinstance(o, dict)]
        enabled = [o for o in options if not o.get("is_locked")]
        proceed = [
            o for o in enabled
            if o.get("is_proceed") or str(o.get("title", "")).casefold() == "proceed"
        ]
        if len(enabled) == 1 and len(proceed) == 1:
            return {"action": "choose_event_option", "index": proceed[0].get("index", 0)}, "choose event proceed"
        return None, None

    if state_type == "map":
        options = [o for o in (state.get("map") or {}).get("next_options") or [] if isinstance(o, dict)]
        if len(options) == 1:
            return {"action": "choose_map_node", "index": options[0].get("index", 0)}, "choose only map node"
        return None, None

    if state_type in {"shop", "fake_merchant"}:
        shop = state.get("shop") or state.get("fake_merchant") or {}
        if shop.get("can_proceed") and not shop.get("items"):
            return {"action": "proceed"}, f"leave empty {state_type}"
        return None, None

    return None, None


def drain_trivial(
    client: Any,
    *,
    logger: JsonlLogger,
    stats: RunStats,
    max_steps: int = 30,
    poll_delay: float = 0.12,
    initial_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    drained: list[dict[str, Any]] = []
    transient_polls = 0
    state = initial_state if initial_state is not None else client.state()

    for _ in range(max_steps):
        body, reason = next_trivial_action(state)
        if body is None:
            if is_transient_state(state) and transient_polls < 20:
                transient_polls += 1
                time.sleep(poll_delay)
                state = client.state()
                continue
            return state, drained

        transient_polls = 0
        stats.actions += 1
        stats.drains += 1
        logger.write(
            "drain_action",
            state_type=state.get("state_type"),
            reason=reason,
            body=body,
            before=state_digest(state),
            decision_point=decision_point(state),
        )
        state_before = deepcopy(state)
        client.post(body)
        drained.append({"reason": reason, "body": body})
        time.sleep(poll_delay)
        if body.get("action") == "choose_map_node":
            state = wait_for_map_node_transition(
                client,
                state_before,
                logger=logger,
                max_polls=60,
                poll_delay=poll_delay,
            )
        else:
            state = wait_for_state_change(
                client,
                state_before,
                logger=logger,
                reason="drain",
                max_polls=10,
                poll_delay=poll_delay,
            )
        logger.write(
            "drain_result",
            reason=reason,
            body=body,
            before=state_digest(state_before),
            after=state_digest(state),
            delta=state_delta(state_before, state),
            decision_point=decision_point(state),
        )

    return state, drained


def target_from_policy(state: dict[str, Any], policy: str | None) -> str | None:
    live = enemies(state)
    if not live:
        return None
    if policy in {None, "auto", "first"}:
        return live[0].get("entity_id")
    if policy == "lowest_hp":
        return min(live, key=lambda e: e.get("hp") or 999999).get("entity_id")
    if policy == "highest_hp":
        return max(live, key=lambda e: e.get("hp") or -1).get("entity_id")
    return policy


def card_needs_target(card: dict[str, Any]) -> bool:
    target_type = card.get("target_type")
    if isinstance(target_type, str):
        return target_type in {"AnyEnemy", "Enemy"}
    desc = str(card.get("description") or "")
    if "ALL enemies" in desc or "random enemy" in desc:
        return False
    if desc.startswith("Gain ") or desc.startswith("Whenever ") or desc.startswith("At the end "):
        return False
    return "Deal " in desc or "Apply " in desc


def find_card(state: dict[str, Any], name: str, *, occurrence: int = 0) -> dict[str, Any]:
    wanted = _norm(name)
    matches = [c for c in hand(state) if _norm(str(c.get("name") or "")) == wanted]
    if not matches:
        wanted_base = _base_card_norm(name)
        matches = [
            c for c in hand(state)
            if _base_card_norm(str(c.get("name") or "")) == wanted_base
        ]
    if not matches:
        available = ", ".join(str(c.get("name")) for c in hand(state))
        raise RuntimeError(f"Card {name!r} not found in hand. Available: {available}")
    if occurrence >= len(matches):
        raise RuntimeError(f"Card {name!r} occurrence {occurrence} not found; {len(matches)} match(es)")
    return matches[occurrence]


def find_potion(state: dict[str, Any], slot: int) -> dict[str, Any] | None:
    player = state.get("player") or {}
    for potion in player.get("potions") or []:
        if isinstance(potion, dict) and potion.get("slot") == slot:
            return potion
    return None


def normalize_action(action: Any) -> dict[str, Any]:
    if isinstance(action, str):
        return {"action": action}
    if not isinstance(action, dict):
        raise RuntimeError(f"Action must be a string or object, got {type(action).__name__}")
    if "action" in action:
        return dict(action)
    if "play" in action or "card" in action:
        value = action.get("play", action.get("card"))
        normalized = {k: v for k, v in action.items() if k not in {"play", "card"}}
        normalized.update({"action": "play_card", "card": value})
        return normalized
    if "potion" in action or "use_potion" in action:
        value = action.get("potion", action.get("use_potion"))
        normalized = {k: v for k, v in action.items() if k not in {"potion", "use_potion"}}
        normalized.update({"action": "use_potion", "slot": value})
        return normalized
    if "end" in action or "end_turn" in action:
        normalized = {k: v for k, v in action.items() if k not in {"end", "end_turn"}}
        normalized.update({"action": "end_turn"})
        return normalized
    if len(action) == 1:
        key, value = next(iter(action.items()))
        if key == "drain":
            return {"action": "drain"}
        single_index_aliases = {
            "reward",
            "map",
            "event",
            "rest",
            "shop",
            "pick_card",
            "select_card_reward",
            "combat_select_card",
            "deck_select_card",
            "hand_select",
            "select_card",
            "select_bundle",
            "select_relic",
            "claim_treasure_relic",
            "discard_potion",
        }
        if key in single_index_aliases:
            return {"action": key, "index": value}
    raise RuntimeError(f"Action object needs an 'action' field: {action}")


def action_body_from_plan(
    client: Any,
    planned: dict[str, Any],
    *,
    auto_target: bool,
    current_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    action = planned.get("action")
    if action in {"play", "play_card"}:
        state = current_state if current_state is not None else client.state()
        card_obj: dict[str, Any] | None = None
        if "card" in planned:
            card_obj = find_card(state, str(planned["card"]), occurrence=int(planned.get("occurrence", 0)))
            card_index = card_obj.get("index")
        else:
            card_index = planned.get("card_index", planned.get("index"))
            if card_index is None:
                raise RuntimeError("play_card needs 'card', 'card_index', or 'index'")
            for candidate in hand(state):
                if candidate.get("index") == card_index:
                    card_obj = candidate
                    break

        body: dict[str, Any] = {"action": "play_card", "card_index": card_index}
        target_policy = planned.get("target")
        if target_policy is not None and card_obj and not card_needs_target(card_obj):
            target_policy = None
        if target_policy is None and auto_target and card_obj and card_needs_target(card_obj):
            target_policy = "auto"
        target = target_from_policy(state, target_policy) if target_policy is not None else None
        if target is not None:
            body["target"] = target
        return body, state

    if action in {"potion", "use_potion"}:
        state = current_state if current_state is not None else client.state()
        slot = planned.get("slot")
        if slot is None:
            raise RuntimeError("use_potion needs 'slot'")
        body = {"action": "use_potion", "slot": slot}
        target_policy = planned.get("target")
        potion = find_potion(state, int(slot))
        if target_policy is None and auto_target and potion and potion.get("target_type") == "AnyEnemy":
            target_policy = "auto"
        target = target_from_policy(state, target_policy) if target_policy is not None else None
        if target is not None:
            body["target"] = target
        return body, state

    raw_map = {
        "end_turn": ("end_turn", None),
        "choose_map_node": ("choose_map_node", "index"),
        "map": ("choose_map_node", "index"),
        "choose_event_option": ("choose_event_option", "index"),
        "event": ("choose_event_option", "index"),
        "choose_rest_option": ("choose_rest_option", "index"),
        "rest": ("choose_rest_option", "index"),
        "shop_purchase": ("shop_purchase", "index"),
        "shop": ("shop_purchase", "index"),
        "claim_reward": ("claim_reward", "index"),
        "reward": ("claim_reward", "index"),
        "select_card_reward": ("select_card_reward", "card_index"),
        "pick_card": ("select_card_reward", "card_index"),
        "skip_card_reward": ("skip_card_reward", None),
        "combat_select_card": ("combat_select_card", "card_index"),
        "hand_select": ("combat_select_card", "card_index"),
        "combat_confirm_selection": ("combat_confirm_selection", None),
        "deck_select_card": ("select_card", "index"),
        "deck_confirm_selection": ("confirm_selection", None),
        "deck_cancel_selection": ("cancel_selection", None),
        "select_card": ("select_card", "index"),
        "confirm_selection": ("confirm_selection", None),
        "cancel_selection": ("cancel_selection", None),
        "select_bundle": ("select_bundle", "index"),
        "confirm_bundle_selection": ("confirm_bundle_selection", None),
        "cancel_bundle_selection": ("cancel_bundle_selection", None),
        "select_relic": ("select_relic", "index"),
        "skip_relic_selection": ("skip_relic_selection", None),
        "relic_skip": ("skip_relic_selection", None),
        "claim_treasure_relic": ("claim_treasure_relic", "index"),
        "proceed": ("proceed", None),
        "advance_dialogue": ("advance_dialogue", None),
        "discard_potion": ("discard_potion", "slot"),
        "crystal_sphere_set_tool": ("crystal_sphere_set_tool", "tool"),
        "crystal_sphere_click_cell": ("crystal_sphere_click_cell", None),
        "crystal_sphere_proceed": ("crystal_sphere_proceed", None),
    }
    if action == "raw":
        body = planned.get("body")
        if not isinstance(body, dict):
            raise RuntimeError("raw action needs object 'body'")
        return body, None
    if action not in raw_map:
        raise RuntimeError(f"Unsupported action: {action}")

    http_action, param = raw_map[action]
    body = {"action": http_action}
    if action == "crystal_sphere_click_cell":
        if planned.get("x") is None or planned.get("y") is None:
            raise RuntimeError("crystal_sphere_click_cell needs 'x' and 'y'")
        body["x"] = planned["x"]
        body["y"] = planned["y"]
    elif param is not None:
        source_key = "card_index" if param == "card_index" else param
        value = planned.get(source_key, planned.get("index"))
        if value is None:
            raise RuntimeError(f"{action} needs '{source_key}'")
        body[param] = value
    return body, None


def hand_signature(state: dict[str, Any]) -> list[tuple[Any, Any]]:
    return [(card.get("index"), card.get("name")) for card in hand(state)]


def state_digest_key(state: dict[str, Any]) -> str:
    return json.dumps(state_digest(state), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def log_wait(logger: JsonlLogger, *, started: float, reason: str, polls: int) -> None:
    logger.write(
        "wait",
        reason=reason,
        polls=polls,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )


def wait_for_card_play_applied(
    client: Any,
    state_before: dict[str, Any],
    *,
    before_sig: list[tuple[Any, Any]],
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    settling_state: dict[str, Any] | None = None
    for poll in range(max_polls):
        state = client.state()
        if state.get("state_type") != state_before.get("state_type"):
            log_wait(logger, started=started, reason="card_play_state_changed", polls=poll + 1)
            return state
        if not is_combat_state(state):
            log_wait(logger, started=started, reason="card_play_left_combat", polls=poll + 1)
            return state
        if hand_signature(state) != before_sig:
            if is_transient_state(state) and poll + 1 < max_polls:
                time.sleep(poll_delay)
                continue
            if settling_state is None:
                if poll + 1 >= max_polls:
                    log_wait(
                        logger,
                        started=started,
                        reason="card_play_changed_unsettled",
                        polls=poll + 1,
                    )
                    return state
                settling_state = state
                time.sleep(poll_delay)
                continue
            if state_digest_key(state) == state_digest_key(settling_state):
                log_wait(logger, started=started, reason="card_play_settled", polls=poll + 1)
                return state
            if poll + 1 >= max_polls:
                log_wait(
                    logger,
                    started=started,
                    reason="card_play_changed_unsettled",
                    polls=poll + 1,
                )
                return state
            settling_state = state
        time.sleep(poll_delay)
    log_wait(logger, started=started, reason="card_play_timeout", polls=max_polls)
    raise RuntimeError(
        f"Timed out waiting for card play to apply after {max_polls} polls"
    )


def wait_for_state_change(
    client: Any,
    state_before: dict[str, Any],
    *,
    logger: JsonlLogger,
    reason: str,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    before_key = state_digest_key(state_before)
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) != before_key and not is_transient_state(state):
            log_wait(logger, started=started, reason=f"{reason}_state_changed", polls=poll + 1)
            return state
        time.sleep(poll_delay)
    log_wait(logger, started=started, reason=f"{reason}_state_unchanged", polls=max_polls)
    raise RuntimeError(f"Timed out waiting for {reason} state change after {max_polls} polls")


def wait_for_map_node_transition(
    client: Any,
    state_before: dict[str, Any],
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if (
            state.get("state_type") != "map"
            and not is_transient_state(state)
            and is_ready_combat_decision(state)
        ):
            log_wait(logger, started=started, reason="map_node_state_changed", polls=poll + 1)
            return state
        time.sleep(poll_delay)
    log_wait(logger, started=started, reason="map_node_still_on_map", polls=max_polls)
    state_type = last_state.get("state_type")
    raise RuntimeError(
        f"Timed out waiting for map node transition after {max_polls} polls; "
        f"last state_type={state_type!r}"
    )


def wait_for_player_or_screen(
    client: Any,
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    last_state: dict[str, Any] = {}
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if not is_transient_state(state):
            log_wait(logger, started=started, reason="ready_state", polls=poll + 1)
            return state
        time.sleep(poll_delay)
    state_type = last_state.get("state_type")
    log_wait(logger, started=started, reason="ready_state_timeout", polls=max_polls)
    raise RuntimeError(
        f"Timed out waiting for a ready state after {max_polls} polls; last state_type={state_type!r}"
    )


def wait_for_end_turn_resolution(
    client: Any,
    state_before: dict[str, Any],
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    before_key = state_digest_key(state_before)
    before_battle = state_before.get("battle") or {}
    before_round = before_battle.get("round")
    seen_change = False
    ready_state: dict[str, Any] | None = None
    ready_reason: str | None = None
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) != before_key:
            seen_change = True
        if not seen_change:
            time.sleep(poll_delay)
            continue
        candidate_reason: str | None = None
        if state.get("state_type") != state_before.get("state_type") and not is_transient_state(state):
            candidate_reason = "end_turn_screen_changed"
        elif is_combat_state(state):
            battle = state.get("battle") or {}
            if (
                battle.get("turn") == "player"
                and battle.get("is_play_phase") is True
                and battle.get("round") != before_round
                and is_ready_combat_decision(state)
            ):
                candidate_reason = "end_turn_next_player_turn"
        elif not is_transient_state(state):
            candidate_reason = "end_turn_ready_state"

        if candidate_reason is not None:
            if poll + 1 >= max_polls:
                log_wait(
                    logger,
                    started=started,
                    reason=f"{candidate_reason}_unsettled",
                    polls=poll + 1,
                )
                return state
            if ready_state is not None and state_digest_key(state) == state_digest_key(ready_state):
                log_wait(logger, started=started, reason=f"{ready_reason}_settled", polls=poll + 1)
                return state
            ready_state = state
            ready_reason = candidate_reason
        else:
            ready_state = None
            ready_reason = None
        time.sleep(poll_delay)
    state_type = last_state.get("state_type")
    log_wait(logger, started=started, reason="end_turn_timeout", polls=max_polls)
    raise RuntimeError(
        f"Timed out waiting for end turn after {max_polls} polls; last state_type={state_type!r}"
    )


def should_wait_for_state_change_after(body: dict[str, Any]) -> bool:
    return body.get("action") in {
        "choose_map_node",
        "choose_event_option",
        "advance_dialogue",
        "choose_rest_option",
        "shop_purchase",
        "claim_reward",
        "select_card_reward",
        "skip_card_reward",
        "combat_select_card",
        "combat_confirm_selection",
        "select_card",
        "select_bundle",
        "select_relic",
        "skip_relic_selection",
        "claim_treasure_relic",
        "proceed",
        "confirm_selection",
        "cancel_selection",
        "confirm_bundle_selection",
        "cancel_bundle_selection",
        "crystal_sphere_proceed",
    }


def execute_actions(
    client: Any,
    actions: list[Any],
    *,
    logger: JsonlLogger,
    stats: RunStats,
    auto_target: bool,
    drain_after: bool,
    wait_after_end_turn: bool,
    max_polls: int,
    poll_delay: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    executed: list[dict[str, Any]] = []
    final_state: dict[str, Any] = {}

    for raw_action in actions:
        planned = normalize_action(raw_action)
        if planned.get("action") == "drain":
            final_state, drained = drain_trivial(
                client,
                logger=logger,
                stats=stats,
                max_steps=int(planned.get("max_steps", 30)),
                poll_delay=poll_delay,
            )
            executed.extend({"drain": item} for item in drained)
            continue

        current_state = final_state if final_state else None
        body, state_before = action_body_from_plan(
            client,
            planned,
            auto_target=auto_target,
            current_state=current_state,
        )
        if state_before is None:
            state_before = final_state if final_state else client.state()
        state_before = deepcopy(state_before)
        stats.actions += 1
        logger.write(
            "planned_action",
            planned=planned,
            body=body,
            state_type=(state_before or {}).get("state_type"),
            before=state_digest(state_before),
            decision_point=decision_point(state_before),
        )
        before_sig = (
            hand_signature(state_before)
            if body.get("action") == "play_card" and state_before is not None
            else []
        )
        client.post(body)
        executed.append({"planned": planned, "body": body})

        if body.get("action") == "play_card" and state_before is not None:
            final_state = wait_for_card_play_applied(
                client,
                state_before,
                before_sig=before_sig,
                logger=logger,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        elif body.get("action") == "end_turn" and wait_after_end_turn:
            final_state = wait_for_end_turn_resolution(
                client,
                state_before,
                logger=logger,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        elif body.get("action") == "choose_map_node":
            final_state = wait_for_map_node_transition(
                client,
                state_before,
                logger=logger,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        elif should_wait_for_state_change_after(body):
            final_state = wait_for_state_change(
                client,
                state_before,
                logger=logger,
                reason="action",
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        else:
            final_state = client.state()

        logger.write(
            "action_result",
            planned=planned,
            body=body,
            before=state_digest(state_before),
            after=state_digest(final_state),
            delta=state_delta(state_before, final_state),
            decision_point=decision_point(final_state),
        )

        if drain_after:
            final_state, drained = drain_trivial(
                client,
                logger=logger,
                stats=stats,
                max_steps=30,
                poll_delay=poll_delay,
                initial_state=final_state,
            )
            executed.extend({"drain": item} for item in drained)

    if not final_state:
        final_state = client.state()
    return final_state, executed


def load_actions(value: str) -> list[Any]:
    text = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
    parsed = json.loads(text)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise RuntimeError("Actions JSON must be an object or list")


def log_state_result(logger: JsonlLogger, state: dict[str, Any]) -> None:
    logger.write(
        "state_result",
        state=state_digest(state),
        decision_point=decision_point(state),
    )


def emit_result(
    *,
    ok: bool,
    stats: RunStats,
    log_path: Path | None,
    state: dict[str, Any] | None = None,
    executed: list[dict[str, Any]] | None = None,
    drained: list[dict[str, Any]] | None = None,
    verbose: bool = False,
    indent: int | None = 2,
    error: str | None = None,
) -> None:
    result: dict[str, Any] = {
        "ok": ok,
        "summary": {
            **stats.as_dict(),
            "log_path": str(log_path) if log_path is not None else None,
        },
    }
    if error is not None:
        result["error"] = error
    if executed is not None:
        result["executed"] = executed
    if drained is not None:
        result["drained"] = drained
    if state is not None:
        result["state"] = summarize_state(state, verbose=verbose)
    print(_json_dumps(result, indent=indent))


def resolve_log_path(path: Path) -> Path:
    if not path.exists() and not path.is_absolute():
        repo_relative = _repo_root() / path
        if repo_relative.exists():
            path = repo_relative
    return path


def read_log_events(path: Path) -> tuple[Path, list[dict[str, Any]]]:
    path = resolve_log_path(path)
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return path, events


def event_ts(event: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event["ts"]))
    except Exception:
        return None


def http_event_start_ts(event: dict[str, Any]) -> datetime | None:
    explicit = event.get("started_ts")
    if explicit:
        try:
            return datetime.fromisoformat(str(explicit))
        except Exception:
            pass
    completed = event_ts(event)
    elapsed_ms = event.get("elapsed_ms")
    if completed is not None and isinstance(elapsed_ms, (int, float)):
        return completed - timedelta(milliseconds=float(elapsed_ms))
    return completed


def event_span_ms(events: list[dict[str, Any]]) -> float | None:
    timestamps = [ts for event in events if (ts := event_ts(event)) is not None]
    if len(timestamps) < 2:
        return None
    return round((max(timestamps) - min(timestamps)).total_seconds() * 1000.0, 1)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 1)


def _timing_distribution(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "total_ms": round(sum(values), 1),
        "avg_ms": round(sum(values) / len(values), 1) if values else None,
        "p50_ms": _percentile(values, 0.50),
        "p90_ms": _percentile(values, 0.90),
        "max_ms": round(max(values), 1) if values else None,
    }


def summarize_command_runs(logs: list[tuple[Path, list[dict[str, Any]]]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path, events in logs:
        timestamps = [ts for event in events if (ts := event_ts(event)) is not None]
        if not timestamps:
            continue
        ordered_events = sorted(events, key=lambda event: str(event.get("ts") or ""))
        run_start = next((event for event in ordered_events if event.get("kind") == "run_start"), {})
        run_end = next((event for event in reversed(ordered_events) if event.get("kind") == "run_end"), {})
        http_events = [event for event in ordered_events if event.get("kind") == "http"]
        post_events = [event for event in http_events if event.get("method") == "POST"]
        start_ts = min(timestamps)
        end_ts = max(timestamps)
        first_post_start_ts = http_event_start_ts(post_events[0]) if post_events else None
        first_post_end_ts = event_ts(post_events[0]) if post_events else None
        decision_events = [
            event
            for event in ordered_events
            if event.get("kind") in {"action_result", "drain_result", "state_result"}
            and isinstance(event.get("decision_point"), dict)
        ]
        rows.append(
            {
                "path": str(path),
                "command": run_start.get("command"),
                "argv": run_start.get("argv"),
                "start_ts": start_ts.isoformat(timespec="milliseconds"),
                "end_ts": end_ts.isoformat(timespec="milliseconds"),
                "active_wall_time_ms": event_span_ms(ordered_events),
                "http_calls": len(http_events),
                "get_calls": sum(1 for event in http_events if event.get("method") == "GET"),
                "post_calls": len(post_events),
                "first_post_action": post_events[0].get("action") if post_events else None,
                "first_post_started_ts": (
                    first_post_start_ts.isoformat(timespec="milliseconds")
                    if first_post_start_ts
                    else None
                ),
                "first_post_completed_ts": (
                    first_post_end_ts.isoformat(timespec="milliseconds")
                    if first_post_end_ts
                    else None
                ),
                "first_post_delay_ms": round(
                    (first_post_start_ts - start_ts).total_seconds() * 1000.0,
                    1,
                )
                if first_post_start_ts
                else None,
                "actions": run_end.get("actions"),
                "final_decision_point": decision_events[-1].get("decision_point") if decision_events else None,
            }
        )

    rows.sort(key=lambda row: str(row.get("start_ts") or ""))
    previous_end: datetime | None = None
    gaps: list[float] = []
    for row in rows:
        start = datetime.fromisoformat(str(row["start_ts"]))
        end = datetime.fromisoformat(str(row["end_ts"]))
        if previous_end is None:
            row["gap_before_ms"] = None
        else:
            gap = max(0.0, round((start - previous_end).total_seconds() * 1000.0, 1))
            row["gap_before_ms"] = gap
            gaps.append(gap)
        previous_end = end

    post_rows = [row for row in rows if int(row.get("post_calls") or 0) > 0]
    post_command_gap_values: list[float] = []
    post_to_first_post_values: list[float] = []
    post_pairs: list[dict[str, Any]] = []
    previous_post_row: dict[str, Any] | None = None
    for row in post_rows:
        if previous_post_row is not None:
            previous_end_dt = datetime.fromisoformat(str(previous_post_row["end_ts"]))
            current_start_dt = datetime.fromisoformat(str(row["start_ts"]))
            command_gap = max(
                0.0,
                round((current_start_dt - previous_end_dt).total_seconds() * 1000.0, 1),
            )
            post_command_gap_values.append(command_gap)
            first_post_gap: float | None = None
            if row.get("first_post_started_ts"):
                current_first_post_dt = datetime.fromisoformat(str(row["first_post_started_ts"]))
                first_post_gap = max(
                    0.0,
                    round((current_first_post_dt - previous_end_dt).total_seconds() * 1000.0, 1),
                )
                post_to_first_post_values.append(first_post_gap)
            post_pairs.append(
                {
                    "from_path": previous_post_row["path"],
                    "to_path": row["path"],
                    "to_first_post_action": row.get("first_post_action"),
                    "command_start_gap_ms": command_gap,
                    "first_post_gap_ms": first_post_gap,
                    "from_final_decision_point": previous_post_row.get("final_decision_point"),
                    "to_final_decision_point": row.get("final_decision_point"),
                }
            )
        previous_post_row = row

    return {
        "runs": rows,
        "inter_command_gaps": _timing_distribution(gaps),
        "next_post_gaps": {
            "command_start": _timing_distribution(post_command_gap_values),
            "first_post": _timing_distribution(post_to_first_post_values),
            "pairs": post_pairs,
        },
    }


def analyze_events(
    paths: list[Path],
    events: list[dict[str, Any]],
    *,
    active_wall_time_ms: float | None = None,
) -> dict[str, Any]:
    event_kinds = Counter(str(e.get("kind") or "unknown") for e in events)
    http_events = [e for e in events if e.get("kind") == "http"]
    by_action: dict[str, dict[str, Any]] = {}
    for event in http_events:
        action = event.get("action") or f"{event.get('method')} {event.get('path')}"
        bucket = by_action.setdefault(action, {"count": 0, "elapsed_ms": 0.0, "errors": 0})
        bucket["count"] += 1
        bucket["elapsed_ms"] += float(event.get("elapsed_ms") or 0)
        if event.get("error") or (event.get("status_code") and event.get("status_code") >= 400):
            bucket["errors"] += 1

    actions = []
    for action, data in sorted(by_action.items(), key=lambda item: item[1]["elapsed_ms"], reverse=True):
        count = data["count"]
        total = data["elapsed_ms"]
        actions.append(
            {
                "action": action,
                "count": count,
                "total_ms": round(total, 1),
                "avg_ms": round(total / count, 1) if count else 0,
                "errors": data["errors"],
            }
        )

    wait_events = [e for e in events if e.get("kind") == "wait"]
    wait_reasons = Counter(str(e.get("reason") or "unknown") for e in wait_events)
    wait_elapsed_values = [
        float(e.get("elapsed_ms"))
        for e in wait_events
        if isinstance(e.get("elapsed_ms"), (int, float))
    ]
    decision_point_events = Counter()
    before_decision_points = Counter()
    after_decision_points = Counter()
    after_decision_sequence: list[dict[str, Any]] = []
    final_decision_point: dict[str, Any] | None = None
    transitions = Counter()
    gameplay = {
        "player_hp_delta": 0,
        "player_gold_delta": 0,
        "enemy_hp_lost": 0,
        "enemy_block_lost": 0,
    }
    for event in events:
        point = event.get("decision_point") or {}
        point_kind = point.get("kind")
        if point_kind:
            decision_point_events[str(point_kind)] += 1
            if event.get("kind") in {"planned_action", "drain_action"}:
                before_decision_points[str(point_kind)] += 1
            elif event.get("kind") in {"action_result", "drain_result", "state_result"}:
                after_decision_points[str(point_kind)] += 1
                final_decision_point = point
                if (
                    not after_decision_sequence
                    or _decision_point_key(point) != _decision_point_key(after_decision_sequence[-1])
                ):
                    after_decision_sequence.append(point)

        delta = event.get("delta") or {}
        state_type = delta.get("state_type") or {}
        before_type = state_type.get("before")
        after_type = state_type.get("after")
        if before_type or after_type:
            transitions[f"{before_type}->{after_type}"] += 1

        player_delta = delta.get("player") or {}
        gameplay["player_hp_delta"] += int(player_delta.get("hp") or 0)
        gameplay["player_gold_delta"] += int(player_delta.get("gold") or 0)

        combat_delta = delta.get("combat") or {}
        for enemy_delta in combat_delta.get("enemies") or []:
            hp_delta = enemy_delta.get("hp_delta")
            block_delta = enemy_delta.get("block_delta")
            if isinstance(hp_delta, (int, float)) and hp_delta < 0:
                gameplay["enemy_hp_lost"] += int(abs(hp_delta))
            if isinstance(block_delta, (int, float)) and block_delta < 0:
                gameplay["enemy_block_lost"] += int(abs(block_delta))

    wall_time_ms = event_span_ms(events)

    decision_points = Counter(str(point.get("kind") or "unknown") for point in after_decision_sequence)

    result = {
        "path": str(paths[0]) if len(paths) == 1 else None,
        "paths": [str(path) for path in paths],
        "events": len(events),
        "event_kinds": dict(sorted(event_kinds.items())),
        "http_calls": len(http_events),
        "http_total_ms": round(sum(float(e.get("elapsed_ms") or 0) for e in http_events), 1),
        "wall_time_ms": wall_time_ms,
        "active_wall_time_ms": active_wall_time_ms if active_wall_time_ms is not None else wall_time_ms,
        "actions": actions,
        "waits": {
            "count": len(wait_events),
            "total_polls": sum(int(e.get("polls") or 0) for e in wait_events),
            "elapsed_ms": _timing_distribution(wait_elapsed_values),
            "reasons": dict(sorted(wait_reasons.items())),
        },
        "decision_points": dict(sorted(decision_points.items())),
        "decision_point_events": dict(sorted(decision_point_events.items())),
        "decision_points_by_phase": {
            "before": dict(sorted(before_decision_points.items())),
            "after": dict(sorted(after_decision_points.items())),
        },
        "decision_path": after_decision_sequence,
        "final_decision_point": final_decision_point,
        "state_transitions": dict(sorted(transitions.items())),
        "gameplay": gameplay,
    }
    if len(paths) == 1:
        result.pop("paths")
    return result


def analyze_logs(paths: list[Path]) -> dict[str, Any]:
    resolved_paths: list[Path] = []
    all_events: list[dict[str, Any]] = []
    active_spans: list[float] = []
    logs: list[tuple[Path, list[dict[str, Any]]]] = []
    for path in paths:
        resolved, events = read_log_events(path)
        resolved_paths.append(resolved)
        all_events.extend(events)
        logs.append((resolved, events))
        span = event_span_ms(events)
        if span is not None:
            active_spans.append(span)
    all_events.sort(key=lambda event: str(event.get("ts") or ""))
    active_wall_time_ms = round(sum(active_spans), 1) if active_spans else None
    result = analyze_events(resolved_paths, all_events, active_wall_time_ms=active_wall_time_ms)
    if len(logs) > 1:
        result["command_timing"] = summarize_command_runs(logs)
    return result


def analyze_log(path: Path) -> dict[str, Any]:
    return analyze_logs([path])


def expand_log_path_args(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        if any(char in value for char in "*?["):
            patterns = [value]
            value_path = Path(value)
            if not value_path.is_absolute():
                patterns.append(str(_repo_root() / value))
            matches: list[str] = []
            for pattern in patterns:
                matches.extend(glob.glob(pattern))
            unique_matches = sorted(dict.fromkeys(matches))
            paths.extend(Path(match) for match in unique_matches)
        else:
            paths.append(Path(value))
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sts2-fast",
        description="Fast CLI adapter for STS2 HTTP gameplay automation.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--trust-env", action="store_true")
    parser.add_argument("--log", default=None, help="JSONL log path. Defaults to logs/sts2-fast/<timestamp>.jsonl")
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state", help="Print a concise game-state summary")
    state.add_argument("--drain", action="store_true", help="Auto-resolve trivial screens before printing")
    state.add_argument("--verbose", action="store_true")

    drain = sub.add_parser("drain", help="Auto-resolve no-decision screens")
    drain.add_argument("--max-steps", type=int, default=30)
    drain.add_argument("--verbose", action="store_true")

    act = sub.add_parser("act", help="Execute a JSON action plan")
    act.add_argument("actions", help="JSON object/list, or @path/to/actions.json")
    act.add_argument("--no-auto-target", action="store_true")
    act.add_argument("--drain", action="store_true", help="Run trivial drain after each action")
    act.add_argument("--no-wait-end-turn", action="store_true")
    act.add_argument("--max-polls", type=int, default=60)
    act.add_argument("--poll-delay", type=float, default=0.12)
    act.add_argument("--verbose", action="store_true")

    cards = sub.add_parser("cards", help="Play a sequence of cards by name in one CLI call")
    cards.add_argument("cards", nargs="+", help="Card names in play order")
    cards.add_argument("--target", default=None, help="first, lowest_hp, highest_hp, entity id, or omitted for auto")
    cards.add_argument("--end-turn", action="store_true")
    cards.add_argument("--drain", action="store_true")
    cards.add_argument("--max-polls", type=int, default=60)
    cards.add_argument("--poll-delay", type=float, default=0.12)
    cards.add_argument("--verbose", action="store_true")

    analyze = sub.add_parser("analyze-log", help="Summarize sts2-fast JSONL timing logs")
    analyze.add_argument("paths", nargs="+")

    return parser


def make_client(args: argparse.Namespace, logger: JsonlLogger, stats: RunStats) -> STS2Client:
    return STS2Client(
        base_url=args.base_url,
        logger=logger,
        stats=stats,
        timeout=args.timeout,
        trust_env=args.trust_env,
    )


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    indent = None if args.compact else 2

    if args.command == "analyze-log":
        print(_json_dumps(analyze_logs(expand_log_path_args(args.paths)), indent=indent))
        return 0

    log_path = None if args.no_log else Path(args.log) if args.log else _default_log_path()
    logger = JsonlLogger(log_path)
    stats = RunStats()
    logger.write("run_start", command=args.command, argv=sys.argv[1:] if argv is None else argv)

    client = make_client(args, logger, stats)
    try:
        if args.command == "state":
            if args.drain:
                state, drained = drain_trivial(client, logger=logger, stats=stats)
            else:
                state, drained = client.state(), None
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                state=state,
                drained=drained,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "drain":
            state, drained = drain_trivial(
                client,
                logger=logger,
                stats=stats,
                max_steps=args.max_steps,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                state=state,
                drained=drained,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "act":
            actions = load_actions(args.actions)
            state, executed = execute_actions(
                client,
                actions,
                logger=logger,
                stats=stats,
                auto_target=not args.no_auto_target,
                drain_after=args.drain,
                wait_after_end_turn=not args.no_wait_end_turn,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                state=state,
                executed=executed,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "cards":
            actions: list[Any] = [
                {"action": "play_card", "card": card, "target": args.target}
                for card in args.cards
            ]
            if args.end_turn:
                actions.append({"action": "end_turn"})
            state, executed = execute_actions(
                client,
                actions,
                logger=logger,
                stats=stats,
                auto_target=True,
                drain_after=args.drain,
                wait_after_end_turn=True,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                state=state,
                executed=executed,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        parser.error(f"Unhandled command {args.command}")
        return 2
    except Exception as exc:
        logger.write("run_error", error=str(exc))
        emit_result(
            ok=False,
            stats=stats,
            log_path=log_path,
            error=str(exc),
            indent=indent,
        )
        return 1
    finally:
        logger.write("run_end", **stats.as_dict())
        client.close()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
