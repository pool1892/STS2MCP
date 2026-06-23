# STS2 Fast CLI Surface

This fork uses `cli/sts2_fast_cli.py` as the only agent-control surface. Do not
start or call an MCP server. The CLI intentionally covers the original MCP tool
surface through first-class commands plus `act` aliases for old MCP tool names.

Use this reference when playing, starting runs, inspecting profile/wiki data,
debugging timing, extending controls, or checking whether a capability already
exists. If a capability is missing here, either it is not implemented or the
docs are stale; inspect `cli/sts2_fast_cli.py`, add tests, and update this file
in the same change.

## Contents

- [Global Flags](#global-flags)
- [Command Catalog](#command-catalog)
- [Output And Error Contract](#output-and-error-contract)
- [State And Raw State](#state-and-raw-state)
- [Whole Act Map](#whole-act-map)
- [Start And Menu Control](#start-and-menu-control)
- [Gameplay Actions](#gameplay-actions)
- [No-Decision Drain](#no-decision-drain)
- [Waiting And Polling](#waiting-and-polling)
- [Profile, Compendium, And Wiki](#profile-compendium-and-wiki)
- [Timing Logs](#timing-logs)
- [Multiplayer](#multiplayer)
- [MCP Parity Table](#mcp-parity-table)
- [Extending The CLI](#extending-the-cli)

## Global Flags

Use these before the subcommand:

```fish
uv run --directory cli python sts2_fast_cli.py --compact state --drain
uv run --directory cli python sts2_fast_cli.py --base-url http://127.0.0.1:15526 --compact state
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact state
uv run --directory cli python sts2_fast_cli.py --log logs/sts2-fast/fight-01.jsonl --compact act '[{"end_turn":true}]'
```

- `--base-url`: game mod API root. Default: `http://127.0.0.1:15526`.
- `--timeout`: HTTP timeout in seconds.
- `--trust-env`: allow proxy environment variables.
- `--log`: JSONL timing log path. Relative paths resolve from repo root.
- default log path: `logs/sts2-fast/<timestamp>-<pid>.jsonl`.
- `--no-log`: disable JSONL logging.
- `--compact`: one-line JSON output for lower token usage.
- `--multiplayer`: route state/actions through `/api/v1/multiplayer`.
- Default mutating-command polling uses an adaptive cadence: 15ms for the first
  4 polls, 20ms for polls 4-7, then 40ms. Passing `--poll-delay` disables this
  adaptive schedule and uses the provided delay for every poll.
- Readiness is stability-based. Combat entry and next-turn combat states wait
  for several matching state digests before returning, because the game can
  briefly expose a legal-looking hand before start-of-turn relics, draw effects,
  modal choices, or reward transitions finish resolving.
- `act` and `cards` default to 120 polls, `menu` to 120 polls, `start-run` to
  160 polls, and `switch-profile` to 30 polls at an 80ms default profile delay.
  Override `--poll-delay` or `--max-polls` only when measuring or debugging a
  specific transition.

## Command Catalog

Global flags must come before the subcommand.

| Command | Purpose | Options |
| --- | --- | --- |
| `state` | Read the current game state. | `--drain`, `--verbose`, `--raw-format json|markdown` |
| `map` | Read the full current act map graph. | none |
| `drain` | Resolve no-decision screens only. | `--max-steps N`, `--verbose` |
| `act ACTIONS` | Execute one JSON object/list, or `@path/to/actions.json`. | `--no-auto-target`, `--drain`, `--no-wait-end-turn`, `--fast-action-waits`, `--max-polls N`, `--poll-delay SECONDS`, `--verbose` |
| `cards CARD...` | Play card names in order with index re-resolution between plays. | `--target POLICY`, `--end-turn`, `--drain`, `--fast-action-waits`, `--max-polls N`, `--poll-delay SECONDS`, `--verbose` |
| `analyze-log PATH...` | Summarize one or more JSONL timing logs. | Paths may be files or glob patterns. Empty globs fail loudly. |
| `menu OPTION` | Select a visible menu/lobby/game-over/popup/profile option. | `--seed SEED`, `--no-wait`, `--max-polls N`, `--poll-delay SECONDS`, `--verbose` |
| `start-run` | Start a fresh singleplayer run from menu or game over. | `--mode standard|daily|custom`, `--character CHARACTER|first`, `--seed SEED`, `--max-steps N`, `--max-polls N`, `--poll-delay SECONDS`, `--verbose` |
| `profile` | Read active profile progress. | none |
| `compendium` | Read profile compendium/run-history context. | none |
| `wiki QUERY` | Search profile-unlocked card/relic wiki entries. | `--item-type all|card|relic`, `--limit N` |
| `profiles` | List profile slots. | `--delete PROFILE_ID` is a legacy-compatible shortcut for deleting an inactive slot. |
| `switch-profile PROFILE_ID` | Switch active profile through the game UI. | `--max-polls N`, `--poll-delay SECONDS` |
| `delete-profile PROFILE_ID` | Delete an inactive profile slot. | none |

Command output is JSON. Gameplay commands return `ok`, `summary`, and usually
`state`; mutating commands also include `executed` and/or `drained`. Data
commands return `data` or `text`. On failure the CLI exits non-zero and returns
`ok: false` plus an `error` string.

## Output And Error Contract

Every command writes JSON to stdout. With `--compact`, this is a single line.
Without `--compact`, it is pretty-printed.

Successful gameplay output:

- `ok`: `true`.
- `summary`: elapsed time, HTTP time, call counts, action counts, stdout size,
  and log path.
- `state`: concise state summary unless the command is data-only.
- `executed`: action bodies posted by `act`, `cards`, `menu`, or `start-run`.
- `drained`: no-decision actions taken by `state --drain`, `drain`, or
  mutating commands with `--drain`.

Successful data output:

- `ok`: `true`.
- `summary`: timing and log metadata.
- `data`: structured JSON for `profile`, `compendium`, `wiki`, `profiles`,
  `switch-profile`, `delete-profile`, and raw JSON state.
- `text`: raw Markdown state when using `state --raw-format markdown`.

Failure output:

- exit status is non-zero.
- `ok`: `false`.
- `error`: actionable error string when available.
- `summary.log_path`: path to the JSONL log unless `--no-log` was used.

Stop a gameplay batch on failures. Read state again before retrying, because
wrong screen, stale indexes, shifted reward indexes, and missing targets are the
most common causes.

## State And Raw State

```fish
uv run --directory cli python sts2_fast_cli.py --compact state --drain
uv run --directory cli python sts2_fast_cli.py --compact state --verbose
uv run --directory cli python sts2_fast_cli.py --compact state --raw-format json
uv run --directory cli python sts2_fast_cli.py --compact state --raw-format markdown
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact state --raw-format json
```

- Default `state` prints a concise structured summary.
- `--drain` resolves no-decision screens before printing.
- `--raw-format json|markdown` provides the original MCP-style state format.
- Prefer compact summaries for gameplay decisions; use raw JSON/Markdown when
  debugging parity or missing fields.
- `state --drain --raw-format json|markdown` drains first, then fetches raw
  state text.

## Whole Act Map

```fish
uv run --directory cli python sts2_fast_cli.py --compact map
```

`map` is a read-only command for route planning from a map screen. It returns:

- `run`: act/floor metadata.
- `player`: HP, max HP, gold, potions, and relic names.
- `current_position`: current map coordinate.
- `visited`: visited map nodes.
- `next_options`: currently selectable next nodes with their immediate
  children.
- `boss` and `bosses`: boss metadata.
- `nodes`: the full act graph, preserving node fields from the game API,
  including `col`, `row`, `type`, `children`, and any boss identifiers/names.

The command exits non-zero if the current state does not expose `map.nodes`.
Use it while on the map screen; during combat or selection screens, use normal
state reads until the run returns to the map.

## Start And Menu Control

Start a normal run from the menu:

```fish
uv run --directory cli python sts2_fast_cli.py --compact start-run --character ironclad
uv run --directory cli python sts2_fast_cli.py --compact start-run --character first
uv run --directory cli python sts2_fast_cli.py --compact start-run --mode daily --character first
```

Menu/lobby/game-over/profile/timeline/popup control:

```fish
uv run --directory cli python sts2_fast_cli.py --compact menu singleplayer
uv run --directory cli python sts2_fast_cli.py --compact menu standard
uv run --directory cli python sts2_fast_cli.py --compact menu ironclad
uv run --directory cli python sts2_fast_cli.py --compact menu confirm
uv run --directory cli python sts2_fast_cli.py --compact menu main_menu
uv run --directory cli python sts2_fast_cli.py --compact menu advance
uv run --directory cli python sts2_fast_cli.py --compact menu join_0
uv run --directory cli python sts2_fast_cli.py --compact menu confirm --no-wait
```

- `menu` wraps the API `menu_select` action.
- It covers main menu, singleplayer, multiplayer host/join/load lobbies,
  character select, tutorial/FTUE popups, Timeline, profile select, and
  game-over return.
- `--seed` is forwarded to the game only when the selected option supports it,
  typically the final confirm/embark step.
- Use `--no-wait` only when the selected option is expected to keep the visible
  state unchanged. Normal gameplay should let the CLI wait for the next state.
- `start-run` refuses to start from an active run; it is startup ceremony, not
  an abandon-run tool.
- `start-run` can begin from main menu or game-over. It walks through
  singleplayer, mode select, character select, and confirm/embark.

## Gameplay Actions

Execute a fused plan:

```fish
uv run --directory cli python sts2_fast_cli.py --compact act '[{"play":"Shrug It Off+"},{"play":"Uppercut+","target":"first"},{"end_turn":true}]' --drain --max-polls 80
uv run --directory cli python sts2_fast_cli.py --compact act @notes/next-actions.json --drain
```

Play cards by name:

```fish
uv run --directory cli python sts2_fast_cli.py --compact cards "Bash" "Strike" --target first --end-turn --drain
```

Drain no-decision screens:

```fish
uv run --directory cli python sts2_fast_cli.py --compact drain --max-steps 30
```

`drain` handles simple rewards, empty proceed screens, completed rest/treasure
screens, event dialogue proceeds, single-node maps, completed Crystal Sphere
screens, and shops where no stocked item is affordable.

Common action shorthands:

```json
{"play":"Strike","target":"lowest_hp"}
{"potion":0,"target":"first"}
{"end_turn":true}
{"reward":0}
{"pick_card":2}
{"action":"skip_card_reward"}
{"map":0}
{"event":0}
{"rest":1}
{"shop":3}
{"hand_pick":1}
{"deck_pick":4}
{"card_select_pick":4}
{"bundle_pick":0}
{"deck_select_card":4}
{"action":"deck_confirm_selection"}
{"action":"deck_cancel_selection"}
{"hand_select":1}
{"action":"confirm_hand_selection"}
{"combat_select_card":1}
{"action":"combat_confirm_selection"}
{"select_bundle":0}
{"action":"confirm_bundle_selection"}
{"action":"cancel_bundle_selection"}
{"select_relic":0}
{"action":"skip_relic_selection"}
{"claim_treasure_relic":0}
{"action":"crystal_sphere_set_tool","tool":"big"}
{"action":"crystal_sphere_click_cell","x":1,"y":2}
{"action":"crystal_sphere_proceed"}
{"menu":"singleplayer"}
{"action":"raw","body":{"action":"custom_action"}}
```

Action plans may use either ergonomic shorthands or exact HTTP action names.
`play`/`card` resolves the current card index by name at execution time; use
`occurrence` for duplicate cards. `cards` and multi-action `act` re-read state
between card plays so shifted hand indexes do not corrupt the later actions.
No-argument actions that do not have an ergonomic shorthand should use the
explicit `{"action":"..."}` shape.

Pick-and-confirm macros reduce one-decision modal screens to one planned
action:

- `{"hand_pick":1}` selects a `hand_select` card and confirms only if the modal
  is still present and `can_confirm` is true.
- `{"deck_pick":4}` and `{"card_select_pick":4}` select a card-selection item
  and confirm when `card_select.can_confirm` becomes true.
- `{"bundle_pick":0}` selects a bundle and confirms when
  `bundle_select.can_confirm` becomes true.

Use these when the selected item is the whole decision. Use lower-level select
and confirm actions when multiple selections, cancellation, or post-select
inspection is strategically relevant.

Action input formats:

- inline JSON object: `act '{"end_turn":true}'`
- inline JSON list: `act '[{"play":"Strike"},{"end_turn":true}]'`
- file-backed JSON object or list: `act @notes/next-actions.json`
- string action inside a JSON list: `act '["combat_end_turn"]'`

Important action semantics:

- `{"play":"Name"}` and `{"card":"Name"}` resolve by normalized card name.
- `{"play":"Name","occurrence":1}` chooses the second matching duplicate.
- `{"action":"play_card","card_index":N}` uses the current hand index.
- `{"potion":0}` and `{"use_potion":0}` use potion slot index.
- `{"reward":0}` claims reward list index.
- `{"pick_card":2}` selects card reward card index.
- `{"map":0}`, `{"event":0}`, `{"rest":1}`, and `{"shop":3}` select screen
  option/item indexes.
- `{"action":"raw","body":{...}}` posts an exact custom HTTP body. Use this
  only while adding or debugging a proper CLI alias.
- Combat-only actions (`play_card`, `end_turn`, and `use_potion`) are refused
  unless the current state is combat. This protects against modal selection
  screens that still expose stale `player.hand` data in the raw API state.

Batching rules:

- Batch deterministic card plays and `end_turn` when target choices and draw
  outcomes cannot change the correct next action.
- Do not batch across random draws, card-generation effects, card/bundle/relic
  selection prompts, reward screens, or unknown event outcomes unless the later
  action is still correct for every possible result.
- `act --drain` and `cards --drain` run no-decision draining after each action.
- `--no-auto-target` disables automatic enemy targeting for cards and potions
  whose current state requires a target.
- `--fast-action-waits` shortens settle checks for the final simple in-combat
  card play in an action plan. Intermediate card plays stay conservative so
  subsequent name/index resolution does not read a stale hand. Delayed,
  draw/modal-opening cards, potions, map transitions, end turns, and
  screen-changing waits remain conservative.
- Use `--fast-action-waits` only when the command's final card play does not
  need a conservative post-action state for an immediate follow-up decision.
- `--no-wait-end-turn` returns after posting `end_turn`; use only for low-level
  debugging because normal play needs the next ready state.

Selection-screen rule:

- `hand_select`, `card_select`, `bundle_select`, and `relic_select` are modal
  decisions.
- Resolve `hand_select` with `hand_select`, `select_hand_card`, or
  `combat_select_card`. For compatibility with the currently loaded Steam mod,
  the CLI posts `combat_select_card` with `card_index`; the patched bridge also
  accepts the clearer `select_hand_card` action name after rebuild/reload. Use
  `confirm_hand_selection` or `combat_confirm_selection` only when
  `can_confirm` is true.
- Resolve deck/overlay screens with `deck_select_card`, `select_bundle`,
  `select_relic`, confirm/cancel actions, or the appropriate skip action before
  attempting combat actions.
- If the visible game has cards but the CLI state says `hand_select` or
  `card_select`, the selection state is authoritative.

Target policies:

- `first`: first living enemy.
- `lowest_hp`: living enemy with lowest HP.
- `highest_hp`: living enemy with highest HP.
- `auto`: same as `first` for target-required cards and enemy-targeted
  potions.
- any other string: sent as an explicit entity id, such as `BOWLBUG_ROCK_0`.
- omit `target` for self, power, all-enemy, and random-enemy effects.

Waiting behavior:

- Card plays wait until the hand/state settles. Cards that can trigger delayed
  effects, draws, generated cards, powers, auto-plays, or end-of-action effects
  receive extra stability polls before the CLI returns. Cards likely to open a
  hand/card selection prompt receive additional settle budget so combat-looking
  intermediate frames do not hide the modal.
- With `--fast-action-waits`, the final simple in-combat card play uses a
  shorter settle window. Treat this as an optimization for deterministic,
  already-reasoned endings, not as the default for uncertain draw, target,
  lethal, or follow-up-card lines.
- `end_turn` waits for the next player turn or next ready screen unless
  `--no-wait-end-turn` is set. A player-turn combat state is considered ready
  after a short stable run, not merely because one poll showed a playable card.
- Map/event/reward/rest/shop/selection/menu/proceed-like actions wait for a
  visible state change. Any action that lands in combat waits for a stable
  combat digest before returning.
- Multiplayer `mp_*` aliases post and then read state once by default, because
  many multiplayer actions are votes that cannot resolve without other players.

## No-Decision Drain

`state --drain`, `drain`, and mutating commands with `--drain` resolve screens
that should not spend model reasoning. Current drain rules:

- claim gold rewards
- claim relic rewards
- claim potion rewards when a potion slot is empty
- claim the single relic on a treasure screen
- proceed from empty reward, rest, treasure, event Proceed, and completed
  Crystal Sphere screens
- advance event dialogue when there are no options
- choose the only available map node
- leave shops and fake merchant screens when they have no stocked affordable
  items

Drain intentionally avoids strategic choices:

- card rewards
- relic selection screens with multiple options
- shops with affordable stocked items
- rest sites with real choices
- events with multiple enabled non-Proceed options
- maps with more than one next node
- potion rewards when potion slots are full

When a repeated screen has only one valid or reasonable action, add it to
`next_trivial_action`, add a regression test, and update this section.

## Waiting And Polling

The CLI waits after POSTs so agents do not read transient sparse states:

- `play_card`: wait for hand signature or state to change, then require stable
  state digests. Delayed-effect cards receive additional stability polls; cards
  that may open selection prompts wait longer.
- `end_turn`: wait for a changed state and then either the next ready player
  turn after a stable start-of-turn window, a ready non-combat screen, or a
  settled screen transition. This catches retained-card-only and sparse
  start-of-turn frames before the draw/relic queue finishes.
- `choose_map_node`: wait for the map to leave and the destination to become a
  ready decision state. Combat destinations require a stable combat window;
  this prevents early empty or partial combat frames and delayed modal choices
  from leaking to the agent.
- menu/proceed/reward/event/rest/shop/selection actions: wait for visible state
  digest changes. If the changed state is combat, the generic waiter also
  requires a stable combat window before returning.
- transient states are polled through instead of returned as decisions.

Default wait reasons appear in timing logs, including:

- `card_play_settled`
- `card_play_extra_settled`
- `card_play_state_changed_settled`
- `card_play_left_combat_settled`
- `end_turn_next_player_turn_settled`
- `end_turn_screen_changed_settled`
- `map_node_combat_ready_settled`
- `map_node_state_changed_settled`
- `action_combat_ready_settled`
- `action_state_changed_settled`
- `hand_select_can_confirm_settled`
- `hand_select_resolved_settled`
- `card_select_can_confirm_settled`
- `bundle_select_can_confirm_settled`
- `menu_state_changed_settled`
- `drain_state_changed_settled`
- `ready_state`

Timeouts raise an error and preserve the log path. On a timeout, inspect the
last state, wait reason, poll count, and correlated HTTP events before changing
poll budgets.

## Profile, Compendium, And Wiki

```fish
uv run --directory cli python sts2_fast_cli.py --compact profile
uv run --directory cli python sts2_fast_cli.py --compact compendium
uv run --directory cli python sts2_fast_cli.py --compact wiki "perfected strike" --item-type card --limit 5
uv run --directory cli python sts2_fast_cli.py --compact profiles
uv run --directory cli python sts2_fast_cli.py --compact switch-profile 2
uv run --directory cli python sts2_fast_cli.py --compact delete-profile 3
```

- `profile` maps to old MCP `get_profile`.
- `compendium` maps to old MCP `get_compendium`.
- `wiki` maps to old MCP `search_wiki`.
- `profiles`, `switch-profile`, and `delete-profile` map to old MCP profile
  slot tools.
- Switching profile is not allowed during an active run.

## Timing Logs

```fish
uv run --directory cli python sts2_fast_cli.py --compact analyze-log 'logs/sts2-fast/fight-*.jsonl'
```

Log writing:

- Logs are JSONL files under `logs/sts2-fast/` by default.
- Use `--log path/to/file.jsonl` to pin a measured slice.
- Use `--no-log` only for tiny ad hoc reads where no later analysis is useful.
- Relative `--log` paths resolve from the repo root.

Event kinds:

- `run_start`: command, argv, cwd, repo root, git SHA, Python/platform, base
  URL, endpoint, timeout, compact flag, poll delay, max polls, max steps, and
  drain/no-log flags.
- `http`: method, path, params, POST action, status, elapsed time, response
  bytes, error, and correlated `wait_id` when inside a waiter.
- `planned_action`: normalized planned action, exact POST body, state digest,
  and current decision point before the POST.
- `action_result`: before/after digests, state delta, and resulting decision
  point after a non-drain action.
- `drain_action` and `drain_result`: same as planned/action result, plus the
  no-decision reason.
- `wait`: wait reason, poll count, elapsed time, and `wait_id`.
- `state_result`: final state digest and decision point for state-producing
  commands.
- `data_result`: data endpoint result metadata for profile/wiki/compendium and
  raw state.
- `stdout`: model-visible output size.
- `run_error`: exception string.
- `run_end`: final elapsed time, HTTP counts, action counts, and stdout size.

Analyze-log fields to inspect:

- `active_wall_time_ms`: elapsed CLI wall time for the command or analyzed log
  set.
- `wall_time_ms`: span from first to last event inside the analyzed file set.
- `timing_breakdown.http_total_ms`: local API/request time.
- `timing_breakdown.wait_elapsed_ms`: total wait span time.
- `timing_breakdown.wait_http_overlap_ms`: HTTP time that occurred inside wait
  spans.
- `timing_breakdown.wait_non_http_ms`: animation/sleep time.
- `timing_breakdown.local_overhead_ms`: CLI overhead outside HTTP/wait sleeps.
- `waits.reasons`: count by wait reason.
- `waits.elapsed_ms`: count, total, average, p50, p90, and max wait span.
- `waits.http_by_wait_id`: HTTP calls and elapsed time correlated to each wait.
- `actions`: HTTP action totals, averages, and errors.
- `decision_points`, `decision_points_by_phase`, and `decision_path`: compact
  view of strategic decision states reached by the command sequence.
- `state_transitions`: before/after state-type transitions.
- `gameplay`: inferred HP, gold, enemy HP, and enemy block deltas from logged
  state deltas.
- `stdout.bytes`: token-visible output size.
- `command_timing.next_post_gaps.first_post`: observed end-to-end gap from the
  previous POST command finishing to the next POST beginning. This includes
  model reasoning, tool dispatch, shell/runtime overhead, user interruption,
  and any non-POST commands between POSTs; it is not pure Codex internal think
  time.

Use multi-file analysis to study agent-loop timing:

```fish
uv run --directory cli python sts2_fast_cli.py --compact analyze-log 'logs/sts2-fast/goal-win-*.jsonl'
```

`command_timing.next_post_gaps.first_post` is the best available distribution
for "how long until the agent caused the next game-changing POST." It is an
observed end-to-end loop metric, not a private model-think timer.

## Multiplayer

Use either global `--multiplayer` with normal actions:

```fish
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact state
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact act '[{"map":0}]'
```

Or use old MCP `mp_*` action aliases inside `act`:

```fish
uv run --directory cli python sts2_fast_cli.py --compact act '[{"action":"mp_map_vote","node_index":0}]'
uv run --directory cli python sts2_fast_cli.py --compact act '[{"action":"mp_combat_end_turn"}]'
uv run --directory cli python sts2_fast_cli.py --compact act '[{"action":"mp_combat_undo_end_turn"}]'
```

Old MCP `mp_*` aliases post and then read the current state by default, matching
the old MCP bridge's immediate-return behavior for votes. They do not wait for
other players to resolve the vote.

## MCP Parity Table

General and profile tools:

| Original MCP tool | CLI path |
| --- | --- |
| `get_game_state(format)` | `state --raw-format json|markdown` |
| `menu_select(option, seed)` | `menu OPTION [--seed SEED]` or `act '[{"menu":"OPTION"}]'` |
| `get_profile()` | `profile` |
| `get_compendium()` | `compendium` |
| `search_wiki(query, item_type, limit)` | `wiki QUERY --item-type TYPE --limit N` |
| `list_profiles()` | `profiles` |
| `switch_profile(profile_id)` | `switch-profile PROFILE_ID` |
| `delete_profile(profile_id)` | `delete-profile PROFILE_ID` |

Singleplayer gameplay tools map to `act` aliases:

| Original MCP tool | CLI `act` alias |
| --- | --- |
| `use_potion(slot, target)` | `{"action":"use_potion","slot":0,"target":"ENEMY_0"}` |
| `discard_potion(slot)` | `{"action":"discard_potion","slot":0}` |
| `proceed_to_map()` | `{"action":"proceed_to_map"}` |
| `combat_play_card(card_index, target)` | `{"action":"combat_play_card","card_index":0,"target":"ENEMY_0"}` |
| `combat_end_turn()` | `{"action":"combat_end_turn"}` |
| `combat_select_card(card_index)` | `{"action":"combat_select_card","card_index":0}` |
| `combat_confirm_selection()` | `{"action":"combat_confirm_selection"}` |
| `rewards_claim(reward_index)` | `{"action":"rewards_claim","reward_index":0}` |
| `rewards_pick_card(card_index)` | `{"action":"rewards_pick_card","card_index":0}` |
| `rewards_skip_card()` | `{"action":"rewards_skip_card"}` |
| `map_choose_node(node_index)` | `{"action":"map_choose_node","node_index":0}` |
| `rest_choose_option(option_index)` | `{"action":"rest_choose_option","option_index":0}` |
| `shop_purchase(item_index)` | `{"action":"shop_purchase","item_index":0}` |
| `event_choose_option(option_index)` | `{"action":"event_choose_option","option_index":0}` |
| `event_advance_dialogue()` | `{"action":"event_advance_dialogue"}` |
| `deck_select_card(card_index)` | `{"action":"deck_select_card","card_index":0}` |
| `deck_confirm_selection()` | `{"action":"deck_confirm_selection"}` |
| `deck_cancel_selection()` | `{"action":"deck_cancel_selection"}` |
| `bundle_select(bundle_index)` | `{"action":"bundle_select","bundle_index":0}` |
| `bundle_confirm_selection()` | `{"action":"bundle_confirm_selection"}` |
| `bundle_cancel_selection()` | `{"action":"bundle_cancel_selection"}` |
| `relic_select(relic_index)` | `{"action":"relic_select","relic_index":0}` |
| `relic_skip()` | `{"action":"relic_skip"}` |
| `treasure_claim_relic(relic_index)` | `{"action":"treasure_claim_relic","relic_index":0}` |
| `crystal_sphere_set_tool(tool)` | `{"action":"crystal_sphere_set_tool","tool":"big"}` |
| `crystal_sphere_click_cell(x, y)` | `{"action":"crystal_sphere_click_cell","x":1,"y":2}` |
| `crystal_sphere_proceed()` | `{"action":"crystal_sphere_proceed"}` |

Multiplayer MCP tools map to `act` aliases with automatic multiplayer routing:

| Original MCP tool | CLI `act` alias |
| --- | --- |
| `mp_get_game_state(format)` | `--multiplayer state --raw-format json|markdown` |
| `mp_combat_play_card(card_index, target)` | `{"action":"mp_combat_play_card","card_index":0,"target":"ENEMY_0"}` |
| `mp_combat_end_turn()` | `{"action":"mp_combat_end_turn"}` |
| `mp_combat_undo_end_turn()` | `{"action":"mp_combat_undo_end_turn"}` |
| `mp_use_potion(slot, target)` | `{"action":"mp_use_potion","slot":0,"target":"ENEMY_0"}` |
| `mp_discard_potion(slot)` | `{"action":"mp_discard_potion","slot":0}` |
| `mp_map_vote(node_index)` | `{"action":"mp_map_vote","node_index":0}` |
| `mp_event_choose_option(option_index)` | `{"action":"mp_event_choose_option","option_index":0}` |
| `mp_event_advance_dialogue()` | `{"action":"mp_event_advance_dialogue"}` |
| `mp_rest_choose_option(option_index)` | `{"action":"mp_rest_choose_option","option_index":0}` |
| `mp_shop_purchase(item_index)` | `{"action":"mp_shop_purchase","item_index":0}` |
| `mp_rewards_claim(reward_index)` | `{"action":"mp_rewards_claim","reward_index":0}` |
| `mp_rewards_pick_card(card_index)` | `{"action":"mp_rewards_pick_card","card_index":0}` |
| `mp_rewards_skip_card()` | `{"action":"mp_rewards_skip_card"}` |
| `mp_proceed_to_map()` | `{"action":"mp_proceed_to_map"}` |
| `mp_deck_select_card(card_index)` | `{"action":"mp_deck_select_card","card_index":0}` |
| `mp_deck_confirm_selection()` | `{"action":"mp_deck_confirm_selection"}` |
| `mp_deck_cancel_selection()` | `{"action":"mp_deck_cancel_selection"}` |
| `mp_bundle_select(bundle_index)` | `{"action":"mp_bundle_select","bundle_index":0}` |
| `mp_bundle_confirm_selection()` | `{"action":"mp_bundle_confirm_selection"}` |
| `mp_bundle_cancel_selection()` | `{"action":"mp_bundle_cancel_selection"}` |
| `mp_combat_select_card(card_index)` | `{"action":"mp_combat_select_card","card_index":0}`; posts `combat_select_card` |
| `mp_combat_confirm_selection()` | `{"action":"mp_combat_confirm_selection"}`; posts `combat_confirm_selection` |
| `mp_relic_select(relic_index)` | `{"action":"mp_relic_select","relic_index":0}` |
| `mp_relic_skip()` | `{"action":"mp_relic_skip"}` |
| `mp_treasure_claim_relic(relic_index)` | `{"action":"mp_treasure_claim_relic","relic_index":0}` |
| `mp_crystal_sphere_set_tool(tool)` | `{"action":"mp_crystal_sphere_set_tool","tool":"big"}` |
| `mp_crystal_sphere_click_cell(x, y)` | `{"action":"mp_crystal_sphere_click_cell","x":1,"y":2}` |
| `mp_crystal_sphere_proceed()` | `{"action":"mp_crystal_sphere_proceed"}` |

## Extending The CLI

Preserve the one-surface rule: add capabilities to `cli/sts2_fast_cli.py`, not
to a separate server.

For new API actions:

1. Add an ergonomic shorthand in `normalize_action` when the action is common
   during play.
2. Add an exact action mapping in `action_body_from_plan`.
3. Add an old-MCP alias in `MCP_ACTION_ALIASES` if the old bridge exposed the
   capability or a compatibility name is useful.
4. Add or update wait behavior in `execute_actions`,
   `should_wait_for_state_change_after`, or a dedicated waiter.
5. Add no-decision automation in `next_trivial_action` only when the action has
   no strategic choice.
6. Add regression tests in `cli/tests/test_sts2_fast_cli.py`.
7. Update this file, `skills/sts2-play/SKILL.md` if gameplay behavior changes,
   and `README.md` if the command is player-facing.

For new timing or logging fields:

1. Log the raw event data close to where it is observed.
2. Teach `analyze-log` to aggregate it.
3. Document the field under [Timing Logs](#timing-logs).

For new startup/menu/profile/wiki capabilities, keep the first-class command
surface complete. Avoid requiring agents to know the raw HTTP endpoint unless
they are debugging or implementing the CLI itself.
