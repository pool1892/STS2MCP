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
import platform
import re
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
DEFAULT_POLL_DELAY = 0.04
DEFAULT_INITIAL_POLL_DELAY = 0.015
DEFAULT_MAX_POLLS = 120
DEFAULT_MENU_MAX_POLLS = 120
DEFAULT_START_RUN_MAX_POLLS = 160
DEFAULT_PROFILE_POLL_DELAY = 0.08
READY_STATE_SETTLE_POLLS = 2
FAST_CARD_SETTLE_POLLS = 1
DELAYED_CARD_SETTLE_POLLS = 8
SELECTION_CARD_SETTLE_POLLS = 6
START_TURN_SETTLE_POLLS = 8
MAP_COMBAT_SETTLE_POLLS = 8
SELECTION_POTION_SETTLE_POLLS = 8


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return _repo_root() / "logs" / "sts2-fast" / f"{stamp}-{os.getpid()}.jsonl"


def _git_dir() -> Path | None:
    git_path = _repo_root() / ".git"
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        prefix = "gitdir:"
        if text.startswith(prefix):
            return (_repo_root() / text[len(prefix):].strip()).resolve()
    return None


def _git_head_sha() -> str | None:
    git_dir = _git_dir()
    if git_dir is None:
        return None
    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref_path = git_dir / head.split(":", 1)[1].strip()
        try:
            return ref_path.read_text(encoding="utf-8").strip()[:12]
        except OSError:
            return None
    return head[:12] if head else None


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
        self.wait_seq = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def next_wait_id(self) -> str:
        self.wait_seq += 1
        return f"wait-{self.wait_seq:04d}"

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
    stdout_bytes: int = 0
    stdout_lines: int = 0
    stdout_writes: int = 0
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
            "stdout_bytes": self.stdout_bytes,
            "stdout_lines": self.stdout_lines,
            "stdout_writes": self.stdout_writes,
        }


class STS2Client:
    def __init__(
        self,
        *,
        base_url: str,
        endpoint: str = "singleplayer",
        logger: JsonlLogger,
        stats: RunStats,
        timeout: float,
        trust_env: bool,
    ):
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.logger = logger
        self.stats = stats
        self.active_wait_id: str | None = None
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
                wait_id=self.active_wait_id,
                params=params,
                action=json_body.get("action") if json_body else None,
                status_code=status_code,
                elapsed_ms=round(elapsed_ms, 1),
                response_bytes=len(response_text.encode("utf-8")),
                error=error,
            )

    def run_path(self) -> str:
        if self.endpoint == "multiplayer":
            return "/api/v1/multiplayer"
        return "/api/v1/singleplayer"

    def state(self) -> dict[str, Any]:
        data = self._request("GET", self.run_path(), params={"format": "json"})
        if not isinstance(data, dict):
            raise RuntimeError("Expected JSON object from game state")
        return data

    def state_text(self, *, format_name: str) -> str:
        return self._request_text("GET", self.run_path(), params={"format": format_name})

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", self.run_path(), json_body=body)
        result = data if isinstance(data, dict) else {}
        validate_post_response(result, body)
        return result

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", path, json_body=body)
        result = data if isinstance(data, dict) else {}
        validate_post_response(result, body)
        return result

    def post_json_unchecked(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = self._request("POST", path, json_body=body)
        return data if isinstance(data, dict) else {}

    def _request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
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
            return response_text
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
                wait_id=self.active_wait_id,
                params=params,
                action=json_body.get("action") if json_body else None,
                status_code=status_code,
                elapsed_ms=round(elapsed_ms, 1),
                response_bytes=len(response_text.encode("utf-8")),
                error=error,
            )


def validate_post_response(result: dict[str, Any], body: dict[str, Any]) -> None:
    status = result.get("status")
    error = result.get("error")
    ok = result.get("ok")
    if status == "error" or error or ok is False:
        action = body.get("action")
        detail = error or result.get("detail") or result.get("message") or "unknown error"
        raise RuntimeError(f"Action {action!r} failed: {detail}")


