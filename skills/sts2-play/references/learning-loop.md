# STS2 Learning Loop

Use this reference when turning gameplay into reusable improvements.

## Notes To Maintain

- `notes/act1-current-run.md`: concrete run log and tactical decisions.
- `notes/act1-generalized-insights.md`: reusable strategic hypotheses and
  lessons.
- `notes/api-interaction-quality.md`: API and CLI interaction notes.

For future long runs, add act-specific notes rather than overwriting Act 1
history.

## What To Record After Decisions

Card rewards:

- offered cards
- chosen card or skip
- deck gap the decision addresses
- expected next-fight value

Combat turns:

- enemy intents and incoming damage
- intended target priority
- potion use or non-use
- damage taken
- missed lethal or preventable damage
- API/index mistakes

Map nodes:

- HP and potion status
- elite readiness
- rest/shop value
- why the selected route fits the run goal

Events:

- option text
- downside accepted or avoided
- card/relic/potion/deck impact

## What To Record About The Interface

- state fields that were missing, stale, or ambiguous
- transient states and how many polls were needed
- action names or indexes that were easy to confuse
- repeated no-decision states that should move out of model reasoning
- timing pain: waiting on game animation, CLI polling, HTTP, or agent
  deliberation

## Efficiency Objective

The agent should spend tokens on strategy, not ceremony. When a screen has only
one reasonable action, perform it through HTTP and mention it only as a drained
step in the handoff.

Good automation candidates:

- proceed-only screens
- single-node maps
- deterministic reward claims
- deterministic card sequences where the CLI resolves card indexes between
  plays
- post-action polling loops handled by the CLI

Keep these lessons in repo-local skills so the next run starts smarter without
needing to rediscover basic control rules.
