# DaVinci Resolve automation workflow

This repository keeps RabbitHole's narration timing, edit decisions, and media
provenance as the source of truth, but makes DaVinci Resolve the primary editing
and finishing environment.

The integration is deliberately hybrid:

- deterministic Python compiles `timing.json`, `edit/edl.json`, and
  `provenance.json` into a versioned Resolve plan and FCPXML;
- DaVinci Resolve Free executes the prepared job from its in-app Python console
  or `Workspace > Scripts`;
- DaVinci Resolve Studio may execute the same job through the external scripting
  bridge;
- FFmpeg remains available for media preparation, audio stems, generated plates,
  and a documented legacy render fallback.

The compiler never treats a Resolve timeline as the canonical data model. This
makes rebuilds reproducible and editor handoffs auditable.

## Prerequisites

- Windows 10/11 x64, or macOS 12+ on Intel/Apple silicon hardware
  supported by Resolve
- Python 3.11 or newer
- FFmpeg and ffprobe on `PATH` for media preparation and the legacy backend.
  Subtitle and graphic-card generation requires the `ass` filter (libass).
- Poppler's `pdftoppm` for PDF evidence and Chrome/Edge/Chromium for browser
  capture
- DaVinci Resolve 21 or newer; the implementation targets its installed
  scripting documentation, while the local live-application pilot remains a
  release gate
- source media bound in the episode's `provenance.json`
- an approved `narration/timing.json` and `edit/edl.json`

Install the Python project:

```text
uv sync
uv run rabbithole resolve doctor --mode free
uv run rabbithole --help
```

Use `scripts/bootstrap.ps1` on Windows or `scripts/bootstrap.sh` on macOS.
`doctor` is read-only: it does not launch Resolve, open a project, or touch the
render queue. Set `RABBITHOLE_RESOLVE_PATH` or `RABBITHOLE_BROWSER_PATH` only
for a nonstandard application location.

On macOS, Homebrew's regular FFmpeg 8 formula omits libass. Install and select
the full keg before running the pipeline:

```bash
brew install ffmpeg-full poppler
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
```

## Project contract

A renderable episode has this minimum layout:

```text
projects/<slug>/
  narration/
    timing.json
    vo.wav
  edit/
    edl.json
  research/
    source-audio.json        # optional
    highlights.json          # optional
  assets/
  provenance.json
```

Resolve-specific generated state is isolated under `projects/<slug>/resolve/`.
It is never written into `assets/`, `narration/`, `edit/`, or the append-only
provenance audit trail. The one deliberate provenance exception is the
recoverable `assets --refresh --slot ...` workflow: it retains old media in
project-local quarantine and converts its record into an unclaimed retirement
entry instead of deleting history. See
[Asset acquisition and visual QA](./asset-acquisition-workflow.md).

## DaVinci Resolve Free

Resolve Free can run supported scripting calls inside the application. It does
not expose the external scripting connection used by an MCP server or a normal
terminal process. The normal Free workflow is therefore prepare outside Resolve,
execute inside Resolve:

```text
uv run rabbithole resolve preflight projects/<slug>
uv run rabbithole resolve prepare projects/<slug>
uv run rabbithole resolve build projects/<slug>
```

`build` writes a durable queued job and prints the exact one-line loader for that
checkout. In Resolve:

1. Open the project that should receive the generated timeline.
2. Open `Workspace > Console`, select Python 3, paste the printed loader, and
   press Enter.
3. Alternatively, install the menu runner once:

   ```text
   uv run rabbithole resolve install-runner
   ```

   Restart Resolve, then run `Workspace > Scripts > Utility > RabbitHole Runner`.
4. Inspect the result:

   ```text
   uv run rabbithole resolve status projects/<slug>
   ```

The in-app runner accepts the `resolve` object injected by Resolve, and also
supports `app.GetResolve()` in Fusion-hosted sessions. It does not need the
Studio-only external connection. This is API access inside the running
application, not a promise that every API feature is licensed in Free:
Studio-only calls may return `False` or an edition-specific error.

