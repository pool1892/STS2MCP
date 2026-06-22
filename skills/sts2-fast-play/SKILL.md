# STS2 Fast Play Skill

Use this skill when playing Slay the Spire 2 through this repository and the
goal is speed, lower token use, or repeatable self-improvement logs.

## Principle

Prefer the repo-local `sts2-fast` CLI over individual MCP tool calls. The CLI
keeps the HTTP game adapter but batches deterministic operations behind a deeper
interface: card plans, trivial reward draining, transition polling, and timing
logs.

## Setup

Run commands from the repository root unless noted:

```bash
uv run --directory mcp python sts2_fast_cli.py state --drain
```

Every command writes a JSONL timing log by default under
repo-root `logs/sts2-fast/<timestamp>.jsonl`. Use `--log <path>` to pin a log
file. With `uv --directory mcp`, relative pinned paths resolve from `mcp/`, so
use `../logs/sts2-fast/<slice>.jsonl` for repo-root logs.

## Core Commands

Inspect current state and automatically clear no-decision screens first:

```bash
uv run --directory mcp python sts2_fast_cli.py state --drain
```

Drain only trivial screens such as gold rewards, single treasure relics,
completed rest sites, event Proceed options, and maps with exactly one next
node:

```bash
uv run --directory mcp python sts2_fast_cli.py drain
```

Run a fused action plan:

```bash
uv run --directory mcp python sts2_fast_cli.py act '[{"play":"Shrug It Off+"},{"play":"Uppercut+","target":"first"},{"end_turn":true}]' --drain
```

Play multiple cards by name in one command. Use a larger poll budget for
multi-enemy or animation-heavy fights:

```bash
uv run --directory mcp python sts2_fast_cli.py cards "Strike" "Flame Barrier+" "Twin Strike" --target first --end-turn --max-polls 80
```

Summarize timing after a run slice:

```bash
uv run --directory mcp python sts2_fast_cli.py analyze-log ../logs/sts2-fast/<timestamp>.jsonl
uv run --directory mcp python sts2_fast_cli.py analyze-log '../logs/sts2-fast/act2-fight-01-*.jsonl'
```

## Operating Rules

- Do not manually call proceed after rewards/rest/treasure when `sts2-fast drain`
  can do it.
- Do not manually claim gold. Let `drain` claim gold and stop at card choices.
- Do not manually choose a map node when exactly one next node is available.
  Let `drain` choose it and wait for the next ready screen.
- Use `act` or `cards` for deterministic multi-card turns. The CLI re-resolves
  card names after each play, so index shifting is handled locally.
- Use explicit targets for multi-enemy fights when target choice matters.
  Otherwise `target: "first"` or default auto-target is acceptable for
  single-enemy fights.
- Use `end_turn` inside the same `act` command when the turn is finished. The
  CLI polls until the next player turn or the next screen, avoiding empty state
  polling loops.
- Keep card rewards, map choices, shops, rest/smith choices, and event branches
  as decision points unless the state has become a single Proceed option.

## Logging For Self-Improvement

Each log records HTTP timing plus high-level gameplay events:
`planned_action`, `action_result`, `drain_action`, `drain_result`, and
`state_result`. Result events include compact before/after state digests,
gameplay deltas, and the next decision point.

`analyze-log` reports deduped after-state `decision_points` for the actual path
landed on, plus raw `decision_point_events` and phase counts when event-level
accounting matters. Wait events include elapsed milliseconds on new logs. When
analyzing several log files, it reports both
`command_timing.inter_command_gaps` for all CLI commands and
`command_timing.next_post_gaps` for the practical Codex-side delay from one
mutating command to the next. `next_post_gaps.first_post` includes the next
command's local preflight GET/card lookup time before the first POST.

Use this to separate:

- time spent in agent reasoning
- time spent in local HTTP calls
- time spent waiting for game animations or enemy turns
- action count saved by batched commands
- gameplay impact from each fused action plan

For run notes, record the command log path next to the strategic decision log so
gameplay learning and interaction-quality learning can be joined later.

For deliberate measurement slices, pin a named log path:

```bash
uv run --directory mcp python sts2_fast_cli.py --log ../logs/sts2-fast/act2-fight-01.jsonl act '[{"end_turn":true}]'
uv run --directory mcp python sts2_fast_cli.py analyze-log ../logs/sts2-fast/act2-fight-01.jsonl
```
