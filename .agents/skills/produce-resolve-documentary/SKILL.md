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
4. Inspect the existing episode, Resolve status, and Git status before resuming.
   Do not repeat paid narration, downloads, or completed queue jobs.
5. Select `animatic` or `final`, and Resolve `free` or `studio`. Treat an
   unspecified first technical pilot as `animatic`; never silently downgrade a
   requested final episode.

## Prepare a host

Use the same `uv run` commands on Windows, Intel Mac, and Apple silicon Mac.

```text
Windows: powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
macOS:   bash scripts/bootstrap.sh
Check:   uv run rabbithole resolve doctor --mode free
```

Require Python 3.11+, Resolve 21+, FFmpeg, and ffprobe. Require Poppler
(`pdftoppm`) for PDF evidence and Chrome, Edge, or Chromium for browser
captures. On macOS, recommend Homebrew only as an installation option; do not
install system packages without authorization.

Keep `.env` local and ignored. Never print, transfer, or commit credentials.
Require `ELEVENLABS_API_KEY` and `RABBITHOLE_VOICE_ID` only for paid narration
or sound generation. Use `RABBITHOLE_PROJECTS_DIR` or `new --projects-dir` when
episode media belongs on a separate volume.

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
`script/05-devanagari.md` as the matching TTS edition. Use only supported
markers: `ACT`, `CHAPTER`, `SHOT`, `SILENCE`, `SFX`, `MUSIC`, `REHOOK`,
`CENSOR`, and `KEY`.

```text
uv run rabbithole validate projects/<slug>/script/04-final.md
uv run rabbithole narrate projects/<slug>/script/04-final.md --out projects/<slug>/narration/vo.wav --dry-run
```

Do not use `--force` for a final episode. Show billable characters/cost and
obtain approval before the paid narration call. After approval, repeat
`narrate` without `--dry-run`. Preserve `vo.wav` and word-aligned
`timing.json`.

## Source assets and build the edit

Start with dry runs and preserve provenance:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --dry-run --quality <animatic|final>
uv run rabbithole edl projects/<slug>/narration/timing.json --dry-run --quality <animatic|final>
```

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
tests prove a Free Console runtime.

Review evidence, redactions, subtitles, source audio, rights, and grade on the
generated timeline. Then queue the deterministic render and poll status until
Resolve reports completion:

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

After Resolve assembly, create the editor handoff:

```text
uv run rabbithole resolve handoff projects/<slug> --mode <free|studio> --out <specific-handoff-directory>
uv run rabbithole resolve verify-handoff <handoff.zip>
uv run rabbithole resolve restore-handoff <handoff.zip> --out <restore-directory>
```

Treat the source-inclusive DRA as primary. Treat the DRP as a lightweight
backup, not a media archive. Restore the DRA manually in Resolve's Project
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
