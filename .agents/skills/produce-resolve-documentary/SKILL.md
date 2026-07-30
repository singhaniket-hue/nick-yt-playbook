---
name: produce-resolve-documentary
description: Produce, resume, validate, render, or transfer a RabbitHole dark-documentary episode in this repository from a topic, research pack, or marked Hinglish script through narration, asset sourcing, EDL generation, DaVinci Resolve Free or Studio, and a portable episode bundle or DRA/DRP handoff. Use for end-to-end YouTube pilots, Crowley-style documentary production, Windows/macOS rendering setup, Resolve queue or status work, and moving an episode between machines. Do not use for arbitrary video edits outside RabbitHole.
---

# Produce a Resolve documentary

Treat the repository artifacts as the production source of truth and DaVinci
Resolve as the editable finishing environment. Preserve factual, licensing,
privacy, cost, and render-safety gates even when asked to run end to end.

## Establish state

1. Resolve the Git root and work from it.
2. Read `README.md`, `docs/resolve-workflow.md`, and
   `pipeline/crowley-hinglish.yaml` before acting.
3. Read only the relevant research, scripting, ethics, thumbnail, or publishing
   sections of `docs/dark-documentary-playbook.md`.
4. Before binding, acquiring, replacing, or approving visual assets, read
   `docs/asset-acquisition-workflow.md`.
5. Inspect the existing episode, Resolve status, and Git status before resuming.
   Do not repeat paid narration, downloads, or completed queue jobs.
6. Select `animatic` or `final`, and Resolve `free` or `studio`. Treat an
   unspecified first technical pilot as `animatic`; never silently downgrade a
   requested final episode.

## Prepare a host

Use the same `uv run` commands on Windows, Intel Mac, and Apple silicon Mac.

```text
Windows: powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
macOS:   bash scripts/bootstrap.sh
Check:   uv run rabbithole resolve doctor --mode free
```

Require Python 3.11+, Resolve 21+, FFmpeg, and ffprobe. Subtitle and graphic
card generation also requires FFmpeg's `ass` filter (libass); `resolve doctor`
checks it before episode work. Homebrew's regular macOS FFmpeg formula omits
libass, so use `ffmpeg-full` and prepend `$(brew --prefix
ffmpeg-full)/bin` when Homebrew is authorized. Require Poppler (`pdftoppm`) for
PDF evidence and Chrome, Edge, or Chromium for browser captures. Recommend
Homebrew only as an installation option; do not install system packages
without authorization.

Keep `.env` local and ignored. Never print, transfer, or commit credentials.
Require `ELEVENLABS_API_KEY` for paid narration or sound generation. Require
`RABBITHOLE_VOICE_ID` only for narration; sound generation must not depend on a
voice ID. The current narration dry run still loads narration configuration.
Use `RABBITHOLE_PROJECTS_DIR` or `new --projects-dir` when episode media belongs
on a separate volume.

Use project-relative POSIX media paths in JSON. Copy authorized external media
into `projects/<slug>/assets/` or another episode subdirectory without changing
the original. Never write `C:\...`, `/Users/...`, secrets, or editor-specific
mount points into portable metadata.

## Start from a topic

Collect or safely infer:

- topic and core question;
- audience, language/register, and target duration;
- must-cover and must-avoid constraints;
- animatic versus final quality;
- public-domain/CC/provided-media policy;
- supplied narration versus an approved ElevenLabs voice.

Default to formal `aap`-register Hinglish and 34 minutes for a final episode.
Use a short fictional or public-domain animatic for the first engineering
pilot. Make clear that a short pilot does not satisfy the production
4,500–7,400-word duration gate.

Apply the playbook's topic test. Create the workspace:

```text
uv run rabbithole new <slug>
```

Complete `brief.json`. Research at least three independent sources and prefer
primary evidence. Populate `research/artifacts.json`. Record every real-person
wrongdoing assertion in `claims.json` with `claim_id`, `text`, `confidence`,
and `sources`; use only `documented`, `reported`, `alleged`, or `speculation`,
and include `attributed_to` for allegations.

Do not contact subjects, infiltrate communities, collect non-public personal
information, defeat access controls, or present speculation as fact. Pause for
brief/research approval before writing the final script.

## Author and narrate

Write `script/04-final.md` as the canonical Romanized Hinglish edition and
`script/05-devanagari.md` as the matching, mixed-script TTS edition. In the TTS
edition, write Hindi words in Devanagari and preserve every English word,
brand, acronym, and technical term in Latin. Never transliterate an English
word into Devanagari. Good: `रात हो चुकी है, घर में finally silence है।` Bad:
`रात हो चुकी है, घर में फाइनली साइलेंस है।` Write `account`, not `अकाउंट`.