The repository code requires Python 3.11 or newer, while some Resolve
installations still expose an older Python Console. Before the first run, enter:

```python
import sys; print(sys.version)
```

If the result is older than 3.11, do not run the loader. Its Python-3.6-compatible
bootstrap will refuse the job before importing RabbitHole or changing the
project. Import the prepared FCPXML manually, use the FFmpeg backend, or use
Resolve Studio's external bridge from Python 3.11+.

## DaVinci Resolve Studio

Studio can run the same queue through the external scripting bridge:

```text
uv run rabbithole resolve build projects/<slug> --mode studio
uv run rabbithole resolve status projects/<slug>
```

External mode is explicitly gated. Selecting it on a Free installation produces
an actionable error instead of silently falling back or attempting unsupported
network control. On a standard Windows or macOS installation, RabbitHole
discovers Resolve's official `Developer/Scripting/Modules` directory and
`fusionscript` library automatically. Existing `RESOLVE_SCRIPT_API`,
`RESOLVE_SCRIPT_LIB`, and `RABBITHOLE_RESOLVE_PATH` overrides remain
authoritative.

## Generated timeline

Each build has a content-derived identifier. Its generated timeline is named:

```text
AUTO_BUILD_<short-hash>
```

The build contains:

- 1920 x 1080, 30 fps timeline settings
- 48 kHz audio intent
- V1 primary footage
- V2 evidence, chapter, censor, and highlight overlays
- V3 designed titles and graphics
- V4 texture and finishing overlays
- A1 narration
- A2 original-source bites
- A3 a full-length music stem with the approved constant-power
  bed joins, level, and silence-drop ramps already baked
- A4 a full-length SFX stem with cue timing, category levels, and
  silence-drop ramps already baked
- A5 room tone and utility audio
- editable subtitle and source-caption intent
- provenance and review markers with machine-readable custom data

FCPXML carries deterministic cuts, simple framing transforms, supported
primary-story transitions, and title/subtitle timing. V1 is the primary
storyline; V2–V4 are connected lanes above it, and A1–A5 are connected lanes
below it. The Resolve scripting layer imports that timeline, names and validates
the tracks, adds metadata markers, applies a checksum-matched archival grade
when available, records manual Fusion/template intent, and configures the render
job.

Resolve 21 can ignore valid FCPXML `caption` elements. `resolve prepare`
therefore emits a checksum-pinned `subtitles.srt` beside the FCPXML. The runner
keeps a complete native caption import, appends the SRT only when the imported
subtitle count is exactly zero, and refuses a partial count to prevent
duplicates. The resulting cues remain editable on the subtitle track.
Review-warning and human-review markers use Resolve-supported Yellow; Resolve
rejects `Orange` as a marker color. The runner creates each marker first and
then attaches its machine-readable JSON with `UpdateMarkerCustomData`, avoiding
Resolve's combined marker-payload rejection. A failed metadata update rolls
back only marker changes made by that call. Build reuse, render, and handoff
require the complete per-frame RabbitHole marker contract, so a partial marker
set can never authenticate an immutable timeline.

`resolve prepare` creates A3/A4 under
`resolve/audio-stems/<content-sha256>/`. The directory is immutable: changing
timing, a generated sound, the sound manifest, or the SFX style map creates a
new stem set instead of overwriting audio that a running render may have open.
The raw project-local sound library is retained for editors who want to replace
individual cues. Resolve imports the stems by default because FCPXML cannot
faithfully express RabbitHole's constant-power tiling and per-cue automation.
The stem manifest also records the non-positive peak-ceiling gain calculated
from the summed A1/A3/A4 mix. FCPXML applies that same editable volume
adjustment to all three tracks, matching the approved FFmpeg master without
flattening the handoff. Authored source-audio bites are rejected by Resolve
stem preparation for now because their A1/A3 duck automation is not yet baked;
those projects remain supported by the FFmpeg renderer.

