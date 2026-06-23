# STS2 HTTP API Reference

Use this reference when debugging or extending the fast CLI's localhost API
contract. Normal gameplay should go through `cli/sts2_fast_cli.py`.

For user-facing CLI commands, startup/menu/profile/wiki commands, multiplayer
routing, and original MCP tool-name aliases, read `cli-surface.md`.

## State

Read:

```http
GET /api/v1/singleplayer?format=json
```

Important top-level fields:

- `state_type`: screen or phase, such as `monster`, `elite`, `boss`, `map`,
  `event`, `rewards`, `card_reward`, `rest_site`, `treasure`, `shop`,
  `card_select`, `bundle_select`, or `relic_select`.
- `run`: act, floor, ascension, and run metadata.
- `player`: HP, max HP, block, energy, gold, hand, potions, relics, piles.
- `battle`: combat round, turn, play phase, and enemies.

Do not assume top-level `hand`, `enemies`, or `next_options`. Combat cards live
under `player.hand`. Combat enemies live under `battle.enemies`. Map options
live under `map.next_options`.

## Common POST Actions

All actions are sent to:

```http
POST /api/v1/singleplayer
Content-Type: application/json
```

Common bodies:

```json
{"action":"play_card","card_index":0,"target":"ENEMY_ID_0"}
{"action":"end_turn"}
{"action":"use_potion","slot":0,"target":"ENEMY_ID_0"}
{"action":"discard_potion","slot":0}
{"action":"claim_reward","index":0}
{"action":"select_card_reward","card_index":2}
{"action":"skip_card_reward"}
{"action":"choose_map_node","index":0}
{"action":"choose_rest_option","index":0}
{"action":"shop_purchase","index":0}
{"action":"proceed"}
{"action":"choose_event_option","index":0}
{"action":"advance_dialogue"}
{"action":"combat_select_card","card_index":1}
{"action":"combat_confirm_selection"}
{"action":"select_card","index":15}
{"action":"confirm_selection"}
{"action":"cancel_selection"}
{"action":"select_bundle","index":0}
{"action":"confirm_bundle_selection"}
{"action":"cancel_bundle_selection"}
{"action":"select_relic","index":0}
{"action":"skip_relic_selection"}
{"action":"claim_treasure_relic","index":0}
```

## Ready-State Polling

After a POST, poll state until one of these is true:

- non-combat screen has populated options or can proceed
- combat screen has `battle.turn == "player"`, `battle.is_play_phase == true`,
  and a non-empty `player.hand`
- reward/card/map/event/rest screens show stable options

Transient states to keep polling through:

- `event` with `in_dialogue == false` and no options
- `map` with no `next_options` and no current position
- `treasure` with only a message and no relics
- combat with enemy turn, `is_play_phase == false`, or empty player hand

## Index Rules

- Playing a card removes it from hand and shifts remaining indexes.
- Claiming rewards can shift reward indexes.
- Card reward selection uses `card_index`.
- `hand_select` uses `card_index` with `combat_select_card` and
  `combat_confirm_selection` in the currently loaded Steam mod. This fork's
  patched bridge also accepts clearer `select_hand_card` and
  `confirm_hand_selection` aliases after rebuild/reload, but the fast CLI emits
  the compatibility action names by default.
- Card/deck selection screens use `index` with `select_card`.
- Potion actions use potion slot, not card or reward index.
- Modal selection screens can still expose stale combat hand data under
  `player.hand`. Treat `state_type` as authoritative: do not send combat
  actions while `state_type` is `hand_select`, `card_select`, `bundle_select`,
  or `relic_select`.

For safe batches, repeatedly:

1. Read state.
2. Resolve the next card/reward/option by name or index from that state.
3. POST exactly that action.
4. Poll until the action is applied.

## Target Rules

Enemy IDs are in `battle.enemies[*].entity_id`, usually upper snake case with a
numeric suffix, such as `BOWLBUG_ROCK_0`.

Use explicit targets when:

- multiple enemies are alive
- a leader/minion relationship matters
- one enemy is attacking and another is buffing
- lethal depends on target choice

Use no target for self-targeting skills, powers, and true all-enemy attacks.

## No-Decision Drain Rules

The model should not deliberate over:

- gold rewards
- relic rewards
- potion rewards with empty potion slots
- single treasure relic
- single event Proceed option
- empty completed rest/reward/treasure screens
- dialogue advance with no options
- maps with exactly one next node
- shops where no stocked item is affordable

These should be resolved by direct HTTP POSTs and followed by polling.

## Error Handling

If a POST response has `status: "error"`, `ok: false`, or an `error` field,
stop the batch and read state again. Common causes are stale indexes, wrong
screen type, missing `card_index`, or an action name that belongs to a different
screen.
