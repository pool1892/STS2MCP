<p align="center">
  <img src="docs/teaser.png" alt="STS2 fast CLI" width="90%" />
</p>

<p align="center"><em>An Experimental Research Project to Fully-Automate your Slay the Spire 2 Runs</em></p>

A mod for [**Slay the Spire 2**](https://store.steampowered.com/app/2868840/Slay_the_Spire_2/) that lets AI agents play the game. Exposes game state and actions via a localhost REST API, with a repo-local fast CLI and gameplay skills for agent-driven runs.

Singleplayer and multiplayer (co-op) supported, plus full menu and lobby control: profile switching, character select (SP and MP host/client) with optional seed, multiplayer host / Steam-friend join / FastMP localhost join, multiplayer load lobby for resuming saved co-op runs, game-over dismissal, FTUE/tutorial popup handling, and Timeline visibility. Tested against STS2 `v0.107.1`.

> [!warning]
> This mod allows external programs to read and control your game via a localhost API. Use at your own risk with runs you care less about.

> [!caution]
> Multiplayer support is in **beta** — expect bugs. Any multiplayer issues encountered with this mod installed are very likely caused by the mod, not the game. Please disable the mod and verify the issue persists before reporting bugs to the STS2 developers.

## For Players

### 1. Install the Mod

Grab the [latest release](https://github.com/Gennadiyev/STS2MCP/releases/latest) and follow the instructions:

1. Copy `STS2_MCP.dll` and `STS2_MCP.json` to `<game_install>/mods/`
2. Launch the game and enable mods in settings (a consent dialog appears on first launch)
3. The mod starts an HTTP server on `127.0.0.1:15526` and `localhost:15526` automatically

> [!note]
> The release DLL is a platform-agnostic .NET assembly — the same `STS2_MCP.dll` and `STS2_MCP.json` work on Windows, Linux, and macOS. No separate builds are needed.

#### macOS install

On macOS, the mods directory lives inside the app bundle. The default Steam install path is:

```
~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/
    SlayTheSpire2.app/Contents/MacOS/mods/
```

To install, right-click `SlayTheSpire2.app` → **Show Package Contents**, navigate to `Contents/MacOS/`, and create a `mods` folder. Or from the terminal:

```bash
GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
MODS_DIR="$GAME_DIR/SlayTheSpire2.app/Contents/MacOS/mods"
mkdir -p "$MODS_DIR"
cp STS2_MCP.dll "$MODS_DIR/"
cp STS2_MCP.json "$MODS_DIR/"
```

Launch the game and open **Settings → Mods**. The mod should appear in the list. A consent dialog appears on first launch — accept it to enable mod loading. Once enabled, verify the HTTP server is running:

```bash
curl -s http://127.0.0.1:15526/
```

A successful response includes an `ok` status, for example:

```json
{"status": "ok"}
```

If you get "Connection refused", the mod is not loaded — check that mods are enabled in the game's settings.

### 2. Give Your AI Instructions to Interact with the Game

**Clone or download the repository**, then tell your AI agent to load
`skills/sts2-play/SKILL.md`. Gameplay in this fork should always go through the
fast CLI, not a separate tool server.

#### Fast CLI setup

Install [uv](https://docs.astral.sh/uv/) if you don't have it (macOS: `brew install uv`). Then run the CLI once to install dependencies:

```bash
uv run --directory /path/to/repo/cli python sts2_fast_cli.py --help
```

`uv` reads `cli/pyproject.toml`, creates an isolated virtual environment, and installs the pinned dependencies from `cli/uv.lock`. Subsequent runs reuse the environment instantly.

Common gameplay commands:

```bash
uv run --directory cli python sts2_fast_cli.py --compact state --drain
uv run --directory cli python sts2_fast_cli.py --compact map
uv run --directory cli python sts2_fast_cli.py --compact start-run --character ironclad
uv run --directory cli python sts2_fast_cli.py --compact menu main_menu
uv run --directory cli python sts2_fast_cli.py --compact act '[{"play":"Shrug It Off+"},{"play":"Uppercut+","target":"first"},{"end_turn":true}]' --drain --max-polls 80
uv run --directory cli python sts2_fast_cli.py --compact act '[{"hand_pick":1}]'
uv run --directory cli python sts2_fast_cli.py analyze-log 'logs/sts2-fast/act2-fight-01*.jsonl'
```

The CLI accepts `--base-url`, `--timeout`, `--trust-env`, `--log`, and
`--compact` flags, plus `--no-log` to disable JSONL logging and
`--multiplayer` to route run state/actions through the multiplayer endpoint. It
talks to the same localhost HTTP API exposed by the game mod, batches
deterministic actions, drains no-decision screens, polls transitions, waits
through transient states, requires stable combat decision frames after map and
end-turn transitions, and writes timing logs by default.

#### Fast CLI command surface

The CLI is intended to cover the original MCP bridge without requiring an MCP
server:

- `state`: concise current run state; use `--drain`, `--verbose`, or
  `--raw-format json|markdown` for MCP-compatible state output. Compact combat
  state includes a `combat.tactical` checksum for incoming damage, enemy attack
  totals, player statuses, and hard constraints such as Bound.
- `map`: full current act map graph for route planning while on a map screen.
- `start-run`: walk main menu -> singleplayer mode -> character select ->
  embark for a fresh singleplayer run. If post-game Timeline epochs require
  manual reveal before singleplayer returns, the CLI reports the pending ids
  instead of trying to bypass the manual game ceremony.
- `menu`: select visible menu, lobby, Timeline, tutorial/popup, profile, or
  game-over options through the old `menu_select` action.
- `act`: execute a JSON action plan. It accepts ergonomic shorthands like
  `{"play":"Strike"}` and old MCP tool-name aliases like
  `{"action":"rewards_claim","reward_index":0}`. Use pick macros such as
  `{"hand_pick":1}` or `{"deck_pick":4}` for one-decision select+confirm
  modal screens, and `--fast-action-waits` when the final action in a command
  is a simple card play that does not need a conservative post-action state.
- `cards`: play card names in order, with optional `--target`, `--end-turn`,
  and `--drain`.
- `drain`: resolve no-decision screens without model deliberation.
- `profile`, `compendium`, `wiki`, `profiles`, `switch-profile`,
  `delete-profile`: profile progress, durable lookup, and profile slot tools.
- `analyze-log`: summarize JSONL timing logs, including HTTP, wait, stdout,
  local overhead, and next-POST gaps.

Multiplayer can be driven with global `--multiplayer`:

```bash
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact state
uv run --directory cli python sts2_fast_cli.py --multiplayer --compact act '[{"map":0}]'
```

Or with old MCP `mp_*` action aliases:

```bash
uv run --directory cli python sts2_fast_cli.py --compact act '[{"action":"mp_map_vote","node_index":0}]'
uv run --directory cli python sts2_fast_cli.py --compact act '[{"action":"mp_combat_end_turn"}]'
```

The full command catalog and MCP parity table live in
`skills/sts2-play/references/cli-surface.md`. That reference is the canonical
CLI contract for commands, flags, action shorthands, old MCP aliases,
no-decision drain rules, waiting behavior, timing logs, multiplayer routing,
and extension rules. Agents should load `skills/sts2-play/SKILL.md` first; it
points to the CLI reference and gameplay policy.

### Profile and Compendium Data

The HTTP API exposes profile-level progress separately from live run state:

- `GET /api/v1/profile` returns the active profile's persistent progress summary, including discoveries, achievements, epochs, character totals, and global run totals.
- `GET /api/v1/compendium` groups that progress into the same high-level sections as the in-game Compendium: Card Library, Relic Collection, Potion Lab, Bestiary, Character Stats, and Run History.
- `GET /api/v1/wiki?query=...` searches discovered card and relic wiki entries for the active profile with fuzzy matching. Results are limited to 10 by default and can be overridden with `limit`; card results include base and upgraded variants when available.
- `GET /api/v1/profiles` lists the three profile slots and the active profile.
- `POST /api/v1/profiles` switches or deletes profile slots through the game UI.

The fast CLI and helper scripts should use these localhost HTTP endpoints
directly when profile data is needed.

`GET /api/v1/compendium` is intended for agents that need durable context outside the current room or current run. It works from the main menu, includes a `current_run` block while a run is active, and summarizes saved `saves/history/*.run` files for the active profile. Run history is capped to the 20 most recent files in the response so long-lived profiles do not create unbounded output.

`GET /api/v1/wiki?query=...` is the selective lookup path for durable card and relic text. It never returns the full game catalog: the mod first filters to the active profile's discovered card and relic IDs, then returns only the best fuzzy matches. Use `item_type="card"` or `item_type="relic"` when the query is known, and raise `limit` only when the default 10 results are not enough.

## For Developers

### Build & Install

Requires [.NET 9 SDK](https://dotnet.microsoft.com/download/dotnet/9.0) and the base game.

**PowerShell** (recommended):

```powershell
# Pass game path directly:
.\build.ps1 -GameDir "D:\SteamLibrary\steamapps\common\Slay the Spire 2"

# Or set it once and forget:
$env:STS2_GAME_DIR = "D:\SteamLibrary\steamapps\common\Slay the Spire 2"
.\build.ps1
```

The script builds `STS2_MCP.dll` into `out/STS2_MCP/`. Copy it along with the manifest JSON to `<game_install>/mods/` to install:

```
out/STS2_MCP/STS2_MCP.dll           ->  <game_install>/mods/STS2_MCP.dll
mod_manifest.json                   ->  <game_install>/mods/STS2_MCP.json
```

### Build instructions for macOS

Install dotnet to compile the mod:

```bash
brew install dotnet@9
export DOTNET_ROOT="/opt/homebrew/opt/dotnet@9/libexec"
export PATH="$DOTNET_ROOT:$PATH"
```

Homebrew installs `dotnet@9` as keg-only, so the exports above are required for the current session. Add them to `~/.zshrc` to persist across sessions.

Build with `dotnet` directly (the PowerShell script is Windows-only):

```bash
dotnet build STS2_MCP.csproj -c Release -o out/STS2_MCP \
  -p:STS2GameDir="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
```

On macOS the game ships as an app bundle. The `.csproj` detects macOS and resolves the data directory to `SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64` automatically.

The mods directory on macOS lives inside the app bundle at `SlayTheSpire2.app/Contents/MacOS/mods/`. Finder hides bundle contents by default — to browse it in the GUI, right-click `SlayTheSpire2.app` → **Show Package Contents**. Or copy from the terminal:

```bash
GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
MODS_DIR="$GAME_DIR/SlayTheSpire2.app/Contents/MacOS/mods"
mkdir -p "$MODS_DIR"
cp out/STS2_MCP/STS2_MCP.dll "$MODS_DIR/"
cp mod_manifest.json "$MODS_DIR/STS2_MCP.json"
```

> [!NOTE] 
> `mod_manifest.json` is renamed to `STS2_MCP.json` on copy — the game's mod loader expects the manifest filename to match the mod ID.

## License

MIT

## FAQ

### Why let the AI play the game for me?

I start building this mod with the hope that I can co-op with an AI player. Singleplayer is originally just built for validation.

### You did not answer the question!

First of all, I play lots of games, including service games that has daily/weekly tasks. I really hoped that modern AI could save me from the grind, which, if you have tried one or more of the GUI agents, never really materialized. Let's face it: modern AI is still pretty bad at gaming because no one cares.

About my intention, as a researcher that loves playing games, the purpose of this project is to test AI models and agents in a rarely explored (we call it out-of-distribution) domain. Ultimately, this might turn into a benchmark for evaluating the reasoning and decision-making capabilities of different language models.

STS2 is just an example to show how good (or bad) current AI agents are at playing such games. **I have no intention to replace human players with AI, and I would definitely rather play STS2 myself** as a big fan of the game.

### Is this a cheat mod?

It can be, but it doesn't have to be. The mod itself does not alter the gameplay. It is just an interface that allows external programs to interact with the game. What you do with that interface is up to you.

### How many tokens do a run consume?

I evaluated on the Ironclad. Claude Sonnet 4.6 uses slightly more than 8M tokens for a full run. GPT-5.4 averages 7.34M tokens. Depending on your prompt and model choice, it can be more or less.

### Do you have a roadmap for future features?

The project is still too early to have a clear roadmap. My current focus is to make sure the core features are stable and well-documented. However, I am open to suggestions and contributions from the community.

- Solidifying multiplayer features and fixing bugs is a priority
- Add support for in-game communication in multiplayer runs when collaborating with an AI agent
- Self-reflection and learning from past runs to improve future performance
- Benchmarking different models and agents is also on my mind