For direct-source media carrying an original URL, the compiler also derives an
editable lower-left V3 source caption from provenance. It uses the authored
source title (or provider/host fallback) and source/publication date; retrieval
time is never presented as a publication date. Continuous cuts from the same
source span are coalesced. An authored non-empty `source_caption` overlay
suppresses generation over its covered span, and media with attribution already
burned into a citation card, source frame, or source image receives no duplicate
caption. FCPXML represents these as editable Basic Title items.

Generated timelines are immutable. Re-running an identical build validates and
reuses its `AUTO_BUILD_<hash>` timeline. It never rewrites or deletes a timeline.
Editors should duplicate a generated timeline to:

```text
EDITORIAL_v1
EDITORIAL_v2
```

Automation never mutates a timeline whose name starts with `EDITORIAL_`.

## Primary render and fallback

After reviewing the generated timeline:

```text
uv run rabbithole resolve render projects/<slug>
```

Resolve Free queues the render and waits for the in-app runner. Studio may execute
it externally. The generated plan selects single-clip MP4/H.264, 1080p30,
48 kHz AAC, and burned-in review subtitles instead of inheriting whichever
Deliver-page format was last used. The general render command also exposes the
explicit backend:

```text
uv run rabbithole render projects/<slug>/narration/timing.json --backend resolve
uv run rabbithole render projects/<slug>/narration/timing.json --backend ffmpeg
```

FFmpeg remains the general-command default until the local Resolve Console,
timeline import, style, and handoff pilot gates pass. `--backend resolve` is the
explicit editable-timeline path; neither backend silently falls back to the
other.

## Safety model

Every mutating operation uses a project-scoped lock containing:

- process ID and process start time
- operation stage
- declared write roots
- creation and heartbeat timestamps

The runner fails closed when the lock belongs to a live process, when Resolve is
already rendering, when the current project is unsafe for the requested job, or
when a path escapes the episode or requested handoff directory. Each queued job
also pins the plan and compiler-recorded FCPXML/SRT SHA-256 values plus the
expected linked-media checksum set. Once compilation has prepared a bundle, the
durable enqueue step reads only the small plan/FCPXML/SRT artifacts; it does not
rescan large media. After Resolve reports idle, the runner authenticates the
queue document and rechecks the plan, FCPXML, SRT, and every linked video/audio
file before mutation.
Before configuring a render, it validates the selected immutable timeline's
identity marker, style marker, track contract, duration, item counts, and exact
subtitle cue timing/text again.

The runner intentionally has no code path that calls:

- `StopRendering`
- `Quit`
- `DeleteAllRenderJobs`
- `DeleteRenderJob`
- `DeleteProject`
- `DeleteTimelines`
- `SetCurrentDatabase`
- `LoadProject`

If another project is open, that current project remains authoritative; the
runner only adds a new immutable generated timeline to it. If no project is
open, it may create a uniquely named RabbitHole project. It never switches away
from a user's open project.

## Move an unfinished episode to another machine

Git transfers the application, tests, reusable Resolve assets, and the
repository-local production skill. It intentionally does not transfer
`projects/<slug>` or its media. Before Resolve assembly, create a portable
episode bundle outside the project root:

```text
uv run rabbithole resolve bundle projects/<slug> --out <episode.bundle.zip>
uv run rabbithole resolve verify-bundle <episode.bundle.zip>
uv run rabbithole resolve restore-bundle <episode.bundle.zip> --out <new-project-root>
```

