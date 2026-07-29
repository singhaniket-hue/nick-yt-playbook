# Documentation

Reference and operating documents for the dark-documentary YouTube production
project. The editorial research was extracted from the source PDFs in July
2026; implementation guides are maintained with the reusable pipeline.

| Document | What it covers |
|---|---|
| [The Dark Documentary Playbook](./dark-documentary-playbook.md) | The complete manual: style analysis, topic selection, research workflow, scripting, narration, assets, music/sound design, DaVinci Resolve editing, VHS/analog-horror VFX, transitions, motion graphics, text & chapter cards, thumbnails, publishing, ethics, and the full free-tool toolkit. |
| [The Crowley Pipeline — Automation Companion](./crowley-pipeline-automation-companion.md) | How the local AI pipeline (Claude Code / Codex CLI → MCP → Resolve scripting API) automates that style: the automation boundary, script tag vocabulary, asset generation rules, timeline assembly, audio/motion/style automation, `crowley_style.yaml`, and the build roadmap. |
| [Asset Acquisition and Visual QA](./asset-acquisition-workflow.md) | The implemented resumable asset workflow: exact slots and batches, recoverable refresh/quarantine, target-aware browser capture, retained source frames, portable yt-dlp downloads, contact sheets, editable source captions, and signal-comparison cards. |
| [DaVinci Resolve Automation Workflow](./resolve-workflow.md) | The implemented Free/Studio workflow, immutable timeline model, render-safety rules, editor handoff format, CLI commands, and recovery procedure. |

## How they fit together

The **Playbook** defines the style — what a video in this genre must look and sound like, and why. The **Pipeline** companion encodes that style as machine rules: every section reference (§) in the companion points back at the Playbook section it automates.

Read the Playbook first if you are making a video by hand. Read the companion if you are building or running the automated rough-cut pipeline.
Use the asset guide while binding, acquiring, replacing, and approving episode
media; use the Resolve workflow after the assets and EDL have passed review.

> **Note on attribution:** these documents are an independent educational analysis of a publicly visible style. They are not affiliated with or endorsed by Nick Crowley.
