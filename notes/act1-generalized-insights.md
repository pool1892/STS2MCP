# Generalized STS2 Strategic And Tactical Insights

Purpose: prototype reusable lessons from this Act 1 run that should improve future games.

## Hypotheses To Validate

- Early Act 1 card picks should be judged by whether they improve the next three fights, not by abstract endgame quality.
- Avoiding chip damage is useful, but losing tempo to over-blocking is often worse.
- The agent should maintain a lightweight deck role inventory: damage, block, scaling, AoE, draw, exhaust/discard, potion coverage.

## Candidate Loop Features

- After each reward, record: current deck role gaps, offered cards, pick/skip decision, and expected next-fight value.
- After each combat, record: damage taken, preventable damage, unused energy, missed lethal, and card-index/API mistakes.
- Before each map node, record: HP threshold, elite readiness, shop value, and rest-site need.

## Early Lesson Seeds

- Neow choices should be classified by horizon: immediate combat power, pathing economy, long-term scaling, or high-variance downside. For an Act 1 win goal, immediate combat power is the default unless the map makes pathing unusually valuable.
- Bundle/card-pack decisions should score the package as a role bundle, not as isolated cards. A pack with damage plus mitigation/debuff can be better than individually cleaner cards because it solves multiple Act 1 threats at once.
- Strong opening rewards should loosen map risk tolerance, but only into optional risk. Prefer routes where an elite can be accepted after new information instead of paths that commit to an elite before combat outcomes are known.
- With existing Vulnerable access, multi-hit or efficient attack cards become higher priority than more debuff copies. Avoid duplicating support before the damage base is sufficient.
- Once early damage is solved, the next reward priority should shift toward survivability or scaling rather than more hallway damage. Track this as a phase change in the run, not a static card ranking.
- Expensive cards are acceptable when they are true swing cards. The loop should distinguish "expensive but decisive" from "expensive and redundant"; Howl-style AoE can justify its cost if the deck otherwise has enough cheap actions.
- Card text hypotheses need runtime validation. Howl from Beyond was still strong, but the expected replay was not visible after the first cast; future agents should mark such mechanics as "observed behavior unknown" until tested.
- Against summoned minions with leader mechanics, target the leader when minions have Minion/Illusion-style powers. Killing minions may waste damage if they revive or flee after the leader dies.
- Elite decision rule prototype: take the elite when HP is roughly 65%+ and at least two of these are true: strong front-loaded damage, reliable block, combat potion, or a rest soon after. Here all but immediate rest were true.
- Relic/card rewards can justify an earlier archetype commitment. After Perfected Strike plus multiple Strike-name cards, Hellraiser became more valuable than a generally strong but isolated rare attack.
- Once an archetype emerges, future picks can be more synergistic even if they are not universally best. Stampede is better in an attack-heavy deck with free-play payoffs than it would be in a balanced starter deck.
- After a profitable elite, reassess risk rather than continuing risk momentum. A successful elite can make the run stronger and still make the next elite wrong if HP has fallen below the boss-safety threshold.
- Rest-site choices should be made against the next two expected dangers, not just current HP. If a relic or path gives passive healing, smithing a card that increases boss-fight reliability can be correct even when HP is not full.
- Upgrade priority should follow the deck's current win condition. For a Strike-synergy deck, improving the main payoff card can be better than a generically solid defensive upgrade because it shortens dangerous fights.
- Opening-energy relics should change tactical planning immediately: expensive cards become less clunky on turn 1, and three-card opening lines become available. The agent should recalculate opening-hand plans after energy relic pickups.
- When a deck is already attack-heavy, the next good card can be a cycling block card rather than another damage card. "Block plus draw" is especially valuable because it buys HP without trapping the deck in low-damage turns.
- Against enemies that alternate attacks and debuffs, debuff turns are damage windows. Apply Vulnerable first, then spend remaining energy on attacks if there is no incoming damage to block.
- Enchantments should be assigned to the card that most improves the deck's central plan, not just the card with the biggest standalone effect. A draw-on-play enchantment is especially valuable on an engine power if it reduces the setup-turn penalty.
- Passive rest-site healing changes the rest/smith threshold. With Eternal Feather, the agent should often path through rest sites even when planning to smith, because the node itself is a heal event.
- Boss-prep upgrades should prioritize debuff uptime when the deck already has enough raw damage. More Weak/Vulnerable turns can convert a racing deck into a survivable boss deck.
- When forced to trade a potion for a card right before the boss, keep the potion that covers the boss plan and trade the more replaceable one. Here Potion of Binding stayed because it supports the burst-damage plan; Dexterity Potion was less central.
- Late Act 1 card rewards should have a high inclusion bar. Pick cards that either improve boss defense immediately, increase the main payoff density, or create a clear potion/relic/card combo.
- Observed synergy matters more than imagined synergy. Swift Hellraiser actually triggered major immediate damage after drawing cards, so future runs should value draw-on-play or mass-draw effects more highly with Hellraiser.
- With a guaranteed rest after the fight, it can be correct to accept a controlled hit to remove an enemy immediately. The self-improvement loop should mark this as intentional HP conversion, not preventable damage.
- Final boss preparation should explicitly record the boss loadout: HP, potions, relics, upgraded cards, and the main win condition. This creates a clean comparison point between Act 1 wins and losses.
- When damage and debuff plans are already strong, the last upgrade can rationally be defensive. A single upgraded block card may prevent the one bad boss turn that damage alone cannot solve.
- Boss-specific mechanics should override generic card value. Vantom's Slippery made Perfected Strike+ temporarily low value; the correct plan was to spend cheap hits and Flame Barrier+ thorns to remove Slippery before using large attacks.
- Potion timing should be tied to tactical inflection points. Potion of Binding plus Bottled Potential was best on the first dangerous no-block turn because it both reduced immediate damage and converted the hand into the Hellraiser engine.
- Free-play engines require hand-shaping. With Stampede active, leaving Howl or Bash in hand can be better than playing all possible cards manually; with Hellraiser active, cantrip block cards become both defense and offense.
- Self-improvement logs should distinguish "damage taken due to setup investment" from "avoidable damage." Taking the Vantom hit after playing Hellraiser was acceptable because it removed Slippery, activated the deck's main engine, and led to a fast kill.
- For future Vantom fights, track Slippery count as a first-class tactical variable every turn, alongside enemy HP and incoming damage. The action planner should prefer low-damage hit count until Slippery is gone, then switch to damage-per-energy.
