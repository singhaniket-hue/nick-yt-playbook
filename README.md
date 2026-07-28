# Nick YouTube Playbook

A portable, Resolve-first production pipeline for long-form dark-documentary
videos. The repository combines the editorial research in [`docs/`](docs/README.md)
with **RabbitHole**, the deterministic Python implementation used to turn a
marked narration script into an editable DaVinci Resolve project.

The reusable code covers script review gates, ElevenLabs narration, word-level
timing, media sourcing and provenance, edit decisions, synthetic plates,
editable audio stems, a versioned Resolve manifest/FCPXML compiler, Free/Studio
automation, rendering, and source-inclusive editor handoff.

The committed `produce-resolve-documentary` agent skill guides the missing
editorial front end from a topic through research, claims, script, approvals,
and the deterministic CLI. The CLI itself still begins with an authored marked
script; topic-to-script is not an unattended executable stage.

Real episode projects, source media, credentials, caches, renders, Resolve
archives, and local application state are intentionally excluded from Git.

## Requirements

- Windows 10/11 x64, or macOS 12+ on Intel or Apple silicon, on hardware
  supported by DaVinci Resolve
- Python 3.11 or newer
- DaVinci Resolve 21 or newer
- FFmpeg and ffprobe on `PATH` for conforming, generated plates, audio stems,
  verification, and the explicit legacy backend. Subtitle and graphic-card
  generation requires FFmpeg's `ass` filter (libass).
- Chrome, Edge, or Chromium for browser-capture operations
- Poppler's `pdftoppm` for PDF evidence capture
- an ElevenLabs API key and voice ID only when using narration or sound
  generation

Resolve Free uses an in-app runner. Resolve Studio may use the external scripting
bridge. The timeline compiler, queue, safety checks, and handoff format are the
same in both editions.

The checked-in runner requires Python 3.11 or newer. Before the first Free
Console run, execute `import sys; print(sys.version)`. If Resolve reports an
older Python, the loader stops before claiming the queued job; import the
generated FCPXML manually, keep the FFmpeg backend, or use Studio's external
bridge from a supported Python environment.

## Setup

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

On macOS:

```bash
# Homebrew's regular FFmpeg 8 formula omits libass; use the keg-only full build.
brew install uv ffmpeg-full poppler
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
bash scripts/bootstrap.sh
```

Or use the identical manual setup on either platform:

```text
uv sync --extra dev
uv run rabbithole resolve doctor --mode free
```

Fill in `.env` only for the paid/network services you use. The checked-in
example contains no credentials or account-specific voice identifier.
`RABBITHOLE_RESOLVE_PATH` and `RABBITHOLE_BROWSER_PATH` can point to
nonstandard application locations. `RABBITHOLE_PROJECTS_DIR` can place ignored
episode workspaces on another local volume.

Run RabbitHole from a repository checkout with this editable installation. The
runner, Fusion templates, schemas, and LUT are versioned repository assets; a
standalone wheel is intentionally not a supported transfer format.

## Episode workflow

Create a Git-ignored episode workspace:

```text
uv run rabbithole new my-episode
uv run rabbithole validate projects/my-episode/script/04-final.md
uv run rabbithole narrate projects/my-episode/script/04-final.md --out projects/my-episode/narration/vo.wav --dry-run
uv run rabbithole assets projects/my-episode/narration/timing.json
uv run rabbithole edl projects/my-episode/narration/timing.json
```

For voice cloning, `script/05-devanagari.md` is deliberately mixed-script:
write Hindi words in Devanagari, but keep English words, brands, acronyms, and
technical terms in Latin. Never transliterate an English word into Devanagari.
Good: `रात हो चुकी है, घर में finally silence है।` Bad:
`रात हो चुकी है, घर में फाइनली साइलेंस है।` Write `account`, not `अकाउंट`.
Maintain the episode's authoritative English-token list in
`script/latin-terms.json` as `{"terms": ["account", "finally", "silence"],
"occurrences": []}`. For a Hindi/English homograph such as `is`, omit the
global term and declare only its English use with a one-based spoken-word
position: `{"token": "is", "word_index": 705}`. Validation and narration
refuse a TTS edition that converts a listed term or leaves an undeclared Latin
word.

`validate` and `narrate` both read the sibling episode `brief.json`. When it
contains a positive numeric `target_duration_minutes`, they derive the spoken
word target from `target_wpm` (or the configured/default WPM), allow the
specified `validation.word_count_tolerance` (default +/-15%), and scale all
five act budgets proportionally. An approved beat plan can instead provide
five explicit `validation.act_word_budgets`, or five
`validation.act_duration_seconds` values to convert at the selected WPM.
`--wpm` explicitly overrides `target_wpm` for either command. With no brief or
no duration field, the original 4,500-7,400-word gate and fixed
90/440/3360/1590/530 act budgets remain unchanged. A present but malformed
brief is an error rather than a silent fallback, including for
`narrate --force`, so narration cannot spend money against an unknown target.

