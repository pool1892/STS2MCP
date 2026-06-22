---
name: sts2-play
description: Use when playing Slay the Spire 2 through this repository. This skill is the primary gameplay workflow and requires the repo-local fast CLI as the only agent-control surface; it explains batching, no-decision automation, compact state-reading, and when to consult gameplay policy references.
---

# STS2 Play

This is the primary repo-local skill for playing Slay the Spire 2.

Use the repo-local fast CLI for everything. Do not start, configure, or call a
separate tool server for gameplay. The CLI is the execution surface; this skill
and its references are the behavioral policy.

## Operating Contract

- CLI: `cli/sts2_fast_cli.py`
- Base URL used by the CLI: `http://127.0.0.1:15526`
- The CLI reads state, sends POST actions, drains no-decision screens, polls
  transitions, resolves shifted card indexes, and writes timing logs.
- Gameplay decisions live in skills and notes, not hidden inside ad hoc tool
  calls.
- Direct HTTP is a debugging fallback or implementation contract, not the
  normal play path.
- Keep a lightweight run log under `notes/` when strategy, mistakes, or API
  behavior teach something reusable.

For exact state fields and action payloads, read
`skills/sts2-play/references/http-api.md`.

For tactical and strategic gameplay policy, read
`skills/sts2-play/references/gameplay-policy.md`.

For logging and self-improvement-loop notes, read
`skills/sts2-play/references/learning-loop.md`.

## Core CLI Commands

Get compact state and drain no-decision screens:

```fish
uv run --directory cli python sts2_fast_cli.py --compact state --drain
```

Execute a fused deterministic turn plan:

```fish
uv run --directory cli python sts2_fast_cli.py --compact act '[{"play":"Shrug It Off+"},{"play":"Uppercut+","target":"first"},{"end_turn":true}]' --drain --max-polls 80
```

Pin a log path for a measured slice:

```fish
uv run --directory cli python sts2_fast_cli.py --log logs/sts2-fast/act2-fight-01.jsonl --compact act '[{"end_turn":true}]' --drain --max-polls 80
```

Analyze logs:

```fish
uv run --directory cli python sts2_fast_cli.py analyze-log 'logs/sts2-fast/act2-fight-01*.jsonl'
```

## Core Loop

1. Read state.
2. Drain no-decision states with the CLI without model deliberation.
3. If a real decision remains, reason from the current state and policy notes.
4. Send the smallest safe batch of deterministic CLI actions.
5. Let the CLI poll until the next ready state.
6. Record decisions, mistakes, timing pain, and new automation candidates.

## No-Decision States

Do not spend reasoning on these once detected:

- claim gold rewards
- claim relic rewards
- claim potion rewards when a potion slot is empty
- proceed from empty reward, rest, treasure, or event screens
- advance event dialogue when there are no options
- claim a single treasure relic
- choose the only available map node
- end a turn when no playable card can improve the result

When a new no-decision state appears repeatedly, update the skill references so
future agents stop paying attention to it.

## Combat Safety Rules

- Re-read state after any card play that changes hand indexes.
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