Create and review the episode's authoritative Latin-token lexicon at
`script/latin-terms.json`, using `{"terms": ["account", "finally", "silence"],
"occurrences": []}`. Put unambiguous English tokens in `terms`. If one spelling
can be either Romanized Hindi or English, omit it from `terms` and declare only
the English use with its one-based spoken-word position, for example
`{"token": "is", "word_index": 705}` in `occurrences`. Validation and narration
must refuse a TTS edition that converts a declared English term or contains an
undeclared Latin word. Correct the script or lexicon; never bypass this
pronunciation gate. Use only supported markers: `ACT`, `CHAPTER`, `SHOT`,
`SILENCE`, `SFX`, `MUSIC`, `REHOOK`, `CENSOR`, and `KEY`.

```text
uv run rabbithole validate projects/<slug>/script/04-final.md
uv run rabbithole narrate projects/<slug>/script/04-final.md --out projects/<slug>/narration/vo.wav --dry-run
```

Do not use `--force` for a final episode. Show billable characters/cost and
obtain approval before the paid narration call. After approval, repeat
`narrate` without `--dry-run`. Preserve `vo.wav` and word-aligned
`timing.json`.

## Source assets and build the edit

Follow `docs/asset-acquisition-workflow.md`. Start with a dry run, then use
bounded batches or exact slots so a long run can be resumed without repeating
completed downloads:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --dry-run --quality <animatic|final>
uv run rabbithole assets projects/<slug>/narration/timing.json --batch-size 20 --quality <animatic|final>
uv run rabbithole assets projects/<slug>/narration/timing.json --slot s041 --slot s042 --quality <animatic|final>
uv run rabbithole edl projects/<slug>/narration/timing.json --dry-run --quality <animatic|final>
```

Each accepted slot is checkpointed atomically. Rerun the same batch to advance
through remaining actionable slots, or the same exact-slot command to fill only
its gaps.

Replace a current asset only through an explicit, dry-run-first refresh:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --refresh --slot s041 --dry-run --quality <animatic|final>
uv run rabbithole assets projects/<slug>/narration/timing.json --refresh --slot s041 --quality <animatic|final>
```

Select every slot claimed by an affected multi-slot asset. Never combine
`--refresh` with `--tier` or `--batch-size`, delete old media, or remove ledger
records manually. Refresh retains old media in project-local quarantine,
records retirement in provenance, rolls back incomplete retirement, and resumes
only the gaps when the exact incomplete slot set is rerun.

For browser evidence, author a `capture_spec` target or crop rather than hoping
the first viewport contains the claim. Same-URL targets may share one page load
but must keep distinct pixels and records; reuse pixels only when the identical
request has the same explicit `capture_spec_fingerprint`. Treat detected consent
walls, challenges, interstitials, and blank frames as acquisition failures.
Never dismiss consent automatically or bypass blank QA. Use an unobstructed
archive, an exact frame from already retained source video, or an explicitly
disclosed citation-card fallback.

Use the uv-managed yt-dlp path. Primary downloads must retain their exact claims
URL gate and pass the portable H.264 MP4/ffprobe checks before provenance accepts
them.

Signal-comparison cards are explanatory graphics, not evidence. Use only the
documented `edge baseline`, `processed change`, `timing and audio`, or
`automated flag` forms. They must visibly retain:

```text
LOCAL ILLUSTRATION · GENERAL TESTING LOGIC
NOT WEBDRIVER TORSO'S PUBLISHED ALGORITHM
```

After each meaningful acquisition pass, generate the read-only visual QA:

```text
uv run python -m rabbithole.contactsheet projects/<slug>/narration/timing.json
```

Review both graphics and evidence sheets in timeline order. Exit code `1` means
the sheets were written with red missing/unreadable cards; resolve every
intended gap before approval. Check source/date labels, target context, derived
frame timestamp/crop, citation-card disclosures, and both signal-card caveats.

For a no-network technical pass, use `--tier atmospheric --quality animatic`.
A final run requires source-bound evidence, rights/licence notes, complete
provenance, and human review. Approve actual assets before writing the EDL;
approve the EDL before Resolve.

## Build and render safely in Resolve

Run status and preflight before every mutating stage:

```text
uv run rabbithole resolve status projects/<slug>
uv run rabbithole resolve preflight projects/<slug> --mode <free|studio>
uv run rabbithole resolve prepare projects/<slug>
uv run rabbithole resolve build projects/<slug> --mode <free|studio>
```

`prepare` must create or reuse the content-addressed A3/A4 mix stems under
`resolve/audio-stems/`. Those stems freeze the approved bed crossfades, music
gain, category-specific SFX levels, cue placement, and silence-drop ramps that
FCPXML cannot reproduce from raw tiles. Keep the raw sound library in the
episode for editorial replacement, but do not substitute raw unity-gain clips
for the approved stems. Preserve the manifest-recorded shared peak-ceiling gain
on A1/A3/A4 as editable FCPXML volume adjustments. A changed narration, timing
file, sound file, manifest, implementation, or style map must produce a new
immutable stem directory; never overwrite one referenced by an existing queue
job or render. Resolve stem preparation must fail closed when
`research/source-audio.json` contains bites until their A1/A3 duck automation
can be baked faithfully; use the FFmpeg renderer for that case.

