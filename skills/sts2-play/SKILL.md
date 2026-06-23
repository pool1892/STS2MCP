---
name: sts2-play
description: Use when playing Slay the Spire 2 through this repository. This skill is the primary gameplay workflow and requires the repo-local fast CLI as the only agent-control surface; it explains batching, no-decision automation, compact state-reading, and when to consult gameplay policy references.
---

# STS2 Play

This is the primary repo-local skill for playing Slay the Spire 2.

Use the repo-local fast CLI for everything. Do not start, configure, or call a
separate tool server for gameplay. The CLI is the execution surface; this skill
and its references are the behavioral policy.

When asked how to control the game, improve gameplay speed, debug CLI behavior,
or preserve parity with the old MCP bridge, read
`skills/sts2-play/references/cli-surface.md` before acting. Treat that file as
the canonical CLI contract.

## Operating Contract

- CLI: `cli/sts2_fast_cli.py`
- Base URL used by the CLI: `http://127.0.0.1:15526`
- The CLI reads state, sends POST actions, drains no-decision screens, polls
  transitions, resolves shifted card indexes, and writes timing logs.
- The CLI covers the original MCP tool surface. For the full command list,
  startup/menu/profile/wiki commands, multiplayer routing, and MCP alias table,
  read `skills/sts2-play/references/cli-surface.md`.
- Before using a direct HTTP fallback or inventing a new helper command, check
  the CLI reference first. If the CLI genuinely lacks a needed capability, add
  it to `cli/sts2_fast_cli.py` and document it in `cli-surface.md`.
- Gameplay decisions live in skills and notes, not hidden inside ad hoc tool
  calls.
- Direct HTTP is a debugging fallback or implementation contract, not the
  normal play path.
- Keep a lightweight run log under `notes/` when strategy, mistakes, or API
  behavior teach something reusable.
- When the CLI gains or changes a command, flag, action alias, drain rule,
  waiting behavior, or log field, update `cli-surface.md` in the same slice.
  Update `README.md` too when the change affects player-facing setup or common
  commands.

For exact state fields and action payloads, read
`skills/sts2-play/references/http-api.md`.

For the full CLI surface and MCP parity table, read
`skills/sts2-play/references/cli-surface.md`.

For tactical and strategic gameplay policy, read
`skills/sts2-play/references/gameplay-policy.md`.

For logging and self-improvement-loop notes, read
`skills/sts2-play/references/learning-loop.md`.

## Core CLI Commands

Command map:

- `state`, `map`, `drain`, `act`, `cards`, `analyze-log`
- `menu`, `start-run`
- `profile`, `compendium`, `wiki`
- `profiles`, `switch-profile`, `delete-profile`

Get compact state and drain no-decision screens:

```fish
uv run --directory cli python sts2_fast_cli.py --compact state --drain
```

Read the full current act map graph while on a map screen:

```fish
uv run --directory cli python sts2_fast_cli.py --compact map
```

Start a fresh singleplayer run from the menu:

```fish
uv run --directory cli python sts2_fast_cli.py --compact start-run --character ironclad
```

Select menu/lobby/game-over/profile/timeline/popup options:

```fish
uv run --directory cli python sts2_fast_cli.py --compact menu singleplayer
uv run --directory cli python sts2_fast_cli.py --compact menu main_menu
```

Execute a fused deterministic turn plan:

```fish
uv run --directory cli python sts2_fast_cli.py --compact act '[{"play":"Shrug It Off+"},{"play":"Uppercut+","target":"first"},{"end_turn":true}]' --drain --max-polls 80
```

For deterministic commands whose final action is a simple card play and whose
next decision does not depend on a conservative post-action state, add
`--fast-action-waits`.
For one-decision modal selections, prefer `hand_pick`, `deck_pick`,
`card_select_pick`, or `bundle_pick` so selection and confirmation stay inside
one CLI command.

Pin a log path for a measured slice:

```fish
uv run --directory cli python sts2_fast_cli.py --log logs/sts2-fast/act2-fight-01.jsonl --compact act '[{"end_turn":true}]' --drain --max-polls 80
```

Analyze logs:

```fish
uv run --directory cli python sts2_fast_cli.py analyze-log 'logs/sts2-fast/act2-fight-01*.jsonl'
```

Read profile and durable wiki context:

```fish
uv run --directory cli python sts2_fast_cli.py --compact profile
uv run --directory cli python sts2_fast_cli.py --compact compendium
uv run --directory cli python sts2_fast_cli.py --compact wiki "perfected strike" --item-type card
```

## Core Loop

1. Read state.
2. Drain no-decision states with the CLI without model deliberation.
3. If a real decision remains, reason from the current state and policy notes.
   For any card-choice decision, read the whole deck context first. Use raw
   state when compact output does not expose the full hand plus draw, discard,
   and exhaust piles.
4. Send the smallest safe batch of deterministic CLI actions.
5. Let the CLI poll until the next ready state.
6. Record decisions, mistakes, timing pain, and new automation candidates.

Use `act` or `cards` to fuse deterministic work into one CLI process when the
action sequence has no unresolved strategic choice. Use separate CLI calls when
card draws, random effects, enemy state, reward contents, or target choice could
change the correct next action.

Trust the CLI's settled state over the first visible combat-looking frame. The
game can briefly expose retained cards, partial hands, or stale combat after
map entry, end turn, draw effects, Skill Potion, and Thinking Ahead-style
selection effects; the CLI waiters intentionally require stable decision frames
before returning.

## No-Decision States

Do not spend reasoning on these once detected:

- claim gold rewards
- claim relic rewards
- claim potion rewards when a potion slot is empty
- proceed from empty reward, rest, treasure, or event screens
- advance event dialogue when there are no options
- claim a single treasure relic
- choose the only available map node
- leave a shop once no stocked item is affordable
- end a turn when no playable card can improve the result

When a new no-decision state appears repeatedly, update the skill references so
future agents stop paying attention to it.

If a no-decision state requires a new action endpoint or waiter, implement it in
the CLI, add a regression test, and document it in `cli-surface.md`.

## Combat Safety Rules

- Re-read state after any card play that changes hand indexes.
- Treat `hand_select`, `card_select`, `bundle_select`, and `relic_select`
  screens as modal decisions. Resolve or cancel the selection before playing
  hand cards, using potions, or ending the turn.
- If batching card plays in one shell snippet, resolve card indexes from the
  latest state between each play.
- Sum enemy attack intents before deciding whether to block.
- Prefer lethal over blocking when lethal is certain.
- Use potions before spending energy when they materially improve the turn.
- Target leader enemies before minions when minion powers imply they will flee
  or become irrelevant after the leader dies.

## Handoff

At a pause, report current act/floor, HP, gold, potion slots, screen type, live
decision options, and any unfinished run-note updates. Summarize CLI actions
and gameplay decisions.