Then compile and build the editable Resolve timeline:

```text
uv run rabbithole resolve preflight projects/my-episode
uv run rabbithole resolve prepare projects/my-episode
uv run rabbithole resolve build projects/my-episode
```

For Resolve Free, `build` prints a one-line Python loader. Open the intended
project, paste the loader into `Workspace > Console`, and press Enter. You can
also install the same runner under `Workspace > Scripts`:

```text
uv run rabbithole resolve install-runner
uv run rabbithole resolve status projects/my-episode
```

The generated timeline is immutable and named `AUTO_BUILD_<hash>`. Duplicate it
to `EDITORIAL_v1` before making human edits; automation never changes or deletes
an `EDITORIAL_` timeline.

Render and package a complete editor handoff:

```text
uv run rabbithole resolve render projects/my-episode
uv run rabbithole resolve handoff projects/my-episode --out <handoff-directory>
```

The handoff contains a source-inclusive `.dra`, a lightweight `.drp`, style
assets, manifests, checksums, licence notes, and restore/relink instructions.
A `.drp` by itself is not treated as a media archive.

The proven render path remains the general-command default until a local
Resolve Console pilot passes. Resolve remains available explicitly:

```text
uv run rabbithole render projects/my-episode/narration/timing.json --backend resolve
uv run rabbithole render projects/my-episode/narration/timing.json --backend ffmpeg
```

See [the Resolve workflow](docs/resolve-workflow.md) for Free/Studio behavior,
safety guarantees, queue states, and editor transfer.

## Move an episode to another machine

Git carries the reusable application and the committed
[`produce-resolve-documentary`](.agents/skills/produce-resolve-documentary/SKILL.md)
skill. Because episode workspaces and media are deliberately Git-ignored, use a
checksum-verified bundle before Resolve assembly:

```text
uv run rabbithole resolve bundle projects/my-episode --out my-episode.bundle.zip
uv run rabbithole resolve verify-bundle my-episode.bundle.zip
uv run rabbithole resolve restore-bundle my-episode.bundle.zip --out projects/my-episode
```

Run `doctor`, `preflight`, and `prepare` again on the receiving machine. The
bundle excludes credentials, stale locks/queues, generated Resolve builds, and
prior renders. After Resolve assembly, use the source-inclusive DRA/DRP handoff
instead:

```text
uv run rabbithole resolve verify-handoff <handoff.zip>
uv run rabbithole resolve restore-handoff <handoff.zip> --out <restore-directory>
```

For a Mac mini used as the render host, clone this repository, run
`bash scripts/bootstrap.sh`, restore the episode bundle, and then run:

```text
uv run rabbithole resolve doctor --mode <free|studio>
uv run rabbithole resolve install-runner
uv run rabbithole resolve preflight projects/my-episode --mode <free|studio>
uv run rabbithole resolve prepare projects/my-episode
uv run rabbithole resolve build projects/my-episode --mode <free|studio>
uv run rabbithole resolve render projects/my-episode --mode <free|studio>
```

Resolve Free still requires its one-time Console Python version check and the
in-app loader. Studio can use the automatically discovered external bridge.

## Local AI / MCP

Install the optional MCP dependency and run the local stdio server:

```text
uv sync --extra dev --extra mcp
uv run rabbithole-resolve-mcp
```

On Resolve Free, MCP tools compile and queue work, then return
`awaiting_in_app_runner`; the in-app runner performs supported Resolve API calls.
On Studio, the same tools can use the external scripting bridge when explicitly
selected. No tool can stop an existing render, clear a queue, quit Resolve, load
another project, or delete an editor timeline.

## Repository map

- [`rabbithole/`](rabbithole/) — reusable application and Resolve adapters
- [`resolve/`](resolve/README.md) — style contract and Free-compatible Fusion
  templates
- [`style/`](style/) — palette, typography, sound map, and deterministic LUT
- [`pipeline/crowley-hinglish.yaml`](pipeline/crowley-hinglish.yaml) — stage and
  checkpoint contract
- [`.agents/skills/produce-resolve-documentary/`](.agents/skills/produce-resolve-documentary/SKILL.md)
  — repository-local topic-to-Resolve operating skill
- [`schemas/`](schemas/) — versioned machine contracts
- [`examples/demo-project/`](examples/demo-project/) — fictional, generated
  fixture with no production media
- [`docs/`](docs/README.md) — manual playbook, automation research, and current
  operating guide

## Tests

```text
uv run pytest
```

Tests use temporary synthetic media and fake Resolve API objects. They do not
open Resolve, access production episodes, or make network calls.

This is an independent educational analysis of a publicly visible production
style. It is not affiliated with or endorsed by Nick Crowley.
