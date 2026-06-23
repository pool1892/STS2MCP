# HTTP API And Game-Interaction Quality Notes

Purpose: prototype observations about whether the localhost API and fast CLI expose enough reliable state and controls for a gameplay self-improvement loop.

## Baseline Observations

- API root and singleplayer state are available on `127.0.0.1:15526`.
- State shape is nested by screen: event data under `event`, combat data split across `battle` and `player`, map data under `map`.
- The agent must not assume top-level `hand`, `enemies`, or `next_options`; it should read screen-specific nested fields.

## Things To Watch During The Run

- Whether every visible decision has a stable action endpoint and index/id.
- Whether card descriptions, enemy intents, powers, rewards, and map nodes are complete enough for strategy.
- Whether state has transient sparse moments after transitions and how many polls are needed before action.
- Whether error messages are actionable when the agent makes a wrong-mode, wrong-index, or wrong-target request.

## Run Observations

- `bundle_select` state is strategically rich: it exposes bundle index, card ids, names, costs, descriptions, rarities, and keywords. This is enough to make a real card-pack decision without screen vision.
- Reward state is reliable after a short post-combat poll. Claiming rewards shifts indices, so the agent should collect simple rewards before opening card selection, then use the card reward's stable card indexes.
- Card text alone can be strategically ambiguous for unusual mechanics. The API exposed Howl from Beyond's text accurately, but the agent needed post-play state checks to learn that the expected exhaust replay was not immediately available.
- Elite reward state exposed relic name and description after pickup, which was enough to update route planning: Eternal Feather makes future rest sites useful even when not choosing the rest action.
- Smith card selection does not expose upgrade previews in the observed state: every `upgraded_description` field was null. That weakens upgrade decisions because the agent must infer upgrade effects from prior knowledge rather than state.
- After selecting a smith target, `can_confirm` flipped to true but the card entries did not expose `is_selected: true`. For robust automation, the loop should treat `can_confirm` as the confirmation signal but record that the selected index is not explicitly echoed.
- POST action responses can be sparse and often omit the next full state. The interaction loop should poll GET state after every transition instead of assuming a POST response is enough.
- Combat state in this build puts player hand, energy, block, piles, potions, and relics under `player`, while enemy intents live under `battle.enemies`. A general agent should not assume all combat fields live under `battle`.
- Reward card selection uses `card_index`, not `index`, even though the card reward entries expose an `index` field. The failed `select_card_reward` call returned a clear "Missing 'card_index'" error, which made recovery easy.
- Event card-selection screens reused the same `card_select` shape as smithing and showed the enchantment reflected in Hellraiser's description after confirmation. That is good for post-decision verification.
- Card-selection screens still did not echo `is_selected: true` for the selected event card, matching the smith behavior. Confirmation had to rely on `can_confirm`.
- In some non-combat states, `player.master_deck` was empty or absent in the observed JSON even when deck-level verification would have been useful. The loop should verify changed card text opportunistically when the card appears in hand or selection lists.
- Potion rewards exposed the new potion with a stable slot after pickup. The reward list re-indexed after each claim, so the same post-claim poll pattern remained necessary.
- The API made it possible to verify emergent card behavior: after Swift Hellraiser, the enemy HP change and active `HELLRAISER_POWER` confirmed that draw-triggered Strike-name cards were being auto-played.
- Combat intents with multiple components are represented as multiple intent objects for one enemy. The tactical loop should sum attack labels for damage while also preserving buff/debuff intent labels for qualitative planning.
- Boss state exposed special powers clearly enough for planning: Vantom's `SLIPPERY_POWER` amount and description were available in enemy status, which enabled a mechanics-aware plan.
- Enemy intent labels update after debuffs: after Potion of Binding and Uppercut+, the attack labels reflected reduced damage. This is very useful because the planner can rely on the current intent label after applying Weak.
- Start-of-turn Hellraiser triggers can apparently finish combat before the next player action. After ending the near-lethal turn, the next poll was already in `rewards` with Vantom gone. The loop should poll after end-turn transitions before assuming another player action is needed.
- The API does not provide an explicit combat summary, damage log, or "last action caused X" trace. For self-improvement, the agent had to infer important events such as Stampede firing Bash/Howl or Hellraiser landing lethal from before/after HP deltas.
- Event transitions can briefly expose `state_type: event` with no options even when the event is not a dialogue screen. Brain Leech showed empty options on the first post-map state and real options on the next read. The fast loop should treat non-dialogue events with no options as transient and keep polling.
- Shop state can report `can_proceed: false` even though a direct `proceed` action succeeds. Shops should remain explicit decision points while stocked/affordable choices remain, but once the agent has decided to leave it should send `proceed` directly instead of trusting `can_proceed`.
- Multi-enemy, multi-hit turns can exceed a 30-poll end-turn wait even when the game resolves correctly. `cards --max-polls 80` or `act --max-polls 80` is the safer default for Act 2+ combats with long animations.
- The timing logs now separate practical latency into HTTP time, wait/poll elapsed time, active CLI wall time, and next-post gaps between mutating commands. For Codex efficiency work, `command_timing.next_post_gaps.first_post` is the most direct measure of "time until the next game-changing POST."
- Card-selection control names are easy to mix up: the underlying HTTP action is `select_card`, followed by `confirm_selection` when the selection can be confirmed.
- Map screens with exactly one available next node contain no strategic choice. The fast loop should auto-select that node during drain and use the map-specific waiter so early combat/event frames do not leak through.

