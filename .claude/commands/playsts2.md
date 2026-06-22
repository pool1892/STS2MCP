Play Slay the Spire 2 using the repo-local fast CLI. Your goal is to play as well as possible and win the run.

## Setup
1. Read `AGENTS.md` for fast CLI gameplay rules.
2. Read `skills/sts2-play/SKILL.md`.
3. Read `skills/sts2-play/references/gameplay-policy.md` for strategy.
4. Start with `uv run --directory cli python sts2_fast_cli.py --compact state --drain`.

## Gameplay Loop
- **Map**: Evaluate paths. Prefer elites when healthy, rest sites before bosses.
- **Combat**: Read intents. Sequence cards optimally (setup → skills → attacks). Use the CLI `act` command for deterministic batches.
- **Events**: Evaluate options based on current HP, gold, and deck needs.
- **Rewards**: Skip cards that don't synergize. Claim gold and relics. Potions if slots open.
- **Rest Sites**: Heal if below 80% HP before boss. Otherwise upgrade or train (Girya).
- **Shop**: Buy if 100+ gold and something useful is available.

## Important Rules
- Always use the fast CLI for state reads and actions.
- Use `--drain` so no-decision screens do not consume reasoning.
- Use `--max-polls 80` for animation-heavy combat or map transitions.
- Use potions BEFORE playing cards when they grant buffs (e.g. Flex Potion).
- Focus fire on bosses — minions flee when the leader dies.

## Learning & Updating
- Record strategic lessons in `notes/` and reusable control lessons in `skills/sts2-play/references/learning-loop.md`.
- When a repeated no-decision step appears, improve the CLI or skill guidance so future runs spend fewer tokens.