def post_json_unchecked(client: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    if hasattr(client, "post_json_unchecked"):
        return client.post_json_unchecked(path, body)
    return client.post_json(path, body)


def is_combat_state(state: dict[str, Any]) -> bool:
    return state.get("state_type") in COMBAT_STATES


def enemies(state: dict[str, Any]) -> list[dict[str, Any]]:
    battle = state.get("battle") or {}
    return [e for e in battle.get("enemies") or [] if isinstance(e, dict)]


def hand(state: dict[str, Any]) -> list[dict[str, Any]]:
    player = state.get("player") or {}
    return [c for c in player.get("hand") or [] if isinstance(c, dict)]


_DAMAGE_MULTIPLIER_RE = re.compile(r"\b(\d+)\s*(?:x|\*)\s*(\d+)\b", re.IGNORECASE)
_DAMAGE_TIMES_RE = re.compile(
    r"\bdeals?\s+(\d+)\s+damage\s+(\d+)\s+times\b",
    re.IGNORECASE,
)
_TIMES_FOR_DAMAGE_RE = re.compile(
    r"\b(\d+)\s+times\s+for\s+(\d+)\s+damage\b",
    re.IGNORECASE,
)
_FOR_DAMAGE_RE = re.compile(r"\bfor\s+(\d+)\s+damage\b", re.IGNORECASE)
_DAMAGE_RE = re.compile(r"\bdeals?\s+(\d+)\s+damage\b", re.IGNORECASE)
_PLAIN_INT_RE = re.compile(r"^\s*(\d+)\s*$")
_BOUND_CONSTRAINT_RE = re.compile(r"\b(bound|chains of binding)\b", re.IGNORECASE)


def _parse_damage_text(value: Any, *, allow_plain_number: bool) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.replace("\u00d7", "x")
    if allow_plain_number:
        match = _PLAIN_INT_RE.match(text)
        if match:
            return int(match.group(1))

    match = _DAMAGE_MULTIPLIER_RE.search(text)
    if match:
        return int(match.group(1)) * int(match.group(2))

    match = _DAMAGE_TIMES_RE.search(text)
    if match:
        return int(match.group(1)) * int(match.group(2))

    match = _TIMES_FOR_DAMAGE_RE.search(text)
    if match:
        return int(match.group(1)) * int(match.group(2))

    match = _FOR_DAMAGE_RE.search(text)
    if match:
        return int(match.group(1))

    match = _DAMAGE_RE.search(text)
    if match:
        return int(match.group(1))

    return None


def _is_attack_intent(intent: dict[str, Any]) -> bool:
    intent_type = str(intent.get("type") or "").casefold()
    title = str(intent.get("title") or "").casefold()
    return (
        "attack" in intent_type
        or "attack" in title
        or _parse_damage_text(intent.get("description"), allow_plain_number=False) is not None
    )


def _intent_attack_damage(intent: dict[str, Any]) -> int | None:
    for key, allow_plain_number in [
        ("label", False),
        ("description", False),
        ("title", False),
        ("label", True),
    ]:
        damage = _parse_damage_text(intent.get(key), allow_plain_number=allow_plain_number)
        if damage is not None:
            return damage
    return None


def _compact_status_name(status: dict[str, Any]) -> str | None:
    name = status.get("name") or status.get("id")
    if not name:
        return None
    amount = status.get("amount")
    if amount is None or amount == -1:
        return str(name)
    return f"{name} {amount}"


def _compact_status_names(statuses: list[Any]) -> list[str]:
    names: list[str] = []
    for status in statuses:
        if not isinstance(status, dict):
            continue
        name = _compact_status_name(status)
        if name:
            names.append(name)
    return names


def _constraint_text_values(item: dict[str, Any]) -> list[str]:
    values = [
        str(item[key])
        for key in ["id", "name", "description", "unplayable_reason"]
        if item.get(key)
    ]
    for keyword in item.get("keywords") or []:
        if isinstance(keyword, dict):
            values.extend(
                str(keyword[key])
                for key in ["id", "name", "description"]
                if keyword.get(key)
            )
        elif keyword:
            values.append(str(keyword))
    return values


def _has_bound_constraint(item: dict[str, Any]) -> bool:
    return any(_BOUND_CONSTRAINT_RE.search(value) for value in _constraint_text_values(item))


def combat_tactical_summary(state: dict[str, Any]) -> dict[str, Any]:
    player = state.get("player") or {}
    incoming_damage = 0
    unknown_attack_damage: list[str] = []
    enemy_attack_damage: list[dict[str, Any]] = []

    for enemy in enemies(state):
        enemy_damage = 0
        has_unknown_attack = False
        for intent in enemy.get("intents") or []:
            if not isinstance(intent, dict) or not _is_attack_intent(intent):
                continue
            damage = _intent_attack_damage(intent)
            if damage is None:
                has_unknown_attack = True
            else:
                enemy_damage += damage
        enemy_id = enemy.get("entity_id")
        enemy_attack_damage.append(
            {
                "id": enemy_id,
                "name": enemy.get("name"),
                "damage": enemy_damage,
            }
        )
        incoming_damage += enemy_damage
        if has_unknown_attack and enemy_id:
            unknown_attack_damage.append(str(enemy_id))

    player_statuses = [
        status
        for status in player.get("status") or []
        if isinstance(status, dict)
    ]
    player_status = _compact_status_names(player_statuses)
    constraints: list[str] = []

    block = player.get("block")
    if isinstance(block, (int, float)) and incoming_damage > block:
        constraints.append(f"incoming>block:{incoming_damage}>{int(block)}")
    elif incoming_damage == 0 and not unknown_attack_damage:
        constraints.append("no_incoming_attack")

    for status in player_statuses:
        if _has_bound_constraint(status):
            status_name = _compact_status_name(status) or "status"
            constraints.append(f"player_constraint:{status_name}")

    bound_cards = [
        f"{card.get('index')}:{card.get('name')}"
        for card in hand(state)
        if _has_bound_constraint(card)
    ]
    if bound_cards:
        constraints.append(f"bound_cards:{','.join(bound_cards)}")

    if unknown_attack_damage:
        constraints.append(f"unknown_attack_damage:{','.join(unknown_attack_damage)}")

    return {
        "incoming_damage": incoming_damage,
        "enemy_attack_damage": enemy_attack_damage,
        "player_status": player_status,
        "constraints": constraints,
    }


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
    if state_type == "menu":
        summary["menu"] = {
            "screen": state.get("menu_screen"),
            "message": state.get("message"),
            "options": state.get("options") or [],
            "blocked_options": state.get("blocked_options") or [],
        }
        if state.get("menu_screen") == "character_select":
            summary["menu"]["characters"] = [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "locked": c.get("locked"),
                    "hp": c.get("hp"),
                    "gold": c.get("gold"),
                    "energy": c.get("energy"),
                }
                for c in state.get("characters") or []
                if isinstance(c, dict)
            ]
        if "lobby" in state:
            summary["menu"]["lobby"] = state.get("lobby")
    elif state_type in COMBAT_STATES:
        battle = state.get("battle") or {}
        summary["combat"] = {
            "round": battle.get("round"),
            "turn": battle.get("turn"),
            "is_play_phase": battle.get("is_play_phase"),
            "tactical": combat_tactical_summary(state),
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
    elif state_type == "hand_select":
        hand_select = state.get("hand_select") or {}
        summary["hand_select"] = {
            "mode": hand_select.get("mode"),
            "prompt": hand_select.get("prompt"),
            "can_confirm": hand_select.get("can_confirm"),
            "selected_cards": hand_select.get("selected_cards") or [],
            "cards": [
                {
                    "index": c.get("index"),
                    "name": c.get("name"),
                    "cost": c.get("cost"),
                    "type": c.get("type"),
                    "description": c.get("description"),
                }
                for c in hand_select.get("cards") or []
                if isinstance(c, dict)
            ],
        }
    elif state_type == "bundle_select":
        summary["bundle_select"] = state.get("bundle_select") or {}
    elif state_type == "relic_select":
        summary["relic_select"] = state.get("relic_select") or {}
    elif state_type == "crystal_sphere":
        crystal = state.get("crystal_sphere") or {}
        summary["crystal_sphere"] = {
            "instructions_title": crystal.get("instructions_title"),
            "instructions_description": crystal.get("instructions_description"),
            "grid_width": crystal.get("grid_width"),
            "grid_height": crystal.get("grid_height"),
            "tool": crystal.get("tool"),
            "can_use_big_tool": crystal.get("can_use_big_tool"),
            "can_use_small_tool": crystal.get("can_use_small_tool"),
            "divinations_left_text": crystal.get("divinations_left_text"),
            "can_proceed": crystal.get("can_proceed"),
            "clickable_cells": crystal.get("clickable_cells") or [],
            "revealed_items": crystal.get("revealed_items") or [],
        }
    elif state_type in {"shop", "fake_merchant"}:
        summary[state_type] = state.get("shop") or state.get("fake_merchant") or {}

    return summary


def act_map_data(state: dict[str, Any]) -> dict[str, Any]:
    map_data = state.get("map")
    if not isinstance(map_data, dict) or not isinstance(map_data.get("nodes"), list):
        state_type = state.get("state_type")
        raise RuntimeError(
            "Whole act map is not available in current state; "
            f"state_type={state_type!r}. Read it from a map screen."
        )

    player = state.get("player") or {}
    return {
        "state_type": state.get("state_type"),
        "run": state.get("run"),
        "player": {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "gold": player.get("gold"),
            "potions": [
                {
                    "slot": potion.get("slot"),
                    "name": potion.get("name"),
                    "target_type": potion.get("target_type"),
                }
                for potion in player.get("potions") or []
                if isinstance(potion, dict)
            ],
            "relics": [
                relic.get("name")
                for relic in player.get("relics") or []
                if isinstance(relic, dict)
            ],
        },
        "current_position": map_data.get("current_position"),
        "visited": map_data.get("visited") or [],
        "next_options": map_data.get("next_options") or [],
        "boss": map_data.get("boss"),
        "bosses": map_data.get("bosses") or [],
        "nodes": map_data.get("nodes") or [],
    }


def normalize_menu_options(options: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for option in options:
        if isinstance(option, str):
            normalized.append({"name": option, "enabled": True})
        elif isinstance(option, dict):
            normalized.append(
                {
                    "name": option.get("name") or option.get("title"),
                    "enabled": option.get("enabled", not option.get("is_locked", False)),
                    "reason": option.get("reason"),
                }
            )
    return normalized


def menu_option_names(state: dict[str, Any], *, enabled_only: bool = True) -> list[str]:
    names: list[str] = []
    for option in normalize_menu_options(state.get("options") or []):
        name = option.get("name")
        if not name:
            continue
        if enabled_only and option.get("enabled") is False:
            continue
        names.append(str(name))
    return names


def menu_option_enabled(state: dict[str, Any], option_name: str) -> bool:
    wanted = option_name.casefold()
    return any(name.casefold() == wanted for name in menu_option_names(state, enabled_only=True))


def manual_timeline_reveal_pending_epoch_ids(state: dict[str, Any]) -> list[str] | None:
    for option in state.get("blocked_options") or []:
        if not isinstance(option, dict):
            continue
        name = option.get("name") or option.get("title") or option.get("option")
        reason = option.get("reason")
        if str(name or "").casefold() != "timeline":
            continue
        if reason != "manual_epoch_reveal_required":
            continue
        pending_ids = option.get("pending_epoch_ids") or []
        if not isinstance(pending_ids, list):
            pending_ids = [pending_ids]
        return [str(epoch_id) for epoch_id in pending_ids if epoch_id is not None]
    return None


def timeline_status_data(client: Any) -> dict[str, Any] | None:
    if not hasattr(client, "get_json"):
        return None
    try:
        data = client.get_json("/api/v1/timeline")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def pending_slot_unlock_epoch_ids_from_status(status: dict[str, Any] | None) -> list[str]:
    if not isinstance(status, dict):
        return []
    ids = status.get("pending_slot_unlock_epoch_ids") or []
    if not isinstance(ids, list):
        ids = [ids]
    return [str(epoch_id) for epoch_id in ids if epoch_id is not None]


def reveal_pending_timeline_epochs(client: Any) -> dict[str, Any]:
    if not hasattr(client, "post_json"):
        raise RuntimeError("Timeline reveal requires the HTTP client timeline endpoint")
    result = post_json_unchecked(
        client,
        "/api/v1/timeline",
        {"action": "reveal_pending", "dry_run": False},
    )
    if result.get("status") == "error" or result.get("error"):
        detail = result.get("error") or "Timeline reveal failed"
        if result.get("pending_slot_unlock_epoch_ids"):
            detail = f"{detail}; pending_slot_unlock_epoch_ids={result['pending_slot_unlock_epoch_ids']}"
        elif result.get("pending_epoch_ids"):
            detail = f"{detail}; pending_epoch_ids={result['pending_epoch_ids']}"
        raise RuntimeError(detail)
    return result


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
    if state_type == "menu":
        digest["menu"] = {
            "screen": state.get("menu_screen"),
            "message": state.get("message"),
            "options": normalize_menu_options(state.get("options") or []),
            "blocked_options": normalize_menu_options(state.get("blocked_options") or []),
        }
        if state.get("menu_screen") == "character_select":
            digest["menu"]["characters"] = [
                {
                    "id": character.get("id"),
                    "name": character.get("name"),
                    "locked": character.get("locked"),
                }
                for character in state.get("characters") or []
                if isinstance(character, dict)
            ]
        if isinstance(state.get("lobby"), dict):
            lobby = state["lobby"]
            digest["menu"]["lobby"] = {
                "type": lobby.get("type"),
                "game_mode": lobby.get("game_mode"),
                "all_ready": lobby.get("all_ready"),
                "is_about_to_begin": lobby.get("is_about_to_begin"),
                "is_local_ready": lobby.get("is_local_ready"),
                "players": [
                    {
                        "id": player.get("id"),
                        "character_id": player.get("character_id"),
                        "is_ready": player.get("is_ready"),
                    }
                    for player in lobby.get("players") or []
                    if isinstance(player, dict)
                ],
            }
    elif state_type in COMBAT_STATES:
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
    elif state_type == "hand_select":
        hand_select = state.get("hand_select") or {}
        digest["hand_select"] = {
            "mode": hand_select.get("mode"),
            "prompt": hand_select.get("prompt"),
            "can_confirm": hand_select.get("can_confirm"),
            "selected_cards": [
                {
                    "index": card.get("index"),
                    "name": card.get("name"),
                }
                for card in hand_select.get("selected_cards") or []
                if isinstance(card, dict)
            ],
            "cards": [
                {
                    "index": card.get("index"),
                    "name": card.get("name"),
                }
                for card in hand_select.get("cards") or []
                if isinstance(card, dict)
            ],
        }
    elif state_type == "bundle_select":
        bundle_select = state.get("bundle_select") or {}
        digest["bundle_select"] = {
            "prompt": bundle_select.get("prompt"),
            "can_confirm": bundle_select.get("can_confirm"),
            "can_cancel": bundle_select.get("can_cancel"),
            "selected_bundle": bundle_select.get("selected_bundle"),
            "bundles": [
                {
                    "index": bundle.get("index"),
                    "name": bundle.get("name"),
                    "title": bundle.get("title"),
                }
                for bundle in bundle_select.get("bundles") or []
                if isinstance(bundle, dict)
            ],
        }
    elif state_type == "relic_select":
        relic_select = state.get("relic_select") or {}
        digest["relic_select"] = {
            "prompt": relic_select.get("prompt"),
            "can_cancel": relic_select.get("can_cancel"),
            "relics": [
                {
                    "index": relic.get("index"),
                    "name": relic.get("name"),
                }
                for relic in relic_select.get("relics") or []
                if isinstance(relic, dict)
            ],
        }
    elif state_type == "crystal_sphere":
        crystal = state.get("crystal_sphere") or {}
        digest["crystal_sphere"] = {
            "grid_width": crystal.get("grid_width"),
            "grid_height": crystal.get("grid_height"),
            "tool": crystal.get("tool"),
            "can_use_big_tool": crystal.get("can_use_big_tool"),
            "can_use_small_tool": crystal.get("can_use_small_tool"),
            "divinations_left_text": crystal.get("divinations_left_text"),
            "can_proceed": crystal.get("can_proceed"),
            "clickable_cells": crystal.get("clickable_cells") or [],
            "revealed_items": [
                {
                    "item_type": item.get("item_type"),
                    "x": item.get("x"),
                    "y": item.get("y"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "is_good": item.get("is_good"),
                }
                for item in crystal.get("revealed_items") or []
                if isinstance(item, dict)
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
    elif state_type == "hand_select":
        hand_select = state.get("hand_select") or {}
        point["kind"] = "hand_select"
        point["option_count"] = len(hand_select.get("cards") or [])
        point["selected_count"] = len(hand_select.get("selected_cards") or [])
        point["can_confirm"] = hand_select.get("can_confirm")
    elif state_type == "crystal_sphere":
        crystal = state.get("crystal_sphere") or {}
        point["kind"] = "crystal_sphere"
        point["clickable_count"] = len(crystal.get("clickable_cells") or [])
        point["can_proceed"] = crystal.get("can_proceed")
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


def is_modal_decision_state(state: dict[str, Any]) -> bool:
    return state.get("state_type") in {"card_select", "bundle_select", "relic_select", "hand_select"}


def card_cost_value(card: dict[str, Any]) -> int | None:
    cost = card.get("cost")
    if isinstance(cost, (int, float)):
        return int(cost)
    text = str(cost or "").strip().casefold()
    if text in {"0", "x"}:
        return 0
    if text.startswith("0"):
        return 0
    try:
        return int(text)
    except ValueError:
        return None


def has_playable_hand_card(state: dict[str, Any]) -> bool:
    player = state.get("player") or {}
    energy = player.get("energy")
    try:
        energy_value = int(energy)
    except (TypeError, ValueError):
        return False
    for card in hand(state):
        cost = card_cost_value(card)
        if cost is None and energy_value > 0:
            return True
        if cost is not None and cost <= energy_value:
            return True
    return False


def is_ready_combat_decision(state: dict[str, Any]) -> bool:
    if not is_combat_state(state):
        return True
    battle = state.get("battle") or {}
    if battle.get("turn") != "player" or battle.get("is_play_phase") is not True:
        return False
    player = state.get("player") or {}
    return isinstance(player.get("hand"), list)


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
        items = [item for item in shop.get("items") or [] if isinstance(item, dict)]
        affordable_stocked = [
            item for item in items
            if item.get("is_stocked") is not False and item.get("can_afford") is True
        ]
        if shop.get("can_proceed") and not items:
            return {"action": "proceed"}, f"leave empty {state_type}"
        if items and not affordable_stocked:
            return {"action": "proceed"}, f"leave spent {state_type}"
        return None, None

    if state_type == "crystal_sphere":
        crystal = state.get("crystal_sphere") or {}
        if crystal.get("can_proceed") and not crystal.get("clickable_cells"):
            return {"action": "crystal_sphere_proceed"}, "leave completed crystal sphere"
        return None, None

    return None, None


def drain_trivial(
    client: Any,
    *,
    logger: JsonlLogger,
    stats: RunStats,
    max_steps: int = 30,
    poll_delay: float = DEFAULT_POLL_DELAY,
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
                sleep_for_poll(transient_polls - 1, poll_delay)
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
        if body.get("action") == "choose_map_node":
            state = wait_for_map_node_transition(
                client,
                state_before,
                logger=logger,
                max_polls=DEFAULT_MAX_POLLS,
                poll_delay=poll_delay,
            )
        else:
            state = wait_for_state_change(
                client,
                state_before,
                logger=logger,
                reason="drain",
                max_polls=DEFAULT_MENU_MAX_POLLS,
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


def potion_may_open_modal(state: dict[str, Any], body: dict[str, Any]) -> bool:
    slot = body.get("slot")
    if not isinstance(slot, int):
        return False
    potion = find_potion(state, slot)
    if not potion:
        return False
    text = " ".join(
        str(potion.get(key) or "")
        for key in ("name", "description")
    ).lower()
    modal_potion_names = (
        "skill potion",
        "attack potion",
        "power potion",
        "colorless potion",
    )
    return "choose" in text or "select" in text or any(name in text for name in modal_potion_names)


MCP_ACTION_ALIASES: dict[str, tuple[str, str | None, dict[str, str]]] = {
    "use_potion": ("use_potion", None, {"slot": "slot", "target": "target"}),
    "discard_potion": ("discard_potion", None, {"slot": "slot"}),
    "proceed_to_map": ("proceed", None, {}),
    "combat_play_card": ("play_card", None, {"card_index": "card_index", "target": "target"}),
    "combat_end_turn": ("end_turn", None, {}),
    "combat_select_card": ("combat_select_card", None, {"card_index": "card_index"}),
    "combat_confirm_selection": ("combat_confirm_selection", None, {}),
    "rewards_claim": ("claim_reward", None, {"reward_index": "index"}),
    "rewards_pick_card": ("select_card_reward", None, {"card_index": "card_index"}),
    "rewards_skip_card": ("skip_card_reward", None, {}),
    "map_choose_node": ("choose_map_node", None, {"node_index": "index"}),
    "rest_choose_option": ("choose_rest_option", None, {"option_index": "index"}),
    "shop_purchase": ("shop_purchase", None, {"item_index": "index"}),
    "event_choose_option": ("choose_event_option", None, {"option_index": "index"}),
    "event_advance_dialogue": ("advance_dialogue", None, {}),
    "deck_select_card": ("select_card", None, {"card_index": "index"}),
    "deck_confirm_selection": ("confirm_selection", None, {}),
    "deck_cancel_selection": ("cancel_selection", None, {}),
    "bundle_select": ("select_bundle", None, {"bundle_index": "index"}),
    "bundle_confirm_selection": ("confirm_bundle_selection", None, {}),
    "bundle_cancel_selection": ("cancel_bundle_selection", None, {}),
    "relic_select": ("select_relic", None, {"relic_index": "index"}),
    "relic_skip": ("skip_relic_selection", None, {}),
    "treasure_claim_relic": ("claim_treasure_relic", None, {"relic_index": "index"}),
    "crystal_sphere_set_tool": ("crystal_sphere_set_tool", None, {"tool": "tool"}),
    "crystal_sphere_click_cell": (
        "crystal_sphere_click_cell",
        None,
        {"x": "x", "y": "y"},
    ),
    "crystal_sphere_proceed": ("crystal_sphere_proceed", None, {}),
    "mp_combat_play_card": ("play_card", "multiplayer", {"card_index": "card_index", "target": "target"}),
    "mp_combat_end_turn": ("end_turn", "multiplayer", {}),
    "mp_combat_undo_end_turn": ("undo_end_turn", "multiplayer", {}),
    "mp_use_potion": ("use_potion", "multiplayer", {"slot": "slot", "target": "target"}),
    "mp_discard_potion": ("discard_potion", "multiplayer", {"slot": "slot"}),
    "mp_map_vote": ("choose_map_node", "multiplayer", {"node_index": "index"}),
    "mp_event_choose_option": ("choose_event_option", "multiplayer", {"option_index": "index"}),
    "mp_event_advance_dialogue": ("advance_dialogue", "multiplayer", {}),
    "mp_rest_choose_option": ("choose_rest_option", "multiplayer", {"option_index": "index"}),
    "mp_shop_purchase": ("shop_purchase", "multiplayer", {"item_index": "index"}),
    "mp_rewards_claim": ("claim_reward", "multiplayer", {"reward_index": "index"}),
    "mp_rewards_pick_card": ("select_card_reward", "multiplayer", {"card_index": "card_index"}),
    "mp_rewards_skip_card": ("skip_card_reward", "multiplayer", {}),
    "mp_proceed_to_map": ("proceed", "multiplayer", {}),
    "mp_deck_select_card": ("select_card", "multiplayer", {"card_index": "index"}),
    "mp_deck_confirm_selection": ("confirm_selection", "multiplayer", {}),
    "mp_deck_cancel_selection": ("cancel_selection", "multiplayer", {}),
    "mp_bundle_select": ("select_bundle", "multiplayer", {"bundle_index": "index"}),
    "mp_bundle_confirm_selection": ("confirm_bundle_selection", "multiplayer", {}),
    "mp_bundle_cancel_selection": ("cancel_bundle_selection", "multiplayer", {}),
    "mp_combat_select_card": ("combat_select_card", "multiplayer", {"card_index": "card_index"}),
    "mp_combat_confirm_selection": ("combat_confirm_selection", "multiplayer", {}),
    "mp_relic_select": ("select_relic", "multiplayer", {"relic_index": "index"}),
    "mp_relic_skip": ("skip_relic_selection", "multiplayer", {}),
    "mp_treasure_claim_relic": ("claim_treasure_relic", "multiplayer", {"relic_index": "index"}),
    "mp_crystal_sphere_set_tool": ("crystal_sphere_set_tool", "multiplayer", {"tool": "tool"}),
    "mp_crystal_sphere_click_cell": (
        "crystal_sphere_click_cell",
        "multiplayer",
        {"x": "x", "y": "y"},
    ),
    "mp_crystal_sphere_proceed": ("crystal_sphere_proceed", "multiplayer", {}),
}


def normalize_mcp_action_alias(planned: dict[str, Any]) -> dict[str, Any]:
    action = planned.get("action")
    if not isinstance(action, str) or action not in MCP_ACTION_ALIASES:
        return planned
    http_action, endpoint, param_map = MCP_ACTION_ALIASES[action]
    normalized = {k: v for k, v in planned.items() if k != "action"}
    normalized["action"] = http_action
    if endpoint is not None:
        normalized["_endpoint"] = endpoint
        normalized.setdefault("_wait", False)
    for source_key, dest_key in param_map.items():
        if source_key in planned and dest_key not in normalized:
            normalized[dest_key] = planned[source_key]
    return normalized


def normalize_action(action: Any) -> dict[str, Any]:
    if isinstance(action, str):
        return normalize_mcp_action_alias({"action": action})
    if not isinstance(action, dict):
        raise RuntimeError(f"Action must be a string or object, got {type(action).__name__}")
    if "action" in action:
        return normalize_mcp_action_alias(dict(action))
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
        if key in {"menu", "menu_select"}:
            return {"action": "menu_select", "option": value}
        single_index_aliases = {
            "reward",
            "map",
            "event",
            "rest",
            "shop",
            "deck_pick",
            "card_select_pick",
            "hand_pick",
            "bundle_pick",
            "pick_card",
            "select_card_reward",
            "combat_select_card",
            "select_hand_card",
            "hand_select_card",
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


def expand_action_macros(actions: list[Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for raw_action in actions:
        planned = normalize_action(raw_action)
        action = planned.get("action")
        if action in {"deck_pick", "card_select_pick"}:
            index = planned.get("index")
            if index is None:
                raise RuntimeError(f"{action} needs 'index'")
            expanded.append(
                {
                    "action": "select_card",
                    "index": index,
                    "_macro": action,
                    "_macro_select_state": "card_select",
                }
            )
            expanded.append(
                {
                    "action": "confirm_selection",
                    "_macro": action,
                    "_macro_confirm_state": "card_select",
                }
            )
            continue
        if action == "hand_pick":
            index = planned.get("index")
            if index is None:
                raise RuntimeError("hand_pick needs 'index'")
            expanded.append(
                {
                    "action": "combat_select_card",
                    "card_index": index,
                    "_macro": action,
                    "_macro_select_state": "hand_select",
                }
            )
            expanded.append(
                {
                    "action": "combat_confirm_selection",
                    "_macro": action,
                    "_macro_confirm_state": "hand_select",
                }
            )
            continue
        if action == "bundle_pick":
            index = planned.get("index")
            if index is None:
                raise RuntimeError("bundle_pick needs 'index'")
            expanded.append(
                {
                    "action": "select_bundle",
                    "index": index,
                    "_macro": action,
                    "_macro_select_state": "bundle_select",
                }
            )
            expanded.append(
                {
                    "action": "confirm_bundle_selection",
                    "_macro": action,
                    "_macro_confirm_state": "bundle_select",
                }
            )
            continue
        expanded.append(planned)
    return expanded


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
        "return_to_menu": ("return_to_menu", None),
        "undo_end_turn": ("undo_end_turn", None),
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
        "select_hand_card": ("combat_select_card", "card_index"),
        "hand_select_card": ("combat_select_card", "card_index"),
        "hand_select": ("combat_select_card", "card_index"),
        "combat_confirm_selection": ("combat_confirm_selection", None),
        "confirm_hand_selection": ("combat_confirm_selection", None),
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
        "menu_select": ("menu_select", "option"),
        "menu": ("menu_select", "option"),
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
    if http_action == "menu_select" and planned.get("seed") is not None:
        body["seed"] = planned["seed"]
    return body, None


def validate_action_allowed_in_state(state: dict[str, Any], body: dict[str, Any]) -> None:
    combat_only_actions = {
        "play_card": "play cards",
        "end_turn": "end the turn",
        "use_potion": "use potions",
    }
    action = body.get("action")
    if action not in combat_only_actions or is_combat_state(state):
        return
    point = decision_point(state)
    point_kind = point.get("kind") or state.get("state_type") or "unknown"
    raise RuntimeError(
        f"Cannot {combat_only_actions[action]} while state_type={state.get('state_type')!r} "
        f"({point_kind}); resolve the current decision screen first."
    )


def hand_signature(state: dict[str, Any]) -> list[tuple[Any, Any]]:
    return [(card.get("index"), card.get("name")) for card in hand(state)]


def state_digest_key(state: dict[str, Any]) -> str:
    return json.dumps(state_digest(state), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def adaptive_poll_delay(poll: int, poll_delay: float) -> float:
    if poll_delay != DEFAULT_POLL_DELAY:
        return poll_delay
    if poll < 4:
        return DEFAULT_INITIAL_POLL_DELAY
    if poll < 8:
        return DEFAULT_POLL_DELAY * 0.5
    return DEFAULT_POLL_DELAY


def sleep_for_poll(poll: int, poll_delay: float) -> None:
    delay = adaptive_poll_delay(poll, poll_delay)
    if delay > 0:
        time.sleep(delay)


def card_obj_for_body(state: dict[str, Any], body: dict[str, Any]) -> dict[str, Any] | None:
    card_index = body.get("card_index")
    for card in hand(state):
        if card.get("index") == card_index:
            return card
    return None


def card_play_needs_delayed_settle(state_before: dict[str, Any], body: dict[str, Any]) -> bool:
    card = card_obj_for_body(state_before, body)
    if card is None:
        return False
    name = str(card.get("name") or "").casefold()
    description = str(card.get("description") or "").casefold()
    card_type = str(card.get("type") or "").casefold()
    delayed_words = (
        "draw",
        "put ",
        "add ",
        "random",
        "whenever",
        "at the end",
        "play ",
        "transform",
        "exhaust your hand",
    )
    known_delayed_names = {
        "battle trance",
        "brand",
        "hellraiser",
        "howl from beyond",
        "offering",
        "pommel strike",
        "shrug it off",
        "stampede",
        "thinking ahead",
        "vicious",
    }
    if card_type == "power" or any(fragment in name for fragment in known_delayed_names):
        return True
    if any(word in description for word in delayed_words):
        return True
    for status in (state_before.get("player") or {}).get("status") or []:
        if isinstance(status, dict) and str(status.get("id") or status.get("name") or "").casefold() in {
            "hellraiser_power",
            "hellraiser",
            "stampede_power",
            "stampede",
            "vicious_power",
            "vicious",
        }:
            return True
    return False


def card_play_may_open_selection(state_before: dict[str, Any], body: dict[str, Any]) -> bool:
    card = card_obj_for_body(state_before, body)
    if card is None:
        return False
    name = str(card.get("name") or "").casefold()
    description = str(card.get("description") or "").casefold()
    if "thinking ahead" in name:
        return True
    selection_words = ("choose", "select")
    selection_phrases = (
        "put 1 card",
        "put a card",
        "from your hand",
        "on top of your draw",
        "return a card",
    )
    return any(word in description for word in selection_words) or any(
        phrase in description for phrase in selection_phrases
    )


def card_play_extra_settle_polls(state_before: dict[str, Any], body: dict[str, Any]) -> int:
    if not card_play_needs_delayed_settle(state_before, body):
        return 0
    extra = DELAYED_CARD_SETTLE_POLLS
    if card_play_may_open_selection(state_before, body):
        extra += SELECTION_CARD_SETTLE_POLLS
    return extra


def begin_wait(client: Any, logger: JsonlLogger) -> str:
    wait_id = logger.next_wait_id()
    try:
        client.active_wait_id = wait_id
    except Exception:
        pass
    return wait_id


def end_wait(client: Any, wait_id: str) -> None:
    try:
        if getattr(client, "active_wait_id", None) == wait_id:
            client.active_wait_id = None
    except Exception:
        pass


def log_wait(logger: JsonlLogger, *, started: float, reason: str, polls: int, wait_id: str | None = None) -> None:
    logger.write(
        "wait",
        wait_id=wait_id,
        reason=reason,
        polls=polls,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )


def wait_for_card_play_applied(
    client: Any,
    state_before: dict[str, Any],
    *,
    before_sig: list[tuple[Any, Any]],
    base_settle_polls: int = READY_STATE_SETTLE_POLLS,
    extra_settle_polls: int = 0,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    settling_state: dict[str, Any] | None = None
    settling_reason: str | None = None
    stable_polls = 0
    for poll in range(max_polls):
        state = client.state()
        candidate_reason: str | None = None
        needed_polls = READY_STATE_SETTLE_POLLS
        if state.get("state_type") != state_before.get("state_type"):
            if not is_transient_state(state):
                candidate_reason = "card_play_state_changed"
        elif not is_combat_state(state):
            if not is_transient_state(state):
                candidate_reason = "card_play_left_combat"
        elif hand_signature(state) != before_sig and not is_transient_state(state):
            candidate_reason = "card_play_extra_settled" if extra_settle_polls else "card_play_settled"
            needed_polls = base_settle_polls + extra_settle_polls

        if candidate_reason is not None:
            if (
                settling_state is not None
                and settling_reason == candidate_reason
                and state_digest_key(state) == state_digest_key(settling_state)
            ):
                stable_polls += 1
            else:
                settling_state = state
                settling_reason = candidate_reason
                stable_polls = 1
            if stable_polls >= needed_polls:
                suffix = "_settled" if candidate_reason in {"card_play_state_changed", "card_play_left_combat"} else ""
                finish(f"{candidate_reason}{suffix}", poll + 1)
                return state
        else:
            settling_state = None
            settling_reason = None
            stable_polls = 0
        sleep_for_poll(poll, poll_delay)
    finish("card_play_timeout", max_polls)
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
    wait_id = begin_wait(client, logger)

    def finish(reason_name: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason_name, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    before_key = state_digest_key(state_before)
    last_state = state_before
    settling_state: dict[str, Any] | None = None
    settling_reason: str | None = None
    stable_polls = 0
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) == before_key or is_transient_state(state):
            settling_state = None
            settling_reason = None
            stable_polls = 0
            sleep_for_poll(poll, poll_delay)
            continue

        candidate_reason = f"{reason}_state_changed"
        needed_polls = READY_STATE_SETTLE_POLLS
        if is_combat_state(state):
            if not is_ready_combat_decision(state):
                settling_state = None
                settling_reason = None
                stable_polls = 0
                sleep_for_poll(poll, poll_delay)
                continue
            candidate_reason = f"{reason}_combat_ready"
            needed_polls = MAP_COMBAT_SETTLE_POLLS

        if (
            settling_state is not None
            and settling_reason == candidate_reason
            and state_digest_key(state) == state_digest_key(settling_state)
        ):
            stable_polls += 1
        else:
            settling_state = state
            settling_reason = candidate_reason
            stable_polls = 1
        if stable_polls >= needed_polls:
            suffix = "_settled" if needed_polls > 1 else ""
            finish(f"{candidate_reason}{suffix}", poll + 1)
            return state
        sleep_for_poll(poll, poll_delay)
    finish(f"{reason}_state_unchanged", max_polls)
    raise RuntimeError(f"Timed out waiting for {reason} state change after {max_polls} polls")


def wait_for_main_menu(
    client: Any,
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    wait_id = begin_wait(client, logger)

    def finish(reason_name: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason_name, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    last_state: dict[str, Any] = {}
    settling_state: dict[str, Any] | None = None
    stable_polls = 0
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if (
            state.get("state_type") == "menu"
            and state.get("menu_screen") == "main"
            and menu_option_names(state, enabled_only=False)
        ):
            if settling_state is not None and state_digest_key(state) == state_digest_key(settling_state):
                stable_polls += 1
            else:
                settling_state = state
                stable_polls = 1
            if stable_polls >= READY_STATE_SETTLE_POLLS:
                finish("return_to_menu_main_menu_settled", poll + 1)
                return state
        else:
            settling_state = None
            stable_polls = 0
        sleep_for_poll(poll, poll_delay)

    finish("return_to_menu_main_menu_timeout", max_polls)
    raise RuntimeError(
        "Timed out waiting for return_to_menu to reach the main menu after "
        f"{max_polls} polls; last state_type={last_state.get('state_type')!r}, "
        f"menu_screen={last_state.get('menu_screen')!r}"
    )


def modal_can_confirm(state: dict[str, Any], expected_state_type: str) -> bool:
    modal = state.get(expected_state_type)
    return isinstance(modal, dict) and modal.get("can_confirm") is True


def wait_for_modal_selection_result(
    client: Any,
    state_before: dict[str, Any],
    *,
    expected_state_type: str,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    before_key = state_digest_key(state_before)
    settling_state: dict[str, Any] | None = None
    settling_reason: str | None = None
    stable_polls = 0
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) == before_key or is_transient_state(state):
            settling_state = None
            settling_reason = None
            stable_polls = 0
            sleep_for_poll(poll, poll_delay)
            continue

        candidate_reason: str | None = None
        needed_polls = READY_STATE_SETTLE_POLLS
        if state.get("state_type") == expected_state_type:
            if modal_can_confirm(state, expected_state_type):
                candidate_reason = f"{expected_state_type}_can_confirm"
        else:
            candidate_reason = f"{expected_state_type}_resolved"
            if is_combat_state(state):
                if not is_ready_combat_decision(state):
                    candidate_reason = None
                else:
                    needed_polls = MAP_COMBAT_SETTLE_POLLS

        if candidate_reason is not None:
            if (
                settling_state is not None
                and settling_reason == candidate_reason
                and state_digest_key(state) == state_digest_key(settling_state)
            ):
                stable_polls += 1
            else:
                settling_state = state
                settling_reason = candidate_reason
                stable_polls = 1
            if stable_polls >= needed_polls:
                suffix = "_settled" if needed_polls > 1 else ""
                finish(f"{candidate_reason}{suffix}", poll + 1)
                return state
        else:
            settling_state = None
            settling_reason = None
            stable_polls = 0
        sleep_for_poll(poll, poll_delay)

    state_type = last_state.get("state_type")
    finish(f"{expected_state_type}_selection_timeout", max_polls)
    raise RuntimeError(
        f"Timed out waiting for {expected_state_type} selection after {max_polls} polls; "
        f"last state_type={state_type!r}"
    )


def wait_for_map_node_transition(
    client: Any,
    state_before: dict[str, Any],
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    last_state = state_before
    settling_state: dict[str, Any] | None = None
    settling_reason: str | None = None
    stable_polls = 0
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        candidate_reason: str | None = None
        needed_polls = READY_STATE_SETTLE_POLLS
        if state.get("state_type") != "map" and not is_transient_state(state):
            if is_combat_state(state):
                if is_ready_combat_decision(state):
                    candidate_reason = "map_node_combat_ready"
                    needed_polls = MAP_COMBAT_SETTLE_POLLS
            else:
                candidate_reason = "map_node_state_changed"

        if candidate_reason is not None:
            if (
                settling_state is not None
                and settling_reason == candidate_reason
                and state_digest_key(state) == state_digest_key(settling_state)
            ):
                stable_polls += 1
            else:
                settling_state = state
                settling_reason = candidate_reason
                stable_polls = 1
            if stable_polls >= needed_polls:
                suffix = "_settled" if needed_polls > 1 else ""
                finish(f"{candidate_reason}{suffix}", poll + 1)
                return state
        else:
            settling_state = None
            settling_reason = None
            stable_polls = 0
        sleep_for_poll(poll, poll_delay)
    finish("map_node_still_on_map", max_polls)
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
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    last_state: dict[str, Any] = {}
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if not is_transient_state(state):
            finish("ready_state", poll + 1)
            return state
        sleep_for_poll(poll, poll_delay)
    state_type = last_state.get("state_type")
    finish("ready_state_timeout", max_polls)
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
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    before_key = state_digest_key(state_before)
    before_battle = state_before.get("battle") or {}
    before_round = before_battle.get("round")
    seen_change = False
    ready_state: dict[str, Any] | None = None
    ready_reason: str | None = None
    ready_stable_polls = 0
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) != before_key:
            seen_change = True
        if not seen_change:
            sleep_for_poll(poll, poll_delay)
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
            needed_polls = (
                START_TURN_SETTLE_POLLS
                if candidate_reason == "end_turn_next_player_turn"
                else READY_STATE_SETTLE_POLLS
            )
            if (
                ready_state is not None
                and ready_reason == candidate_reason
                and state_digest_key(state) == state_digest_key(ready_state)
            ):
                ready_stable_polls += 1
            else:
                ready_state = state
                ready_reason = candidate_reason
                ready_stable_polls = 1
            if ready_stable_polls >= needed_polls:
                finish(f"{ready_reason}_settled", poll + 1)
                return state
        else:
            ready_state = None
            ready_reason = None
            ready_stable_polls = 0
        sleep_for_poll(poll, poll_delay)
    state_type = last_state.get("state_type")
    finish("end_turn_timeout", max_polls)
    raise RuntimeError(
        f"Timed out waiting for end turn after {max_polls} polls; last state_type={state_type!r}"
    )


def wait_for_potion_resolution(
    client: Any,
    state_before: dict[str, Any],
    *,
    action_body: dict[str, Any],
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    wait_id = begin_wait(client, logger)

    def finish(reason: str, polls: int) -> None:
        log_wait(logger, started=started, reason=reason, polls=polls, wait_id=wait_id)
        end_wait(client, wait_id)

    before_key = state_digest_key(state_before)
    may_open_modal = potion_may_open_modal(state_before, action_body)
    settling_state: dict[str, Any] | None = None
    settling_reason: str | None = None
    stable_polls = 0
    last_state = state_before
    for poll in range(max_polls):
        state = client.state()
        last_state = state
        if state_digest_key(state) == before_key or is_transient_state(state):
            settling_state = None
            settling_reason = None
            stable_polls = 0
            sleep_for_poll(poll, poll_delay)
            continue

        candidate_reason: str | None = None
        if is_modal_decision_state(state):
            candidate_reason = "potion_modal_state"
        elif state.get("state_type") != state_before.get("state_type"):
            candidate_reason = "potion_state_changed"
        elif is_combat_state(state):
            if is_ready_combat_decision(state):
                candidate_reason = "potion_combat_settled"
        else:
            candidate_reason = "potion_ready_state"

        if candidate_reason is None:
            settling_state = None
            settling_reason = None
            stable_polls = 0
            sleep_for_poll(poll, poll_delay)
            continue

        if settling_state is not None and state_digest_key(state) == state_digest_key(settling_state):
            stable_polls += 1
        else:
            settling_state = state
            settling_reason = candidate_reason
            stable_polls = 1

        needed_polls = 2
        if settling_reason == "potion_combat_settled":
            needed_polls += DELAYED_CARD_SETTLE_POLLS
            if may_open_modal:
                needed_polls += SELECTION_POTION_SETTLE_POLLS
        if stable_polls >= needed_polls:
            finish(f"{settling_reason}_settled", poll + 1)
            return state
        sleep_for_poll(poll, poll_delay)

    state_type = last_state.get("state_type")
    finish("potion_timeout", max_polls)
    raise RuntimeError(
        f"Timed out waiting for potion resolution after {max_polls} polls; last state_type={state_type!r}"
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
        "select_hand_card",
        "confirm_hand_selection",
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
        "menu_select",
        "undo_end_turn",
        "return_to_menu",
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
    fast_action_waits: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    executed: list[dict[str, Any]] = []
    final_state: dict[str, Any] = {}
    planned_actions = expand_action_macros(actions)

    for action_index, planned in enumerate(planned_actions):
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

        if planned.get("_endpoint") is not None:
            try:
                client.endpoint = str(planned["_endpoint"])
            except Exception:
                pass

        if planned.get("_macro_confirm_state") is not None:
            expected_state_type = str(planned["_macro_confirm_state"])
            current = final_state if final_state else client.state()
            if current.get("state_type") != expected_state_type:
                executed.append(
                    {
                        "planned": planned,
                        "skipped": True,
                        "reason": f"{planned.get('_macro')} selection already resolved",
                    }
                )
                final_state = current
                continue
            if not modal_can_confirm(current, expected_state_type):
                raise RuntimeError(
                    f"{planned.get('_macro')} cannot confirm because "
                    f"{expected_state_type} is not confirmable"
                )

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
        validate_action_allowed_in_state(state_before, body)
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
        extra_card_settle_polls = (
            card_play_extra_settle_polls(state_before, body)
            if body.get("action") == "play_card"
            else 0
        )
        base_card_settle_polls = (
            FAST_CARD_SETTLE_POLLS
            if (
                fast_action_waits
                and action_index == len(planned_actions) - 1
                and extra_card_settle_polls == 0
            )
            else READY_STATE_SETTLE_POLLS
        )
        client.post(body)
        executed.append({"planned": planned, "body": body})

        should_wait = planned.get("_wait") is not False

        if not should_wait:
            final_state = client.state()
        elif body.get("action") == "play_card" and state_before is not None:
            final_state = wait_for_card_play_applied(
                client,
                state_before,
                before_sig=before_sig,
                base_settle_polls=base_card_settle_polls,
                extra_settle_polls=extra_card_settle_polls,
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
        elif body.get("action") == "use_potion":
            final_state = wait_for_potion_resolution(
                client,
                state_before,
                action_body=body,
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
        elif body.get("action") == "return_to_menu":
            final_state = wait_for_main_menu(
                client,
                logger=logger,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        elif planned.get("_macro_select_state") is not None:
            final_state = wait_for_modal_selection_result(
                client,
                state_before,
                expected_state_type=str(planned["_macro_select_state"]),
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


def execute_menu_option(
    client: Any,
    option: str,
    *,
    logger: JsonlLogger,
    stats: RunStats,
    seed: str | None = None,
    wait: bool = True,
    require_change: bool = False,
    max_polls: int,
    poll_delay: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state_before = deepcopy(client.state())
    body: dict[str, Any] = {"action": "menu_select", "option": option}
    if seed is not None:
        body["seed"] = seed
    stats.actions += 1
    logger.write(
        "planned_action",
        planned={"action": "menu_select", "option": option, "seed": seed},
        body=body,
        state_type=state_before.get("state_type"),
        before=state_digest(state_before),
        decision_point=decision_point(state_before),
    )
    post_result = client.post(body)
    if wait:
        try:
            state_after = wait_for_state_change(
                client,
                state_before,
                logger=logger,
                reason="menu",
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
        except RuntimeError:
            if require_change:
                raise
            state_after = client.state()
    else:
        state_after = client.state()
    logger.write(
        "action_result",
        planned={"action": "menu_select", "option": option, "seed": seed},
        body=body,
        before=state_digest(state_before),
        after=state_digest(state_after),
        delta=state_delta(state_before, state_after),
        decision_point=decision_point(state_after),
    )
    return state_after, {
        "planned": {"action": "menu_select", "option": option},
        "body": body,
        "result": post_result,
    }


def choose_character_option(state: dict[str, Any], requested: str) -> str:
    wanted = requested.casefold()
    characters = [
        character
        for character in state.get("characters") or []
        if isinstance(character, dict) and character.get("locked") is not True
    ]
    if wanted == "first":
        if characters:
            return str(characters[0].get("id") or characters[0].get("name"))
        names = [
            name
            for name in menu_option_names(state)
            if name not in {"confirm", "embark", "back", "unready"}
        ]
        if names:
            return names[0]
    for character in characters:
        values = [
            str(character.get("id") or ""),
            str(character.get("name") or ""),
        ]
        if any(value.casefold() == wanted for value in values):
            return str(character.get("id") or character.get("name"))
    if menu_option_enabled(state, requested):
        return requested
    available = ", ".join(
        str(character.get("id") or character.get("name"))
        for character in characters
        if character.get("id") or character.get("name")
    )
    if not available:
        available = ", ".join(menu_option_names(state))
    raise RuntimeError(f"Character {requested!r} is not available. Available: {available}")


def start_run(
    client: Any,
    *,
    logger: JsonlLogger,
    stats: RunStats,
    mode: str,
    character: str,
    seed: str | None,
    max_steps: int,
    max_polls: int,
    poll_delay: float,
    auto_reveal_timeline: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    executed: list[dict[str, Any]] = []
    state = client.state()
    attempted_timeline_auto_reveal = False
    for _ in range(max_steps):
        state_type = state.get("state_type")
        if state_type not in {"menu", "game_over"}:
            if executed:
                return state, executed
            raise RuntimeError(
                f"Cannot start a run while state_type={state_type!r}; "
                "return to the main menu or game-over screen first."
            )
        if state_type == "game_over":
            state, step = execute_menu_option(
                client,
                "main_menu",
                logger=logger,
                stats=stats,
                require_change=True,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
            executed.append(step)
            continue

        screen = state.get("menu_screen")
        if screen == "main":
            if not menu_option_enabled(state, "singleplayer"):
                pending_epoch_ids = manual_timeline_reveal_pending_epoch_ids(state)
                if pending_epoch_ids is not None:
                    timeline_status = timeline_status_data(client)
                    slot_unlock_epoch_ids = pending_slot_unlock_epoch_ids_from_status(timeline_status)
                    if slot_unlock_epoch_ids:
                        raise RuntimeError(
                            "Cannot start a run because Timeline has slot-unlock epochs that "
                            "cannot be auto-revealed safely; "
                            f"pending_slot_unlock_epoch_ids={slot_unlock_epoch_ids}. "
                            "Use `timeline status` and resolve the Timeline unlock side effects "
                            "before starting a run."
                        )
                    if auto_reveal_timeline:
                        if attempted_timeline_auto_reveal:
                            raise RuntimeError(
                                "Timeline auto reveal did not clear the main-menu blocker; "
                                f"pending_epoch_ids={pending_epoch_ids}. "
                                "Run `timeline status` and reveal manually in game if needed."
                            )
                        attempted_timeline_auto_reveal = True
                        body = {"action": "reveal_pending", "dry_run": False}
                        result = reveal_pending_timeline_epochs(client)
                        step = {
                            "planned": {"action": "timeline_reveal_pending"},
                            "body": body,
                            "result": result,
                        }
                        logger.write(
                            "data_result",
                            path="/api/v1/timeline",
                            action="reveal_pending",
                            data=result,
                        )
                        executed.append(step)
                        state = client.state()
                        remaining_epoch_ids = manual_timeline_reveal_pending_epoch_ids(state)
                        if remaining_epoch_ids is not None:
                            raise RuntimeError(
                                "Timeline auto reveal did not clear the main-menu blocker; "
                                f"pending_epoch_ids={remaining_epoch_ids}. "
                                "Run `timeline status` and reveal manually in game if needed."
                            )
                        continue
                    raise RuntimeError(
                        "Cannot start a run because Timeline has epochs that require "
                        "manual Timeline reveal in game before singleplayer is available; "
                        f"pending_epoch_ids={pending_epoch_ids}. "
                        "Run `timeline reveal` or pass `--auto-reveal-timeline`."
                    )
                raise RuntimeError(
                    "Main menu does not expose an enabled singleplayer option; "
                    f"available options: {menu_option_names(state)}"
                )
            state, step = execute_menu_option(
                client,
                "singleplayer",
                logger=logger,
                stats=stats,
                require_change=True,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
            executed.append(step)
            continue

        if screen == "singleplayer":
            if not menu_option_enabled(state, mode):
                raise RuntimeError(
                    f"Singleplayer mode {mode!r} is not enabled; "
                    f"available options: {menu_option_names(state)}"
                )
            state, step = execute_menu_option(
                client,
                mode,
                logger=logger,
                stats=stats,
                require_change=True,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
            executed.append(step)
            continue

        if screen == "character_select":
            if menu_option_enabled(state, "confirm") or menu_option_enabled(state, "embark"):
                option = "confirm" if menu_option_enabled(state, "confirm") else "embark"
                state, step = execute_menu_option(
                    client,
                    option,
                    seed=seed,
                    logger=logger,
                    stats=stats,
                    require_change=True,
                    max_polls=max_polls,
                    poll_delay=poll_delay,
                )
                executed.append(step)
                continue
            option = choose_character_option(state, character)
            state, step = execute_menu_option(
                client,
                option,
                logger=logger,
                stats=stats,
                require_change=True,
                max_polls=max_polls,
                poll_delay=poll_delay,
            )
            executed.append(step)
            continue

        raise RuntimeError(
            f"Cannot start a run from menu_screen={screen!r}; "
            f"available options: {menu_option_names(state)}"
        )

    raise RuntimeError(f"Timed out starting a run after {max_steps} menu steps")


def switch_profile(
    client: STS2Client,
    profile_id: int,
    *,
    logger: JsonlLogger,
    max_polls: int,
    poll_delay: float,
) -> dict[str, Any]:
    previous_endpoint = client.endpoint
    client.endpoint = "singleplayer"
    body = {"action": "switch", "profile_id": profile_id}
    try:
        result = client.post_json("/api/v1/profiles", body)
        message = result.get("message")
        if isinstance(message, str) and message.startswith("Opened profile screen"):
            for poll in range(max_polls):
                sleep_for_poll(poll, poll_delay)
                state = client.state()
                if state.get("state_type") == "menu" and state.get("menu_screen") == "profile_select":
                    result = client.post_json("/api/v1/profiles", body)
                    break
        wait_id = begin_wait(client, logger)
        started = time.perf_counter()
        last_profiles: dict[str, Any] | None = None
        try:
            for poll in range(max_polls):
                sleep_for_poll(poll, poll_delay)
                profiles = client.get_json("/api/v1/profiles")
                last_profiles = profiles if isinstance(profiles, dict) else None
                if isinstance(profiles, dict) and profiles.get("current_profile_id") == profile_id:
                    log_wait(
                        logger,
                        started=started,
                        reason="profile_switched",
                        polls=poll + 1,
                        wait_id=wait_id,
                    )
                    return {
                        "status": "ok",
                        "message": f"Switched to profile {profile_id}",
                        "current_profile_id": profile_id,
                        "profiles": profiles.get("profiles", []),
                        "initial_response": result,
                    }
            log_wait(
                logger,
                started=started,
                reason="profile_switch_timeout",
                polls=max_polls,
                wait_id=wait_id,
            )
            return {
                "status": "error",
                "error": f"Timed out waiting for profile {profile_id} to become active",
                "current_profile_id": (
                    last_profiles.get("current_profile_id") if isinstance(last_profiles, dict) else None
                ),
                "profiles": last_profiles.get("profiles", []) if isinstance(last_profiles, dict) else [],
                "initial_response": result,
            }
        finally:
            end_wait(client, wait_id)
    finally:
        client.endpoint = previous_endpoint


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
    logger: JsonlLogger | None = None,
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
    stats.stdout_writes += 1
    payload = ""
    for _ in range(4):
        result["summary"] = {
            **stats.as_dict(),
            "log_path": str(log_path) if log_path is not None else None,
        }
        payload = _json_dumps(result, indent=indent)
        stdout_bytes = len(payload.encode("utf-8")) + 1
        stdout_lines = payload.count("\n") + 1
        if stats.stdout_bytes == stdout_bytes and stats.stdout_lines == stdout_lines:
            break
        stats.stdout_bytes = stdout_bytes
        stats.stdout_lines = stdout_lines
    if logger is not None:
        logger.write(
            "stdout",
            bytes=stats.stdout_bytes,
            lines=stats.stdout_lines,
            writes=stats.stdout_writes,
            ok=ok,
        )
    print(payload)


def emit_data_result(
    *,
    ok: bool,
    stats: RunStats,
    log_path: Path | None,
    logger: JsonlLogger | None = None,
    data: Any | None = None,
    text: str | None = None,
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
    if data is not None:
        result["data"] = data
    if text is not None:
        result["text"] = text
    stats.stdout_writes += 1
    payload = ""
    for _ in range(4):
        result["summary"] = {
            **stats.as_dict(),
            "log_path": str(log_path) if log_path is not None else None,
        }
        payload = _json_dumps(result, indent=indent)
        stdout_bytes = len(payload.encode("utf-8")) + 1
        stdout_lines = payload.count("\n") + 1
        if stats.stdout_bytes == stdout_bytes and stats.stdout_lines == stdout_lines:
            break
        stats.stdout_bytes = stdout_bytes
        stats.stdout_lines = stdout_lines
    if logger is not None:
        logger.write(
            "stdout",
            bytes=stats.stdout_bytes,
            lines=stats.stdout_lines,
            writes=stats.stdout_writes,
            ok=ok,
        )
    print(payload)


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def output_log_path(value: str | None) -> Path | None:
    if value is None:
        return _default_log_path()
    path = Path(value)
    if not path.is_absolute():
        path = _repo_root() / path
    return path


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
        wait_events = [event for event in ordered_events if event.get("kind") == "wait"]
        stdout_events = [event for event in ordered_events if event.get("kind") == "stdout"]
        http_total_ms = round(sum(float(event.get("elapsed_ms") or 0) for event in http_events), 1)
        wait_total_ms = round(sum(float(event.get("elapsed_ms") or 0) for event in wait_events), 1)
        wait_ids = {event.get("wait_id") for event in wait_events if event.get("wait_id")}
        wait_http_total_ms = round(
            sum(
                float(event.get("elapsed_ms") or 0)
                for event in http_events
                if event.get("wait_id") in wait_ids
            ),
            1,
        )
        wait_non_http_ms = round(max(0.0, wait_total_ms - wait_http_total_ms), 1)
        active_wall_time_ms = event_span_ms(ordered_events)
        local_overhead_ms = (
            round(max(0.0, active_wall_time_ms - http_total_ms - wait_non_http_ms), 1)
            if active_wall_time_ms is not None
            else None
        )
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
                "git_sha": run_start.get("git_sha"),
                "cwd": run_start.get("cwd"),
                "base_url": run_start.get("base_url"),
                "timeout": run_start.get("timeout"),
                "poll_delay": run_start.get("poll_delay"),
                "max_polls": run_start.get("max_polls"),
                "start_ts": start_ts.isoformat(timespec="milliseconds"),
                "end_ts": end_ts.isoformat(timespec="milliseconds"),
                "active_wall_time_ms": active_wall_time_ms,
                "http_total_ms": http_total_ms,
                "wait_elapsed_ms": wait_total_ms,
                "wait_http_overlap_ms": wait_http_total_ms,
                "wait_non_http_ms": wait_non_http_ms,
                "local_overhead_ms": local_overhead_ms,
                "http_calls": len(http_events),
                "get_calls": sum(1 for event in http_events if event.get("method") == "GET"),
                "post_calls": len(post_events),
                "stdout_bytes": sum(int(event.get("bytes") or 0) for event in stdout_events)
                or run_end.get("stdout_bytes"),
                "stdout_lines": sum(int(event.get("lines") or 0) for event in stdout_events)
                or run_end.get("stdout_lines"),
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
    stdout_events = [e for e in events if e.get("kind") == "stdout"]
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
    wait_ids = {e.get("wait_id") for e in wait_events if e.get("wait_id")}
    wait_elapsed_values = [
        float(e.get("elapsed_ms"))
        for e in wait_events
        if isinstance(e.get("elapsed_ms"), (int, float))
    ]
    wait_http_by_id: dict[str, dict[str, Any]] = {}
    for event in http_events:
        wait_id = event.get("wait_id")
        if not wait_id:
            continue
        bucket = wait_http_by_id.setdefault(str(wait_id), {"count": 0, "elapsed_ms": 0.0})
        bucket["count"] += 1
        bucket["elapsed_ms"] += float(event.get("elapsed_ms") or 0)
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
    http_total_ms = round(sum(float(e.get("elapsed_ms") or 0) for e in http_events), 1)
    wait_total_ms = round(sum(wait_elapsed_values), 1)
    wait_http_overlap_ms = round(
        sum(
            float(e.get("elapsed_ms") or 0)
            for e in http_events
            if e.get("wait_id") in wait_ids
        ),
        1,
    )
    wait_non_http_ms = round(max(0.0, wait_total_ms - wait_http_overlap_ms), 1)
    active_ms = active_wall_time_ms if active_wall_time_ms is not None else wall_time_ms
    local_overhead_ms = (
        round(max(0.0, active_ms - http_total_ms - wait_non_http_ms), 1)
        if active_ms is not None
        else None
    )

    decision_points = Counter(str(point.get("kind") or "unknown") for point in after_decision_sequence)

    result = {
        "path": str(paths[0]) if len(paths) == 1 else None,
        "paths": [str(path) for path in paths],
        "events": len(events),
        "event_kinds": dict(sorted(event_kinds.items())),
        "http_calls": len(http_events),
        "http_total_ms": http_total_ms,
        "wall_time_ms": wall_time_ms,
        "active_wall_time_ms": active_ms,
        "timing_breakdown": {
            "active_wall_time_ms": active_ms,
            "http_total_ms": http_total_ms,
            "wait_elapsed_ms": wait_total_ms,
            "wait_http_overlap_ms": wait_http_overlap_ms,
            "wait_non_http_ms": wait_non_http_ms,
            "local_overhead_ms": local_overhead_ms,
            "wait_http_correlation": bool(wait_ids),
        },
        "stdout": {
            "writes": len(stdout_events),
            "bytes": sum(int(e.get("bytes") or 0) for e in stdout_events),
            "lines": sum(int(e.get("lines") or 0) for e in stdout_events),
        },
        "actions": actions,
        "waits": {
            "count": len(wait_events),
            "total_polls": sum(int(e.get("polls") or 0) for e in wait_events),
            "elapsed_ms": _timing_distribution(wait_elapsed_values),
            "reasons": dict(sorted(wait_reasons.items())),
            "http_by_wait_id": {
                wait_id: {
                    "count": data["count"],
                    "elapsed_ms": round(data["elapsed_ms"], 1),
                }
                for wait_id, data in sorted(wait_http_by_id.items())
            },
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
            seen_matches: set[str] = set()
            unique_matches: list[str] = []
            for match in sorted(matches):
                match_key = str(Path(match).resolve())
                if match_key in seen_matches:
                    continue
                seen_matches.add(match_key)
                unique_matches.append(match)
            if not unique_matches:
                raise RuntimeError(f"Log pattern matched no files: {value}")
            paths.extend(Path(match) for match in unique_matches)
        else:
            paths.append(Path(value))
    if not paths:
        raise RuntimeError("No log paths provided")
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
    parser.add_argument("--multiplayer", action="store_true", help="Use /api/v1/multiplayer for run state/actions")

    sub = parser.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state", help="Print a concise game-state summary")
    state.add_argument("--drain", action="store_true", help="Auto-resolve trivial screens before printing")
    state.add_argument("--verbose", action="store_true")
    state.add_argument("--raw-format", choices=["json", "markdown"], default=None, help="Return the raw API state in the requested MCP-compatible format")

    sub.add_parser("map", help="Print the full current act map graph")

    drain = sub.add_parser("drain", help="Auto-resolve no-decision screens")
    drain.add_argument("--max-steps", type=int, default=30)
    drain.add_argument("--verbose", action="store_true")

    act = sub.add_parser("act", help="Execute a JSON action plan")
    act.add_argument("actions", help="JSON object/list, or @path/to/actions.json")
    act.add_argument("--no-auto-target", action="store_true")
    act.add_argument("--drain", action="store_true", help="Run trivial drain after each action")
    act.add_argument("--no-wait-end-turn", action="store_true")
    act.add_argument(
        "--fast-action-waits",
        action="store_true",
        help=(
            "Use shorter settle checks for simple in-combat card plays. "
            "Delayed/draw/modal/end-turn waits remain conservative."
        ),
    )
    act.add_argument("--max-polls", type=int, default=DEFAULT_MAX_POLLS)
    act.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    act.add_argument("--verbose", action="store_true")

    cards = sub.add_parser("cards", help="Play a sequence of cards by name in one CLI call")
    cards.add_argument("cards", nargs="+", help="Card names in play order")
    cards.add_argument("--target", default=None, help="first, lowest_hp, highest_hp, entity id, or omitted for auto")
    cards.add_argument("--end-turn", action="store_true")
    cards.add_argument("--drain", action="store_true")
    cards.add_argument(
        "--fast-action-waits",
        action="store_true",
        help=(
            "Use shorter settle checks for simple in-combat card plays. "
            "Delayed/draw/modal/end-turn waits remain conservative."
        ),
    )
    cards.add_argument("--max-polls", type=int, default=DEFAULT_MAX_POLLS)
    cards.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    cards.add_argument("--verbose", action="store_true")

    analyze = sub.add_parser("analyze-log", help="Summarize sts2-fast JSONL timing logs")
    analyze.add_argument("paths", nargs="+")

    menu = sub.add_parser("menu", help="Select a visible menu/game-over option")
    menu.add_argument("option")
    menu.add_argument("--seed", default=None)
    menu.add_argument("--no-wait", action="store_true", help="Return after the POST instead of waiting for visible state to change")
    menu.add_argument("--max-polls", type=int, default=DEFAULT_MENU_MAX_POLLS)
    menu.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    menu.add_argument("--verbose", action="store_true")

    return_menu = sub.add_parser("return-menu", help="Return an active run to the main menu without creating a new run save")
    return_menu.add_argument("--no-wait", action="store_true", help="Return after the POST instead of waiting for the menu state")
    return_menu.add_argument("--max-polls", type=int, default=DEFAULT_MENU_MAX_POLLS)
    return_menu.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    return_menu.add_argument("--verbose", action="store_true")

    start_run_parser = sub.add_parser("start-run", help="Start a standard singleplayer run from the menu")
    start_run_parser.add_argument("--mode", default="standard", choices=["standard", "daily", "custom"])
    start_run_parser.add_argument("--character", default="ironclad", help="Character id/name, or 'first'")
    start_run_parser.add_argument("--seed", default=None)
    start_run_parser.add_argument("--max-steps", type=int, default=8)
    start_run_parser.add_argument("--max-polls", type=int, default=DEFAULT_START_RUN_MAX_POLLS)
    start_run_parser.add_argument("--poll-delay", type=float, default=DEFAULT_POLL_DELAY)
    start_run_parser.add_argument(
        "--auto-reveal-timeline",
        action="store_true",
        help="Mark pending obtained Timeline epochs as revealed if they block the main menu",
    )
    start_run_parser.add_argument("--verbose", action="store_true")

    sub.add_parser("profile", help="Get active profile progress")
    timeline = sub.add_parser("timeline", help="Inspect or reveal pending Timeline unlocks")
    timeline.add_argument("action", nargs="?", default="status", choices=["status", "reveal"])
    timeline.add_argument("--dry-run", action="store_true", help="Show pending reveals without changing progress")
    sub.add_parser("compendium", help="Get active profile compendium")
    profiles = sub.add_parser("profiles", help="List profile slots")
    profiles.add_argument("--delete", type=int, default=None, help="Delete an inactive profile slot")

    switch_profile_parser = sub.add_parser("switch-profile", help="Switch profile slot through the game UI")
    switch_profile_parser.add_argument("profile_id", type=int)
    switch_profile_parser.add_argument("--max-polls", type=int, default=30)
    switch_profile_parser.add_argument("--poll-delay", type=float, default=DEFAULT_PROFILE_POLL_DELAY)

    delete_profile_parser = sub.add_parser("delete-profile", help="Delete an inactive profile slot")
    delete_profile_parser.add_argument("profile_id", type=int)

    wiki = sub.add_parser("wiki", help="Search profile-unlocked wiki entries")
    wiki.add_argument("query")
    wiki.add_argument("--item-type", default="all", choices=["all", "card", "relic"])
    wiki.add_argument("--limit", type=int, default=10)

    return parser


def make_client(args: argparse.Namespace, logger: JsonlLogger, stats: RunStats) -> STS2Client:
    return STS2Client(
        base_url=args.base_url,
        endpoint="multiplayer" if args.multiplayer else "singleplayer",
        logger=logger,
        stats=stats,
        timeout=args.timeout,
        trust_env=args.trust_env,
    )


def run_start_metadata(args: argparse.Namespace, argv: list[str] | None, log_path: Path | None) -> dict[str, Any]:
    return {
        "command": args.command,
        "argv": sys.argv[1:] if argv is None else argv,
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "repo_root": str(_repo_root()),
        "git_sha": _git_head_sha(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": platform.platform(),
        "base_url": args.base_url,
        "endpoint": "multiplayer" if args.multiplayer else "singleplayer",
        "timeout": args.timeout,
        "trust_env": args.trust_env,
        "log_path": str(log_path) if log_path is not None else None,
        "compact": bool(args.compact),
        "poll_delay": getattr(args, "poll_delay", None),
        "max_polls": getattr(args, "max_polls", None),
        "max_steps": getattr(args, "max_steps", None),
        "drain": bool(getattr(args, "drain", False)),
        "fast_action_waits": bool(getattr(args, "fast_action_waits", False)),
        "no_log": bool(args.no_log),
    }


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    indent = None if args.compact else 2

    if args.command == "analyze-log":
        try:
            print(_json_dumps(analyze_logs(expand_log_path_args(args.paths)), indent=indent))
            return 0
        except Exception as exc:
            print(_json_dumps({"ok": False, "error": str(exc)}, indent=indent))
            return 1

    log_path = None if args.no_log else output_log_path(args.log)
    logger = JsonlLogger(log_path)
    stats = RunStats()
    logger.write("run_start", **run_start_metadata(args, argv, log_path))

    client = make_client(args, logger, stats)
    try:
        if args.command == "state":
            if args.raw_format is not None:
                if args.drain:
                    drain_trivial(client, logger=logger, stats=stats)
                text = client.state_text(format_name=args.raw_format)
                data = parse_json_text(text) if args.raw_format == "json" else None
                logger.write(
                    "data_result",
                    endpoint=client.endpoint,
                    path=client.run_path(),
                    raw_format=args.raw_format,
                    bytes=len(text.encode("utf-8")),
                )
                emit_data_result(
                    ok=True,
                    stats=stats,
                    log_path=log_path,
                    logger=logger,
                    data=data,
                    text=None if data is not None else text,
                    indent=indent,
                )
                return 0
            if args.drain:
                state, drained = drain_trivial(client, logger=logger, stats=stats)
            else:
                state, drained = client.state(), None
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                state=state,
                drained=drained,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "map":
            state = client.state()
            data = act_map_data(state)
            logger.write(
                "data_result",
                endpoint=client.endpoint,
                path=client.run_path(),
                data_kind="act_map",
                node_count=len(data.get("nodes") or []),
                next_option_count=len(data.get("next_options") or []),
                decision_point=decision_point(state),
            )
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0

        if args.command == "menu":
            state, executed = execute_menu_option(
                client,
                args.option,
                seed=args.seed,
                wait=not args.no_wait,
                logger=logger,
                stats=stats,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                state=state,
                executed=[executed],
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "return-menu":
            planned: dict[str, Any] = {"action": "return_to_menu"}
            if args.no_wait:
                planned["_wait"] = False
            state, executed = execute_actions(
                client,
                [planned],
                logger=logger,
                stats=stats,
                auto_target=False,
                drain_after=False,
                wait_after_end_turn=True,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                state=state,
                executed=executed,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "start-run":
            state, executed = start_run(
                client,
                logger=logger,
                stats=stats,
                mode=args.mode,
                character=args.character,
                seed=args.seed,
                max_steps=args.max_steps,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
                auto_reveal_timeline=args.auto_reveal_timeline,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                state=state,
                executed=executed,
                verbose=args.verbose,
                indent=indent,
            )
            return 0

        if args.command == "timeline":
            if args.action == "status":
                data = client.get_json("/api/v1/timeline")
                action_name = "status"
            else:
                data = post_json_unchecked(
                    client,
                    "/api/v1/timeline",
                    {"action": "reveal_pending", "dry_run": args.dry_run},
                )
                action_name = "reveal_pending"
            logger.write("data_result", path="/api/v1/timeline", action=action_name, data=data)
            emit_data_result(
                ok=data.get("status") != "error",
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0 if data.get("status") != "error" else 1

        if args.command == "profile":
            data = client.get_json("/api/v1/profile")
            logger.write("data_result", path="/api/v1/profile", data=data)
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0

        if args.command == "compendium":
            data = client.get_json("/api/v1/compendium")
            logger.write("data_result", path="/api/v1/compendium", data=data)
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0

        if args.command == "wiki":
            data = client.get_json(
                "/api/v1/wiki",
                params={
                    "query": args.query,
                    "item_type": args.item_type,
                    "limit": args.limit,
                },
            )
            logger.write(
                "data_result",
                path="/api/v1/wiki",
                query=args.query,
                item_type=args.item_type,
                limit=args.limit,
                data=data,
            )
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0

        if args.command == "profiles":
            if args.delete is not None:
                data = client.post_json(
                    "/api/v1/profiles",
                    {"action": "delete", "profile_id": args.delete},
                )
                logger.write("data_result", path="/api/v1/profiles", action="delete", data=data)
            else:
                data = client.get_json("/api/v1/profiles")
                logger.write("data_result", path="/api/v1/profiles", data=data)
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0

        if args.command == "switch-profile":
            data = switch_profile(
                client,
                args.profile_id,
                logger=logger,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            logger.write(
                "data_result",
                path="/api/v1/profiles",
                action="switch",
                profile_id=args.profile_id,
                data=data,
            )
            emit_data_result(
                ok=data.get("status") != "error",
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
                indent=indent,
            )
            return 0 if data.get("status") != "error" else 1

        if args.command == "delete-profile":
            data = client.post_json(
                "/api/v1/profiles",
                {"action": "delete", "profile_id": args.profile_id},
            )
            logger.write(
                "data_result",
                path="/api/v1/profiles",
                action="delete",
                profile_id=args.profile_id,
                data=data,
            )
            emit_data_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
                data=data,
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
                logger=logger,
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
                fast_action_waits=args.fast_action_waits,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
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
                fast_action_waits=args.fast_action_waits,
                max_polls=args.max_polls,
                poll_delay=args.poll_delay,
            )
            log_state_result(logger, state)
            emit_result(
                ok=True,
                stats=stats,
                log_path=log_path,
                logger=logger,
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
            logger=logger,
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
