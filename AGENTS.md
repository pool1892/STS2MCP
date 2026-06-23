# STS2 Fast CLI Gameplay Guide

## Primary Gameplay Workflow

For gameplay runs in this fork, use repo-local skills and the fast CLI before
acting:

- `skills/sts2-play/SKILL.md` for the main play loop.
- `skills/sts2-strategy/SKILL.md` for combat, route, reward, rest, shop, and
  event decisions.
- `skills/sts2-learning-loop/SKILL.md` for run notes, timing friction, and
  self-improvement observations.
- `skills/sts2-play/references/cli-surface.md` for the full CLI command
  surface and MCP parity table.
- `skills/sts2-play/references/http-api.md` for state shape, POST actions, and
  no-decision drain rules.
- `skills/sts2-play/references/gameplay-policy.md` for tactical and strategic
  choices.
- `skills/sts2-play/references/learning-loop.md` for run notes and
  self-improvement logging.

This fork intentionally has one agent-control surface: the repo-local CLI at
`cli/sts2_fast_cli.py`, with the repo-local skills carrying the decision
policy. Do not start, configure, or call a separate tool server for gameplay.
Direct localhost HTTP calls are a debugging fallback and the underlying
contract, not the default agent play surface.
The CLI must preserve feature parity with the original MCP bridge; when adding
or changing mod/API capabilities, update the CLI and
`skills/sts2-play/references/cli-surface.md` together.

When the CLI is wanting, slow, missing a needed capability, or behaving
differently from what optimal gameplay requires, fix the CLI instead of working
around it. If runtime policy permits subagents, spawn a focused subagent for the
CLI improvement and have it update both `cli/sts2_fast_cli.py` and the
corresponding gameplay skill/reference docs in the same slice. If subagents are
unavailable, make the same CLI and skill updates directly before continuing
long-form play.

## CLI State Tips

### State Polling
- After the CLI posts `end_turn`, the game may briefly show
  `is_play_phase: false` or `turn: enemy`. Let the CLI poll until the next
  ready player turn or next screen.
- If debugging with direct HTTP, poll `GET /api/v1/singleplayer?format=json`
  until the state is ready.
- Prefer `uv run --directory cli python sts2_fast_cli.py --compact state --drain`
  for normal play state reads.
- Prefer `uv run --directory cli python sts2_fast_cli.py --compact map` for
  whole-act route planning while on the map screen.
- The underlying structured state is `GET /api/v1/singleplayer?format=json`.

### Card Index Shifting
- **CRITICAL**: Playing a card removes it from hand and shifts all indices. Play cards from RIGHT to LEFT (highest index first) to keep lower indices stable, or re-check state between plays.
- When targeting, always provide `target` for single-target cards. Entity IDs are UPPER_SNAKE_CASE with a `_0` suffix (e.g. `KIN_PRIEST_0`).

### Event & Reward Flow
- Events use the HTTP action `choose_event_option`. After choosing, there is
  often a Proceed option at index 0; the CLI drain should clear it.
- Rest sites use `choose_rest_option`, then `proceed` once complete.
- Rewards use `claim_reward`. Card rewards open a sub-screen; use
  `select_card_reward` or `skip_card_reward`.

### Gameplay Token Efficiency
- Always look for no-decision opportunities and remove them from the agent reasoning loop. If a step has only one valid/reasonable outcome, automate it in the CLI/skill/drain layer instead of spending a turn thinking about it.
- Examples: claiming gold, clicking Proceed, leaving empty reward/rest/treasure screens, taking a single available map node, or fusing deterministic card plays when no target/randomness/ordering decision remains.
- When a repeated no-decision step is found during play, add or update automation and logging so future runs spend tokens only on real strategic or tactical choices.

### Potions
- `use_potion` uses the potion slot index, not a card index.
- `discard_potion` discards a potion to free up the slot when full.
- Potions don't cost energy or count as card plays. Use buff potions BEFORE playing cards.

---

## General Strategy

### Core Principles
1. **HP is a resource, not a score.** Take calculated damage to deal more. Don't waste energy on block when enemies aren't attacking.
2. **Deck quality > deck size.** Skip card rewards if nothing synergizes. A lean deck draws key cards more often.
3. **Front-load damage.** Killing enemies faster means less total damage taken.
4. **Read intents carefully.** Sleep/Buff = go all-out offense. Attack = balance block and damage. Debuff = usually no damage, offense turn.

### Combat Sequencing (General)
1. Play 0-cost utility/setup cards first.
2. Play skills before attacks when possible — many mechanics reward this order (e.g. Slow debuff on enemies stacks per card played).
3. Play biggest attacks last to benefit from accumulated buffs/debuffs.
4. Check enemy HP — if you can kill this turn, skip blocking entirely.
5. Read enemy status effects before every turn plan. Buffs like Ritual,
   Strength gain, and other per-turn scaling are tactical clocks; if an enemy is
   gaining large Strength every turn, shift from setup to damage racing,
   Weak/Vulnerable, and lethal pressure.

### Map Pathing
- **Elites** give relics — fight them when healthy (>70% HP).
- **Rest before Boss** — heal if below 80% HP. Boss fights are long and punishing.
- **Unknown nodes** are safer than Elites. Good at medium HP.
- **Shops** — visit with 100+ gold.
- **Deck quality matters more than quantity** — don't add cards just because they're offered.

### Boss Fights
- **Kill the leader, not the minions.** Enemies with "Minion" power flee when their leader dies.
- Use potions aggressively in boss fights — they don't carry between acts.
- Boss fights are wars of attrition. The longer they go, the more enemies scale with Strength buffs.

### Potion Usage
- Don't hoard potions. Dying with full potions is the worst outcome.
- Use permanent-value potions (Fruit Juice = +5 Max HP) early in any combat.
- Use buff potions (Flex Potion) on turns with multiple attacks.

### Common Mistakes
- Blocking when enemies are sleeping/buffing — waste of energy.
- Not checking card indices after playing — indices shift left.
- Taking too long to kill bosses — enemies scale every turn.
- Adding mediocre cards that dilute the deck before boss fights.