The bundle preserves the project-relative tree and verifies every file with
SHA-256. It refuses absolute/escaping media paths, symlinks, missing referenced
media, case-colliding names, unsafe ZIP members, and overwrites. It excludes
`.env` files, credentials, caches, prior renders/handoffs, stale locks/queues,
and generated Resolve builds. The content-addressed A3/A4 stems are media
inputs, not queue/build state, so the set selected by `current.json` is
included and checksum-verified; historical stem directories are omitted.
Bundle creation and verification also follow `audio-stems/current.json` into
the selected immutable manifest, verify its fingerprint, both exact stem files,
and every project-local source-input checksum. Repository implementation/style
hashes remain part of the contract but are not copied into the episode ZIP;
clone the same Git revision on the receiving host, then run `prepare`.
Recoverably quarantined media remains project-relative and travels with its
retired provenance record.

On the receiving Windows or macOS host, recreate `.env` locally and run
`resolve doctor`, `resolve preflight`, and `resolve prepare` again. Recompiling
the plan/FCPXML on that machine is mandatory; do not transfer or execute the old
queue state.

## Editor handoff

Create a source-inclusive handoff:

```text
uv run rabbithole resolve handoff projects/<slug> --out <handoff-directory>
```

The handoff job asks Resolve to create:

- a `.dra` Project Archive with source media included
- render cache excluded
- proxies excluded unless explicitly requested
- a `.drp` lightweight project export with stills and LUTs

The portable ZIP also contains:

- `resolve-plan.v1.json`, FCPXML, and the deterministic subtitle SRT
- source timing, EDL, provenance, optional source-audio/highlight manifests
- LUTs, Fusion templates, fonts supplied by the project, and their licence notes
- chapter/credit/QC reports when the episode already contains them
- a privacy-preserving inventory used for disk-space estimation; source media
  itself is carried by the DRA
- SHA-256 checksums
- an editor README with import and relink instructions

A `.drp` alone is not a media handoff. The source-inclusive `.dra` is the primary
transfer artifact; the `.drp` is a small backup and inspection aid.

On the editor's machine:

1. Unzip the package without renaming its internal directories.
2. Restore the `.dra` from Resolve's Project Manager.
3. The DRA is source-inclusive. If Resolve still reports offline media, inspect
   the restored archive and relink to the media copied by Resolve during restore.
4. Install only the bundled fonts/templates whose licence notes permit it.
5. Duplicate `AUTO_BUILD_<hash>` to `EDITORIAL_v1` before changing the cut.
6. Run `uv run rabbithole resolve verify-handoff <handoff.zip>` before restore,
   or `restore-handoff <handoff.zip> --out <directory>` to extract and verify in
   one operation.

## Queue states

Jobs are durable JSON documents under the episode's `resolve/queue/` directory.
The status command reports:

```text
queued -> running -> succeeded
   |        |      \-> failed
   |        \-> rendering -> succeeded
   |                       \-> failed
   \-> superseded (older queued build only)
```

An asynchronous render stays in `rendering` until that exact Resolve render job
positively reports `Complete`; an idle Resolve session alone is not treated as
success. Free-mode jobs may additionally report `awaiting_in_app_runner`. A
succeeded job is not implicitly rerun. A failed job retains its error, attempt
count, and timestamps for diagnosis. Queue files from an older integrity schema
are counted as `legacy` but never executed; prepare and enqueue a fresh job to
replace one. Enqueuing a new build supersedes only older build jobs that are
still queued; it never changes running/rendering jobs or non-build work.

## Human review gates

Automation deliberately stops short of editorial judgment. Before final delivery,
review:

- graphics and evidence contact sheets, including every red missing/unreadable
  card
- evidence accuracy and source context
- redactions, privacy, defamation, and rights/clearance flags
- deepest-point timing and chapter rhythm
- original-source audio intelligibility
- subtitle accuracy and line breaks
- editable source-caption text, publication date, placement, duration, and
  duplicate suppression
- grade consistency across mixed sources
- the restored handoff on a disposable project

Optional per-episode adjustments belong in `resolve-overrides.json`. They augment
the existing RabbitHole marker grammar; they do not replace `timing.json` or
`edit/edl.json`.