## 2026-06-22 CLI Performance Slice

- Optimization applied: default mutating-command `poll_delay` changed from 120ms to 40ms, default mutating max-poll budgets were raised to preserve transition headroom, and `drain_trivial` no longer pays a blind sleep after every no-decision POST.
- New no-decision rule: when a shop still has stocked items but none are affordable, `drain` should leave the shop with `proceed`. The live Act 2 shop exposed this: after buying Fisticuffs and Strength Potion, all remaining stocked items were unaffordable, yet the API accepted direct `proceed` even though `can_proceed` was false.
- Pre-change comparison slice: `logs/sts2-fast/postpatch-fight-03-turn-*.jsonl` measured 32,759.9ms active CLI wall for 22 mutating/drain actions, or 1,489.1ms/action.
- Post-change live slice: `logs/sts2-fast/perf-goal-postopt-*.jsonl` measured 54,087.4ms active CLI wall for 52 mutating/drain actions, or 1,040.1ms/action across the whole slice. The clean mutating-run denominator is 52,103.7ms for 52 actions, or 1,002.0ms/action.
- Result: all-action average improved from 1,489.1ms/action to 1,040.1ms/action, a 30.2% reduction. Mutating-action average improved from 1,480.1ms/action to 1,002.0ms/action, a 32.3% reduction.
- No-decision command examples after the change: opening a card reward took 215ms, boss gold drain took 175ms, and boss card-reward open took 213ms active CLI wall.
- Remaining bottleneck: boss combat is still dominated by real wait spans around card animations, draw-heavy cards, end-turn animations, and delayed auto-effects. In the Act 2 boss slice, 44,565.8ms of 51,035.0ms active wall was wait elapsed, with 37,170.3ms of non-HTTP wait.
- Wait-quality edge case: delayed powers/effects can complete after the CLI returns a combat-looking state. During The Insatiable, the CLI returned a boss state after `Thinking Ahead`; before the next POST, delayed effects had killed the boss and moved to rewards. The next attempted card play failed safely before posting. The timing fix pass below now covers this class with targeted delayed-card stability polls.
- Gameplay-state edge case: raw `draw_pile` order was not reliable enough to plan deterministic follow-up draws. `Shrug It Off` drew `Vicious` when the prior raw draw pile made a `Defend` draw look plausible. Treat draw from pile lists as uncertain unless the API explicitly marks top-of-deck order.
- Follow-up optimization: the CLI now uses an adaptive default poll cadence (15ms for the first 4 polls, 20ms for polls 4-7, then 40ms) and adds targeted extra stability polls for delayed-effect card plays. This is meant to catch draw/auto-effect transitions like `Thinking Ahead` without adding broad sleeps to every action.
- Documentation follow-up: the canonical CLI contract is now `skills/sts2-play/references/cli-surface.md`, with commands, flags, action shorthands, old MCP aliases, no-decision drain rules, waiting behavior, timing log fields, multiplayer routing, and extension rules linked from the gameplay skill.
- Live Act 3 discrepancy: after using Skill Potion, the API entered a `card_select` prompt while raw state still retained `player.hand`. The CLI previously allowed `play_card` and `end_turn` from that modal state, producing successful-looking POSTs that left the prompt unchanged until timeout. The CLI now rejects combat-only actions outside combat so selection screens must be resolved first.
- Tactical lesson: enemy status effects must be part of the core turn parser. Devoted Sculptor's `Ritual 9` meant the fight was a damage race; missing that clock led to slow setup and preventable danger. The state exposes this under `combat.enemies[*].status`, so future planning should explicitly inspect and react to scaling buffs each turn.

