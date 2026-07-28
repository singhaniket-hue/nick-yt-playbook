# The Crowley Pipeline

**Companion to the Dark Documentary Playbook**

Target design for automating the dark-documentary edit in DaVinci Resolve —
how the local RabbitHole CLI/MCP pipeline maps the Playbook into deterministic
editing rules.

| | |
|---|---|
| **System** | `rabbithole` CLI + `rabbithole.resolve_mcp` |
| **Engine** | DaVinci Resolve in-app API, with FFmpeg preparation/fallback |
| **Style** | Nick Crowley — analog-horror documentary |
| **Input** | Machine-readable script + asset folder |
| **Target output** | Draft rough cut, styled, ready for human pass |
| **Date** | July 2026 |

---

> **IMPLEMENTATION SCOPE**
>
> Sections 03–11 are the target-state design contract, not a claim that every
> treatment is already automated. The implementation-status table in Section 12
> is authoritative. The current Resolve path compiles and imports an editable
> conform, names its tracks, preserves source/review metadata, applies only the
> explicitly tested baseline style treatments, and packages an editor handoff.
> Fairlight processing, advanced VHS damage, tracked redactions, final credits,
> and emotional timing remain manual or planned until their status is updated.

## Contents

1. [What This Document Is](#01-what-this-document-is) — pipeline meets style
2. [The Automation Boundary](#02-the-automation-boundary) — what the machine cuts, what you cut
3. [Ingest](#03-ingest--the-machine-readable-script) — the machine-readable Crowley script format
4. [Asset Gap-Filling](#04-asset-gap-filling--generation-rules) — generation rules for this genre
5. [Timeline Assembly](#05-timeline-assembly--track-map--placement) — track map & placement rules
6. [Audio Pass](#06-audio-pass--fairlight-automation) — narration chain, ducking & scripted silence
7. [Motion & Pacing](#07-motion--pacing--the-ken-burns-rules-engine) — the Ken Burns rules engine
8. [Style Application](#08-style-application--grade-vhs-stack--the-signature-glitch) — grade, VHS stack & the signature glitch as presets
9. [Transition Rules](#09-transition-rules--genre-grammar-as-machine-rules) — the genre grammar as machine rules
10. [Text & Graphics Automation](#10-text--graphics-automation) — chapter cards and source captions
11. [The Edit-Style Config](#11-the-edit-style-config--crowley_styleyaml) — `crowley_style.yaml`
12. [Render, Review Loop & Build Roadmap](#12-render-review-loop--build-roadmap)

---

## 01 What This Document Is

The Project Overview defines a general-purpose pipeline: script + assets in, agent-assembled Resolve timeline out. This document specializes that pipeline for one genre — the Nick Crowley-style dark documentary defined in the Playbook. It answers a single question: **what exact rules, presets, and conventions does the agent need so that its automatic rough cut already looks and sounds like the channel?**

The mapping between the two documents:

| Pipeline stage (Project Overview) | Genre specialization (this doc) | Playbook ref |
|---|---|---|
| 1. Ingest | Machine-readable two-column script with chapter/beat/silence tags; asset naming contract | §04, §06 |
| 2. Asset gap-filling | Atmosphere-only generation rules; evidence integrity constraints | §06, §15 |
| 3. Timeline assembly | Fixed V1–V4 / A1–A4 track map; narration-first "radio edit" assembly | §08 |
| 4. Audio pass | −14 to −16 LUFS dialogue, sidechain ducking, scripted silence drops | §05, §07 |
| 5. Motion & pacing | Ken Burns rules engine: 100→108% pushes, direction alternation | §11 |
| 6. Color grade | The Crowley grade as a PowerGrade/LUT: desaturated, teal-shadow, vignette, grain | §08 |
| 7. Transitions | Genre grammar as deterministic rules: hard cut / static burst / glitch / dip-to-black | §10 |
| 8. Render | Draft MP4 + auto-generated chapter timestamps and credits file | §14 |

> **DESIGN PRINCIPLE**
>
> The Playbook's core rule — *"everything in the edit exists to make real information feel heavier"* — becomes an automation advantage. This genre's edit language is deliberately restrained and repetitive: one grade, five transitions, one title-card template, rule-based zooms. That is exactly the kind of edit a scripting API can assemble well. **The style's discipline is what makes it automatable.**

---

## 02 The Automation Boundary

The Project Overview is explicit: no visual judgment, no emotional-beat sense, no one-shot perfection. In this genre the line falls in a specific place, because dread is a timing art. The machine assembles everything that is **rule-driven**; you own everything that is **dread-driven**.

| Target pipeline does (deterministic) | You do (judgment) |
|---|---|
| Cut narration into the timeline with scripted pauses | Decide whether a pause needs 2 seconds or 4 |
| Place assets against script beats per the shot list | Choose WHICH screenshot is the most chilling one |
| Apply grade, grain, VHS stack, vignette everywhere | Decide where degradation should intensify |
| Insert glitch chapter breaks at chapter boundaries | Verify the glitch lands on the right frame of footage |
| Duck music under voice; drop music at `[SILENCE]` tags | Judge whether the silence lands or needs to move |
| Ken Burns every still per rules | Redirect a push-in toward the actual disturbing detail |
| Generate title cards, captions, credits from metadata | Write the words on them |
| Render drafts on request | Approve the final |

> **NON-NEGOTIABLE HUMAN GATES**
>
> Three things must never ship without human review, regardless of pipeline maturity: (1) every piece of **on-screen evidence** (correct screenshot, correct redaction, correct caption); (2) every **ethical treatment** — blurs on minors/bystanders, "alleged" language matching the claim grade; (3) the **deepest-point chapter**, which lives or dies on timing no rule can encode. The pipeline flags these for review rather than deciding.

---

## 03 Ingest — the Machine-Readable Script

The Project Overview treats the script as "a de facto cut sheet." For this genre, formalize that: the two-column script from Playbook §04 becomes a lightly tagged markdown file the agent can parse deterministically. Tags map 1:1 to pipeline actions.

### Script tag vocabulary

| Tag | Meaning | Pipeline action |
|---|---|---|
| `[CH:3 "The Second Account"]` | Chapter boundary + title | Insert glitch break + chapter card macro; start YouTube chapter timestamp |
| `[ASSET:ch3_012]` | Show this asset for this paragraph | Place `ch3_012_*.png/mp4` on V2 for the paragraph's duration |
| `[PAUSE:2]` | Scripted narration pause (seconds) | Gap in A1; hold current visual; music continues |
| `[SILENCE:3]` | Full drop-out | Music + ambience cut for N seconds (the Playbook's heaviest effect) |
| `[HEAVY]` | Marks the gravest lines | Dip-to-black after paragraph; no VHS jitter during; flag for human review |
| `[TEXT:"..."]` | Hard-cut text card | Black screen + white line + boom SFX (Playbook §12, animation #5) |
| `[ZOOM:asset@x2,y1]` | Push-in target on a still | Ken Burns push-in toward the given region instead of default center |
| `[REC]` / `[VHS]` | Treat clip as recreation / archive | Apply full VHS degradation stack + "RECREATION" caption where tagged |
| `[SRC:"Wayback, Mar 2021"]` | Source of the current evidence | Auto-generate the monospace source caption, lower-left |
| `[MUSIC:tension_02]` | Music bed change | Crossfade A2 to the named bed from the music bin |

### Asset naming contract

The Playbook's naming convention (`ch3_012_profile_screenshot.png`) is now a **hard interface**, not just hygiene: `{chapter}_{beat}_{slug}.{ext}`. The ingest stage validates that every `[ASSET:]` tag resolves to exactly one file and reports gaps — the gap list is what feeds stage 2. Narration is delivered as one cleaned WAV per chapter (`ch3_vo.wav`), which keeps re-records surgical.

> **WHY PER-CHAPTER NARRATION FILES**
>
> The review loop in the Project Overview ("preview → note fixes → re-run affected stage") works best when a fix to chapter 4's narration only re-renders chapter 4's segment. Chapters are the genre's natural unit — of story, of style (the glitch break), and now of incremental builds.

---

## 04 Asset Gap-Filling — Generation Rules

Stage 2 generates whatever the script calls for that doesn't exist. In this genre, generation has one bright line, straight from Playbook §06 and §15:

> **THE EVIDENCE RULE**
>
> Generated media may only ever be **atmosphere** or **labeled recreation** — never evidence. The pipeline enforces this structurally: generated assets are written to `/assets/generated/`, are barred from carrying `[SRC:]` captions, and anything tagged `[REC]` gets an on-screen "RECREATION" label automatically. Real evidence lives in `/assets/evidence/` and is never modified beyond crop/zoom/redaction.

### What each generator is for

| Source | Generates | Style constraints (auto-applied prompt suffix / post-process) |
|---|---|---|
| Configured local image generator | Atmospheric B-roll stills: dark hallways, rural roads, CRT screens, storm windows | Dark, desaturated, underexposed, grain-friendly; degraded by the VHS stack downstream so it blends |
| Configured local video generator | Short ambient loops (static, rain, flickering light) | 4–8s loops; low motion; no faces, no readable text |
| Higgsfield | Higher-end atmosphere shots and stylized recreations | Always tagged `[REC]`; era-appropriate degradation |
| ElevenLabs voice clone | Pickup lines when re-recording isn't possible; quote read-outs (labeled) | Never used to voice real people from the case — quotes are read in YOUR narrator voice or shown as text cards |
| Noise-suppression tool | Cleans your narration before ingest | 30–50% mix per Playbook §05 — over-cleaned voice breaks the intimate close-mic feel |

Generation requests inherit a fixed style prompt block (palette, mood, grain, era) stored in the edit-style config, so every generated asset is born matching the channel's look instead of being corrected later.

---

## 05 Timeline Assembly — Track Map & Placement

The target contract builds every project on a fixed track layout so all projects
are identical and every later stage knows where things live:

```
V4  grain / texture overlays        # one grain clip spanning the timeline, Overlay blend, 10–30%
V3  text & graphics                 # chapter cards, captions, text cards, redaction rectangles
V2  evidence & inserts              # screenshots, screen recordings, documents, maps
V1  base footage                    # archive footage, B-roll, recreations, location shots

A1  narration                       # per-chapter VO WAVs, cut with [PAUSE] gaps
A2  original-source bites           # quoted/source audio preserved separately
A3  music beds                      # per-chapter beds, ducked under A1
A4  SFX hits                        # booms, static bursts, typewriter clicks, camera shutters
A5  ambience / room tone            # utility audio; silence only when authored
```

### Target assembly order (mirrors the Playbook's radio-edit workflow)

1. **Narration skeleton.** Place chapter VO files on A1 in order, inserting `[PAUSE]` gaps. Timeline length is now fixed. Compute each paragraph's in/out timecodes from the VO segments (silence detection between paragraphs, validated against script word counts at 130–150 wpm).
2. **Chapter infrastructure.** At each `[CH:]` boundary: insert the glitch-break macro (Sec. 08), then the chapter-card compound clip on V3 (2.5–4s), then a marker that later exports as a YouTube timestamp.
3. **Visual coverage.** For each paragraph, place its `[ASSET:]` file: stills and screenshots → V2 over a blurred-dark backdrop; footage → V1. Rule check: **no uncovered narration** — any paragraph without a resolvable asset gets a slated placeholder (dark ambient loop + "MISSING: ch3_014" text) so gaps are visible in the draft, never silent.
4. **Music & ambience.** Lay `[MUSIC:]` beds on A2 with 2s crossfades at changes; room-tone loop on A3; cut both at `[SILENCE]` tags. Add riser SFX ending at each chapter's final beat.
5. **SFX pass.** Boom at every chapter card and `[TEXT:]` card; static burst at every static-cut transition; shutter/click when a V2 evidence item appears.

---

## 06 Audio Pass — Fairlight Automation

This section is a target-state Fairlight specification. The current reusable
pipeline can prepare/mix deterministic stems with FFmpeg; it does not yet build
this complete Fairlight chain through Resolve's scripting API.

- **Narration chain** (per Playbook §05), applied as a saved Fairlight preset: high-pass 80 Hz → mud cut ~300–400 Hz → presence lift 2–5 kHz → compression ~3:1 targeting 4–6 dB reduction → normalize dialogue to **−15 LUFS ±1**.
- **Ducking:** sidechain compressor on A2 keyed from A1, ~−15 dB under speech, slow release so beds swell gently in pauses — mimicking the manual keyframing the Playbook describes.
- **Scripted silence:** `[SILENCE:n]` renders as a **hard** music/ambience cut (not a fade) — the Playbook's drop-out effect — with a 1s ambience fade-back after.
- **Master:** limiter at −1 dBTP; loudness report logged per render so drafts are comparable.

**QC checks the agent runs automatically:** any narration peak above −6 dB, any gap in A3 room tone outside a `[SILENCE]` region, any music bed ending early — reported in the draft notes.

---

## 07 Motion & Pacing — the Ken Burns Rules Engine

The Project Overview calls for "templated dynamic zoom/pan, rule-based, not creative judgment." The Playbook §11 supplies the exact rulebook; encoded, it looks like this:

| Clip type | Default motion | Rules |
|---|---|---|
| **Still image (V1)** | Push-in 100→108% over clip duration, ease both ends | Alternate direction (in/out) between consecutive stills; alternate drift axis |
| **Screenshot / document (V2)** | Scale-in 100→105%; if taller than frame, slow vertical scan | If `[ZOOM:@x,y]` given, push toward that region instead of center |
| **Long thread / chat capture** | Vertical pan paced to narration timecodes | Flashlight highlight (dim 40% + windowed bright region) follows the line being read when region tags exist |
| **Footage (V1)** | None (real motion suffices) | Optional 2–4% slow punch-in on clips longer than 12s |
| **Maps** | Zoom from wide to pin over 4–6s | Google Earth Studio renders imported as-is; pin-pulse macro on V3 |
| **`[HEAVY]` paragraphs** | Motion FROZEN | No zoom, no jitter — stillness is the genre's emphasis; flagged for human review |

Every motion value lives in the config (Sec. 11), so "zoom aggressiveness" is one number you tune once, not per-clip work.

---

## 08 Style Application — Grade, VHS Stack & the Signature Glitch

### The Crowley grade (stage 6 of the pipeline)

The target look is built once on the Color page per Playbook §08 — saturation
~35–45, crushed-but-readable shadows pushed teal, lowered highlights, soft
vignette −15 to −25% — then exported as a PowerGrade or `.cube` LUT. The current
runner checksum-locks the asset and applies it only to eligible archival V1
clips on a newly imported build; evidence V2 and pre-styled graphics are left
untouched. Grain, vignette, and VHS damage are not encoded in the LUT and remain
editable V4/Fusion review work.

### The VHS degradation stack as a preset

The target Playbook §09 stack (soften → chromatic aberration → scanlines →
noise → jitter → OSD timecode → edge damage) is built once as a Fusion macro /
saved preset chain. The repository installs its portable template assets, but
the documented scripting API cannot deterministically place and trim them at
an arbitrary edit range. The runner records this as manual style intent instead
of pretending the treatment was applied.

### The signature glitch chapter break

The target channel watermark (Playbook §09) is a reusable timeline chunk for an
editor to place at every `[CH:]` boundary:

1. Preceding footage plays clean for its final 3–6 seconds (assembler guarantees this window exists).
2. 8–15 frames of stacked damage: static burst overlay + displacement slices + 2-frame stutter repeats + hue rotation into magenta/pink-red with saturation spike (pre-built 10-frame adjustment clip).
3. Audio: static/tape-screech burst on A4, then everything cut to silence.
4. Hard cut to the chapter card (Sec. 10) with a low boom.

Because it's one macro, the break is **frame-identical across the whole channel** — which is exactly what makes a signature a signature.

---

## 09 Transition Rules — Genre Grammar as Machine Rules

The Playbook's transition table (§10) translates directly into deterministic insertion rules — no judgment calls needed, which is why automation works here:

| Boundary detected | Transition inserted |
|---|---|
| Between paragraphs within a topic | Hard cut (default — 80% of edits) |
| Between evidence items (V2 → V2) | Static burst cut: 2–5 frames white/pink static + crackle on A4 |
| Chapter boundary `[CH:]` | Full signature glitch break (never used elsewhere — scarcity keeps it special) |
| After a `[HEAVY]` paragraph | Dip to black, hold 1–2s, fade in |
| Photo montage (consecutive stills, same beat) | 12–24 frame cross-dissolves |
| Every scene change | J-cut: incoming audio leads the visual by 1–2s |
| Script says "let's go back to…" `[REWIND:asset]` | Tape-rewind macro: speed-ramped reverse + VCR whir + tracking lines |

---

## 10 Text & Graphics Automation

These are target behaviors. The baseline repository includes portable Fusion
templates and deterministic text intent, but tracked redactions, per-character
SFX, and final credit/description generation are not yet complete.

- **Chapter cards:** one Fusion template (Playbook §12): red monospace "CHAPTER 03" typewriter-animated at top, white title glitch-in below, grain + vignette pulse, boom. The assembler instantiates it per `[CH:]` tag — text is data, design is fixed.
- **Typewriter engine:** Text+ with Write-On/Follower animation as a macro; per-character click SFX auto-laid on A4 at low volume.
- **Source captions:** every `[SRC:]` tag renders the small monospace lower-left caption with fade in/out — automated sourcing on screen, the genre's credibility marker, now impossible to forget.
- **Hard-cut text cards:** `[TEXT:"..."]` → black screen, one line of white type, no animation, one boom. The pipeline's simplest template is the genre's most powerful moment.
- **Redactions:** `[REDACT:asset@region]` draws solid black rectangles on V3 over the region; for video, a tracked blur is applied and **always flagged for human verification** (a slipped redaction is an ethics failure, not a style bug).
- **Credits & description:** at render time the pipeline concatenates every `[MUSIC:]` and `[SRC:]` reference plus generated-asset disclosures into `credits.txt`, and exports chapter markers as YouTube timestamps — the Playbook §14 upload checklist, half-done automatically.

---

## 11 The Edit-Style Config — `crowley_style.yaml`

The Project Overview's "edit-style config format (JSON/YAML)" milestone, filled in for this genre. This file **IS** the channel's style, versioned in the project repo; the setup conversation happens once, then every video inherits it:

```yaml
# crowley_style.yaml — channel style contract, v1
narration:
  wpm_expected: 140          # validation vs script length
  lufs_target: -15
  music_duck_db: -15
grade:
  powergrade: "grades/crowley_v1.cube"
  grain_overlay: "textures/grain_4k.mp4"
  grain_opacity: 0.18
  vignette_amount: 0.20
vhs:
  intensity: 0.6             # drives all 7 degradation layers
  apply_to: [tagged, generated]
  osd_font: "VCR OSD Mono"
motion:
  still_push_pct: 8          # 100 -> 108%
  screenshot_push_pct: 5
  alternate_direction: true
  heavy_freeze: true
transitions:
  default: hard_cut
  evidence: static_burst
  chapter: glitch_break_v1   # macro reference
  post_heavy: dip_black_1500ms
  jcut_lead_ms: 1500
text:
  mono_font: "Courier Prime"
  sans_font: "Inter"
  accent_hex: "#ff2a5f"
  chapter_card: "macros/chapter_card_v1"
  card_hold_s: 3.0
music:
  bins: {tension: "music/tension/", drone: "music/drones/", quiet: "music/quiet/"}
  crossfade_s: 2.0
safety:
  require_review: [redactions, evidence_captions, heavy_sections]
  generated_assets_dir: "assets/generated/"
  forbid_src_on_generated: true
```

> **VERSIONING THE LOOK**
>
> When the channel's look evolves (grade v2, a new glitch), bump the macro/LUT references here — old projects keep rendering identically, new ones pick up the change. The config file is the single source of truth the Playbook's "save everything as presets" advice was building toward.

---

## 12 Render, Review Loop & Build Roadmap

### Draft render output

A complete target-state run produces: `draft_v{n}.mp4` (1080p H.264 review
quality), `chapters.txt`, `credits.txt`, `qc_report.txt`, and a Resolve project
for the human pass. The current handoff includes those text/QC artifacts only
when the episode already generated them.

### The review loop, genre edition

1. Watch the draft once at 1× for story; note fixes against script line IDs ("ch3_014: wrong screenshot; ch5: silence 2s later").
2. Agent re-runs only the affected chapters/stages; re-render.
3. Human pass in Resolve for the judgment work: deepest-point timing, evidence verification, redaction check, the final ±5% on silences.
4. Final render at full quality; description assembled from chapters + credits + content warning template.

### Implementation status

| # | Milestone | Status |
|---|---|---|
| 1 | Local media preparation, capture, source-audio, plate, music, and SFX tooling | Built |
| 2 | Versioned Resolve plan and FCPXML compiler with CLI and local MCP entry points | Built |
| 3 | Resolve Free in-app runner, explicit Studio external bridge, durable queue, and fail-closed safety locks | Built and fake-API tested; Free Console still requires Python 3.11+ confirmation |
| 4 | Portable style pack: `crowley_style.yaml`, deterministic LUT, Fusion titles, and glitch transition | Built; the baseline LUT is applied to eligible archival V1 clips, while Fusion/V4 placement and optional PowerGrade/DRX capture remain manual |
| 5 | Editor handoff with source-inclusive DRA, DRP, presets, fonts/licenses, manifest, and checksums | Built; fake-API tested |
| 6 | Synthetic chapter compile and import fixture | Compiler and fake import pass; live Resolve pilot is pending after the local first-run Welcome screen |
| 7 | Full real-episode run and human review-loop dry run | Pending |
| 8 | Thumbnail drafts and publishing tools | Later |

> **A NOTE ON RESOLVE FREE**
>
> Resolve Free supports the required API calls only from a script running inside
> Resolve, such as the Python Console or an installed Workspace script. It does
> not expose the external scripting bridge used by a terminal process. The CLI
> and MCP server therefore compile and queue the job; the editor pastes the
> printed loader into Resolve's Console, or runs the installed Workspace runner.
> Resolve Studio can run the same queue externally when that mode is selected
> explicitly. The checked-in Python runner requires a Console running Python
> 3.11 or newer; its bootstrap refuses older Consoles before claiming a job.
> Studio-only effects still need their documented free alternatives.

---

*Companion document to "The Dark Documentary Playbook" (July 2026). Pipeline architecture from the "Local AI-Assisted Video Production Pipeline — Project Overview." Section references (§) point to the Playbook.*
