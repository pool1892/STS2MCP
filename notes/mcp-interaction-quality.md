# MCP And Game-Interaction Quality Notes

Purpose: prototype observations about whether STS2MCP exposes enough reliable state and controls for a gameplay self-improvement loop.

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
- Card-selection control names are easy to mix up: the underlying general endpoint is `select_card`, while MCP-style `deck_select_card` is a useful alias for agent ergonomics. Keep aliases close to MCP names where possible.
- Map screens with exactly one available next node contain no strategic choice. The fast loop should auto-select that node during drain and use the map-specific waiter so early combat/event frames do not leak through.