## 2026-06-22 Timing Fix Pass

- Live Act 3 exposed four related readiness bugs: map entry briefly returned sparse combat before a delayed `card_select`, `end_turn` could return a retained-card-only hand before the full draw/relic queue, `Thinking Ahead` could return combat before delayed `hand_select`, and draw/auto-play chains could return combat before a late reward transition.
- The CLI now treats combat readiness as a stable decision boundary rather than "one legal-looking poll." Map-to-combat and next-player-turn waits require several matching digests, while non-combat screens still settle quickly.
- Delayed card plays now receive a larger settle window, and likely selection-opening cards receive extra budget. This is targeted at draw, auto-play, generated-card, end-of-action, and hand/card-selection effects rather than adding broad sleeps to every action.
- Generic state-change waits now apply the same stable-combat rule when an action lands back in combat, including hand-selection confirmation after Thinking Ahead-style effects.
- Waiters now fail closed when a changed state never stabilizes before the poll budget expires. Returning an explicitly unsettled state was the same failure class as the original timing bug.
- Combat readiness no longer depends on having a playable card. A real turn can have no playable actions, so readiness is now based on player turn/play phase plus an exposed hand list, with stability polls doing the transient filtering.
- Regression coverage now includes the exact failure shapes: early combat before delayed modal, playable retained-card transient before full turn state, delayed `hand_select` after `Thinking Ahead`, partial modal frames after card play, late rewards after draw/auto effects, late rewards after confirming a hand-selection modal, and changed-but-never-stable timeout paths.

## 2026-06-22 Act 3 Boss Loss Slice

- Whole-deck rule worked for card choices: `Inflame+`, `Taunt`, and generated
  `Inflame` were chosen because they fit the actual Strike/Vulnerable/Vicious
  boss plan, not because of isolated card strength.
- Modal macros worked in live play: `card_select_pick` resolved Choices Paradox
  choices and skipped confirmation when selection immediately exited the modal;
  `deck_pick` and `hand_pick` had already been exercised earlier in the run.
- `--fast-action-waits` exposed a real safety boundary before this boss slice:
  when used too broadly in a multi-card batch, a stale hand/index frame caused
  an intended follow-up play to resolve to the wrong card. The CLI fix now keeps
  intermediate card plays conservative and only permits fast waits on the final
  simple card play in a command.
- Queen loss root cause was gameplay, not a CLI failure: the state exposed
  `Chains of Binding`, Frail, Weak, Vulnerable, minion Strength, and the minion
  attack intents clearly. The planner underweighted how rapidly the minion's
  Strength would make the leader-race plan untenable.
- Bound should be handled as a turn-shaping constraint before card ordering.
  In Queen, the first three drawn cards were often Bound, and only one Bound
  card could be played. Planning "draw into block" or "play the damage card
  after setup" was invalid unless the selected Bound card had already been
  fixed.
- Raw `draw_pile` remains useful for context but not deterministic enough for
  exact follow-up planning after shuffles, triggered draws, or Vicious draws.
  The floor 48 boss turn showed displayed raw order suggesting Defends, while
  Vicious actually drew Ashen Strike and Thinking Ahead. The correct loop is to
  stop after draw-changing actions and re-read state unless every possible draw
  leaves the same action correct.
- Howl from Beyond text is still not sufficient for timing assumptions. It did
  not replay before enemy attacks in the hallway fight where the planner
  expected end-of-turn lethal, causing a large preventable hit. Treat unusual
  delayed card text as "needs observation" until verified in the current build.
- The CLI's fail-closed behavior prevented compounding one bad draw assumption:
  a batch that expected Defends stopped when `Defend` was absent, preserving
  the actual mid-turn state for recovery.

## 2026-06-22 Fresh-Run Blocker After Game Over

- After the Act 3 game over, the main menu exposed only `settings` and `quit`;
  `singleplayer` was absent because `blocked_options` contained `timeline` with
  reason `manual_epoch_reveal_required` and pending epoch ids.
- Live canaries confirmed this is an intentional API boundary, not a polling
  issue: `menu timeline` returned `manual_action_required`, and `menu advance`
  returned `Unknown menu option`.
- The CLI should surface this as a manual Timeline reveal requirement before
  promising that `start-run` can begin from game over. This is exactly the kind
  of boundary where the agent must ask for the real manual action instead of
  inventing a fake start path.
