# Nick YouTube Playbook

A portable, Resolve-first production pipeline for long-form dark-documentary
videos. The repository combines the editorial research in [`docs/`](docs/README.md)
with **RabbitHole**, the deterministic Python implementation used to turn a
marked narration script into an editable DaVinci Resolve project.

The reusable code covers script review gates, ElevenLabs narration, word-level
timing, media sourcing and provenance, edit decisions, synthetic plates,
editable audio stems, a versioned Resolve manifest/FCPXML compiler, Free/Studio
automation, rendering, and source-inclusive editor handoff.

Real episode projects, source media, credentials, caches, renders, Resolve
archives, and local application state are intentionally excluded from Git.

## Requirements

- Python 3.11 or newer
- DaVinci Resolve 21 or newer
- FFmpeg and ffprobe on `PATH` for conforming, generated plates, audio stems,
  verification, and the explicit legacy backend
- Chrome or Edge for browser-capture operations
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

Or install manually:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in `.env` only for the paid/network services you use. The checked-in
example contains no credentials or account-specific voice identifier.

Run RabbitHole from a repository checkout with this editable installation. The
runner, Fusion templates, schemas, and LUT are versioned repository assets; a
standalone wheel is intentionally not a supported transfer format.

## Episode workflow

Create a Git-ignored episode workspace:

```powershell
rabbithole new my-episode
rabbithole validate projects/my-episode/script/04-final.md
rabbithole narrate projects/my-episode/script/04-final.md `
  --out projects/my-episode/narration/vo.wav --dry-run
rabbithole assets projects/my-episode/narration/timing.json
rabbithole edl projects/my-episode/narration/timing.json
```

Then compile and build the editable Resolve timeline:

```powershell
rabbithole resolve preflight projects/my-episode
rabbithole resolve prepare projects/my-episode
rabbithole resolve build projects/my-episode
```

For Resolve Free, `build` prints a one-line Python loader. Open the intended
project, paste the loader into `Workspace > Console`, and press Enter. You can
also install the same runner under `Workspace > Scripts`:

```powershell
rabbithole resolve install-runner
rabbithole resolve status projects/my-episode
```

The generated timeline is immutable and named `AUTO_BUILD_<hash>`. Duplicate it
to `EDITORIAL_v1` before making human edits; automation never changes or deletes
an `EDITORIAL_` timeline.

Render and package a complete editor handoff:

```powershell
rabbithole resolve render projects/my-episode
rabbithole resolve handoff projects/my-episode --out D:\handoffs
```

The handoff contains a source-inclusive `.dra`, a lightweight `.drp`, style
assets, manifests, checksums, licence notes, and restore/relink instructions.
A `.drp` by itself is not treated as a media archive.

The proven render path remains the general-command default until a local
Resolve Console pilot passes. Resolve remains available explicitly:

```powershell
rabbithole render projects/my-episode/narration/timing.json --backend resolve
rabbithole render projects/my-episode/narration/timing.json --backend ffmpeg
```

See [the Resolve workflow](docs/resolve-workflow.md) for Free/Studio behavior,
safety guarantees, queue states, and editor transfer.

## Local AI / MCP

Install the optional MCP dependency and run the local stdio server:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
rabbithole-resolve-mcp
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
- [`schemas/`](schemas/) — versioned machine contracts
- [`examples/demo-project/`](examples/demo-project/) — fictional, generated
  fixture with no production media
- [`docs/`](docs/README.md) — manual playbook, automation research, and current
  operating guide

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use temporary synthetic media and fake Resolve API objects. They do not
open Resolve, access production episodes, or make network calls.

This is an independent educational analysis of a publicly visible production
style. It is not affiliated with or endorsed by Nick Crowley.
