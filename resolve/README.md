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

Fonts are not vendored by default. The Fusion templates request IBM Plex Mono;
install a properly licensed copy for deterministic typography because Resolve's
substitution varies by machine. FCPXML baseline titles use Courier New and
captions use Arial. A production handoff includes only fonts supplied in the
episode's `fonts/` directory, along with their licence notes.
