# Resolve style assets

This directory is the portable, versioned part of the Resolve integration.

```text
crowley_style.yaml
Fusion/
  Templates/Edit/Titles/
  Templates/Edit/Transitions/
grades/
```

The `.setting` files use only standard Fusion nodes available in Resolve Free.
`rabbithole resolve install-runner` installs or updates them in the current
user's Resolve support directory alongside the in-app runner. Existing files
are backed up when their contents differ.

The canonical color transform is
[`../style/luts/crowley-noir.cube`](../style/luts/crowley-noir.cube). For each
new build, the compiler records the LUT checksum and the exact eligible clip
IDs. The in-app runner grades only archival clips placed on V1: it prefers a
checksum-matched `grades/crowley_v1.drx` when that optional file existed at
compile time, then falls back to the checksum-matched canonical LUT. Evidence
captures on V2 and pre-styled plates/graphics are deliberately not graded.
Existing `AUTO_BUILD_*` timelines are validated and reused without restyling.

A LUT cannot encode a spatial vignette, grain, or VHS damage. Those remain
editable V4/Fusion intent and are recorded in the timeline's machine-readable
build marker. The public Resolve scripting API can insert a Fusion title at the
current UI position, but it does not expose enough placement/trim control to
apply these templates deterministically. The runner therefore does not insert
the title or transition templates automatically; editors can apply them from
the installed template browser. A build result reports `manual_required` if a
versioned grade asset or clip node graph is unavailable.

Provenance-derived source captions take the deterministic FCPXML path instead:
they arrive on V3 as editable lower-left Basic Title items. The
`RabbitholeSource` Fusion title remains an optional editor-applied treatment,
not a prerequisite for receiving or editing source attribution.

Spoken subtitles also remain editable, but the presentation timeline is
selective. `resolve prepare` writes two checksum-pinned artifacts:
`subtitles.srt` is the complete upload/accessibility sidecar, while
`presentation-subtitles.srt` excludes the source-led cold open and cues that
would compete with a text-led card, quote, document, article, or real-browser
recording. Because Resolve 21 can ignore valid FCPXML `caption` elements, the
runner appends only the selective presentation SRT when FCPXML produced exactly
zero subtitle items and fails closed on a partial import. The editor handoff
carries both SRTs beside the plan and FCPXML.

Presentation captions use the `PRESENTATION_SUBTITLES` track contract. Keep a
dark, sufficiently opaque backing or outline enabled so white text stays
readable on light source pixels; do not restore cues over text-heavy evidence
just to make the timeline match the full upload sidecar.

Live article and webpage evidence should be captured from real Chromium pixels.
Only the first browser slot for one exact source URL may establish the page and
make one restrained scroll or push to its authored target; every later use of
that URL is a static highlighted reading hold, even after intervening shots.
A simulated pan remains appropriate for a genuine static document or image,
not as a substitute for live browser context.

Generated sound follows the same deterministic handoff rule. Before compiling
FCPXML, `resolve prepare` freezes the approved mix decisions into immutable,
content-addressed A3 music and A4 SFX WAV stems inside the episode. Resolve
imports them with the same shared master attenuation as A1 narration, preserving
the approved peak ceiling while leaving each track editable. The raw project
sound library remains available for an editor who wants to redesign individual
cues. Projects with authored source-audio bites must use the FFmpeg renderer
until their narration/music duck automation is supported by Resolve stems.

Fonts are not vendored by default. The Fusion templates request Courier New,
which is available on supported Windows and macOS hosts; IBM Plex Mono remains
an optional stylistic alternative.
Install a properly licensed copy of any optional alternative on every editing
host before use because Resolve's substitution varies by machine. FCPXML
baseline titles use Courier New and
captions use Arial. A production handoff includes only fonts supplied in the
episode's `fonts/` directory, along with their licence notes.