`prepare` must also emit a checksum-pinned `subtitles.srt` beside the FCPXML.
Resolve 21 may ignore otherwise-valid FCPXML `caption` elements. The in-app
runner must keep a complete native caption import, append the SRT only when the
imported subtitle count is exactly zero, and fail closed on a partial count so
it never duplicates captions. Validate exact cue timing and normalized text
after import, on immutable timeline reuse, and before render. Use only
Resolve-supported marker colors; warning and human-review markers are Yellow
because Resolve rejects `Orange`.
Create a marker with empty custom data first, then attach its JSON through
`UpdateMarkerCustomData`; Resolve can reject the equivalent combined
`AddMarker` call. Defensively handle a transient `False` response only with
short fixed retries; accept an asynchronously appearing marker only when its
visible shell matches exactly, and otherwise fail closed. Roll back only marker
changes made by a failed call, and validate the complete per-frame RabbitHole
marker contract before reuse, render, or handoff.

Interval-allocate simultaneous editable titles across V3/V4 so Resolve does
not discard a same-lane source caption or chapter card. Count primary-story
transition items separately from linked clips and generated titles during
timeline validation.

Resolve may compact empty FCPXML audio lanes. Accept only an exact compacted
audio-count pattern, insert missing logical lanes at their intended positions,
then name and validate A1-A5. Fail closed on any other imported audio layout.

Never proceed while the user reports an active render. Never stop a render,
clear a render queue, quit Resolve, switch databases/projects, delete a
timeline, or mutate `EDITORIAL_*`. Build or reuse only the immutable
`AUTO_BUILD_<hash>` timeline.

For Resolve Free, have the user run this once in `Workspace > Console`:

```python
import sys; print(sys.version)
```

Execute the printed loader only when the Console is Python 3.11+. If it is
older, stop before claiming the job and use manual FCPXML import, the explicit
FFmpeg backend, or Resolve Studio's external bridge. Never claim that unit
tests prove a Free Console runtime. Resolve's Console persists Python modules
for the full application session, so every loader run must discard cached
RabbitHole package modules, import from the queue-selected checkout, and verify
the imported module path before claiming a job.

Review evidence, redactions, subtitles, editable provenance-derived source
captions, source audio, rights, and grade on the generated timeline. Generated
source captions must use the authored source/publication date, never the
retrieval timestamp; attribution already burned into a source frame, source
image, or citation card must not appear twice. Then queue the deterministic
render and poll status until Resolve reports completion:

```text
uv run rabbithole resolve render projects/<slug> --mode <free|studio> --out projects/<slug>/renders/final.mp4
uv run rabbithole resolve status projects/<slug>
```

Do not treat a successfully queued asynchronous job as a completed video.

## Move between machines

Use Git to transfer reusable code and this skill. Because `projects/` is
ignored, package an in-progress episode separately:

```text
uv run rabbithole resolve bundle projects/<slug> --out <episode-bundle.zip>
uv run rabbithole resolve verify-bundle <episode-bundle.zip>
uv run rabbithole resolve restore-bundle <episode-bundle.zip> --out <new-project-root>
```

After restoring on a Mac, run `doctor`, `preflight`, and `prepare` again so all
Resolve plans and FCPXML contain that machine's paths. Do not transfer `.env`,
stale locks, queue state, generated Resolve builds, or prior renders.
Treat a bundle as portable only after verification follows the selected
audio-stem pointer through its immutable manifest, exact A3/A4 checksums, and
all project-local stem inputs. Repository style/implementation files travel in
Git, not inside the episode ZIP. Bundle only `current.json` and its selected
fingerprint directory; exclude historical stem sets and incomplete `.build-*`
directories.

After Resolve assembly, create the editor handoff:

```text
uv run rabbithole resolve handoff projects/<slug> --mode <free|studio> --out <specific-handoff-directory>
uv run rabbithole resolve verify-handoff <handoff.zip>
uv run rabbithole resolve restore-handoff <handoff.zip> --out <restore-directory>
```

Treat the source-inclusive DRA as primary. Treat the DRP as a lightweight
backup, not a media archive. The verified package must include the plan,
FCPXML, and subtitle SRT sidecar. Restore the DRA manually in Resolve's Project
Manager and duplicate `AUTO_BUILD_<hash>` to `EDITORIAL_v1` before editing.

## Finish and publish

Verify the final MP4 with ffprobe and watch it at full speed. Review factual
accuracy, context, privacy/defamation, licences/credits, redactions, subtitles,
audio, grade, and the restored handoff.

Do not call the package upload-ready until title, thumbnail, description,
chapters, credits, content warnings, and end-screen instructions are supplied
and approved. YouTube metadata generation and upload remain manual in the
current repository.

Before any commit, confirm that `.env`, `projects/`, media, renders, handoffs,
DRA/DRP files, local queue state, and credentials are absent. Commit only
reviewed reusable code, documentation, tests, and this skill.
