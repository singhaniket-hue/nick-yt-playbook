# Asset acquisition and visual QA

RabbitHole sources visual media from the approved timing spine and records each
accepted asset in `provenance.json`. This stage does not open Resolve. It is
designed to be resumed safely, reviewed visually, and transferred between
Windows and macOS with project-relative media paths.

## Normal sequence

Start with a final-quality dry run:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --dry-run --quality final
```

Acquire a bounded batch, or an exact set of slots:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --batch-size 20 --quality final
uv run rabbithole assets projects/<slug>/narration/timing.json --slot s041 --slot s042 --quality final
```

Each successful slot is checkpointed atomically before acquisition continues.
If a process stops or one source fails, rerun the same command. A bounded rerun
skips satisfied entries and advances to the next actionable batch; an exact-slot
rerun fills only the remaining gaps. There is no offset to maintain and no
`--force` flag to use.

One source failure is reported for that slot and does not discard other
successful work. `--dry-run` performs no acquisition and changes neither media
nor provenance.

## Recoverably replacing an asset

Refresh only an explicit, fully reviewed slot set:

```text
uv run rabbithole assets projects/<slug>/narration/timing.json --refresh --slot s041 --dry-run --quality final
uv run rabbithole assets projects/<slug>/narration/timing.json --refresh --slot s041 --quality final
```

`--refresh`:

- requires at least one `--slot`;
- cannot be combined with `--tier` or `--batch-size`;
- requires every slot claimed by an affected multi-slot asset to be selected;
- preflights the complete replacement plan before changing anything;
- moves current media into
  `revisions/quarantine/assets/<refresh-id>/` instead of deleting it;
- converts the old provenance entry into an unclaimed retired audit record;
- rolls completed moves back if retirement or the atomic ledger write fails;
- resumes an incomplete refresh when the exact same slot set is run again,
  retaining successful replacements and regenerating only its gaps.

Do not delete current media or hand-edit records out of `provenance.json` to
replace an asset. Quarantined paths remain project-relative and are validated
with the rest of the episode by the portable bundle workflow.

## Authoring source captures

Bind one catalogue object in `research/artifacts.json` to each source-bound
slot. Existing source metadata remains useful:

```json
{
  "artifact_id": "source-google-acknowledgement",
  "url": "https://web.archive.org/web/20240601000000/https://example.com/article",
  "title": "Google acknowledgement",
  "date": "2014-03-13",
  "source_role": "primary evidence",
  "rights_note": "Commentary use; retain only the discussed passage",
  "slot_id": "s041",
  "acquisition_mode": "screenshot-only",
  "capture_spec": {
    "text": "used by Google to test the quality of YouTube videos"
  }
}
```

`capture_spec` accepts:

- `selector` or `text` to identify the evidence element;
- `scroll_target` with exactly one of `y`, `selector`, or `text`;
- `full_page: true`;
- `crop` as `{x, y, width, height}` in the retained screenshot;
- `highlight: true` to mark the exact matched text range in yellow.

For article and webpage evidence, retain a Chromium reading sequence rather
than a simulated pan across one screenshot. Establish the real page and browser
context first, make one restrained scroll or push only on the first browser slot
for that source URL in the episode, then hold every later target from that URL
without replaying the move, even after intervening graphics or sources.
Highlight the exact narrated DOM text and keep its source context readable. The
recorded target and final crop must pass the same fail-closed QA as a still
capture. Use a still only when motion would add no source context or when the
source itself is a static document/image.

The same fields may be supplied by a `research/capture-targets*.json` overlay
keyed by slot ID. Invalid targets and crops are rejected during planning before
browser or network work begins.

### Several slots from one URL

Several different targets on one exact URL share one browser navigation and
one source probe where possible, but every slot still resolves its own target
and receives distinct pixels, media, asset ID, and provenance record. URL
equality alone never means that two shots are visually identical.

Use the same non-empty `capture_spec_fingerprint` only when the complete capture
request is intentionally identical. That explicit fingerprint permits
in-batch pixel reuse while preserving a separate output for each slot. Never
assign one fingerprint to different selectors, text targets, scroll positions,
or crops.

### Target framing and fail-closed QA

A resolved selector or text target receives an adaptive, bounded 16:9 document
window. Small text keeps at least half a viewport of context; larger evidence
expands the window to retain the target plus surrounding context. A target that
cannot fit without silently clipping selected evidence is refused and asks for
a tighter selector or explicit crop. The resolved target, clip, page bounds,
and authored crop are recorded in provenance notes.

Browser QA rejects detected cookie-consent walls, CAPTCHA or human-verification
challenges, Cloudflare/browser challenges, HTTP error pages, and obstructive
login, subscription, or cookie interstitials. RabbitHole does not accept or
dismiss consent UI automatically. Capture an unobstructed archive, select a
retained source frame, or author a disclosed evidence-card alternative.

Blank-frame QA also fails closed. Sparse black-on-white article text may pass
only when the browser resolved an exact target and the retained target pixels
have text-like structure. Uniform frames, failed embeds, error panels, and
repeated dot or box grids remain rejected. This narrow fallback is not an
operator bypass.

When usable source pixels exist, keep them on screen. If a particular claim is
not fully verified, add a small claim-specific qualifier/source label in a safe
corner; do not replace real pixels with a full-screen `source unverified`
placeholder. When source pixels cannot be retained at all, the supported local
evidence-card strategies are explicit editorial choices:

```text
manual-editorial-card-no-page-capture
catalogue-metadata-card-no-page-or-media-access
local-current-context-citation-card-after-fetch-failure
```

Set one as `capture_strategy` with a concise `capture_note`, title, date, and
source URL. The resulting card is visibly labelled
`EDITORIAL PARAPHRASE · SOURCE-ATTRIBUTED` and appears as `CITATION CARD` during
visual QA. It must not be described as a screenshot.

Final-quality source coverage uses two duration-weighted measurements:

- **Source-backed coverage must be at least 50%.** Primary and archival
  records count here, including a disclosed citation card whose claim and URL
  were reviewed.
- **Source-pixel coverage must independently be at least 35%.** Only retained
  pixels from primary/archival pages, videos, or licensed images count. A
  `rabbithole-evidence-card` never enters this measurement.

The second floor prevents a timeline made mostly from local paraphrase cards
from passing the documentary evidence gate merely because those records cite
real sources. Ordinary generated graphics, plates, and atmospherics enter
neither measurement.

Keep card density restrained. Prefer moving primary-source footage, Chromium
evidence, retained video frames, and archival material; use a designed card for
chapter structure or a concept that source pixels cannot communicate. Merge
adjacent same-heading cards into one stable slide of at most six rows and move a
yellow band to the currently narrated row. Do not zoom that reading surface.
Avoid unrelated consecutive text-led cards and plan caption exclusion over
every text-led shot.

## Deriving video from a licensed source image

A direct image URL can be retained once and transformed into distinct,
slot-length crops. The catalogue must record a machine-readable licence
identifier explicitly:

```json
{
  "artifact_id": "commons-receiver-s062",
  "url": "https://upload.wikimedia.org/example.jpg",
  "title": "Shortwave receiver",
  "slot_id": "s062",
  "acquisition_mode": "screenshot-only",
  "capture_strategy": "licensed-direct-image-crop-deferred",
  "source_license": "CC0-1.0",
  "rights_note": "Photographer, source page, licence URL, and credit details",
  "source_image_crop": [750, 100, 1400, 788],
  "source_attribution": "Photographer / Wikimedia Commons"
}
```

`source_license` must be one of RabbitHole's known direct-image SPDX IDs:
`CC-PDDC`, `CC0-1.0`, or `CC-BY-1.0` through `CC-BY-4.0` (including
`CC-BY-2.5`). A custom `LicenseRef-*` is accepted only when both
`rights_note` and `source_attribution` document the permission or legal basis.
Prose, licence URLs, unknown tokens, and near-miss IDs such as `CCO-1.0` are
refused. The exact identifier is copied into the provenance record—RabbitHole
never assumes CC0 from the host, filename, capture strategy, or rights-note
prose.

## Deriving a frame from retained source video

A screenshot slot can use an exact frame from an already acquired source-video
slot without reopening the public page:

```json
{
  "artifact_id": "webdriver-frame-07",
  "url": "https://www.youtube.com/watch?v=example",
  "title": "Webdriver Torso upload",
  "date": "2013-09-27",
  "slot_id": "s052",
  "acquisition_mode": "screenshot-only",
  "source_video_slot": "s051",
  "source_frame_timestamp": 12.5,
  "source_frame_crop": [180, 90, 1280, 720],
  "source_attribution": "Webdriver Torso / YouTube",
  "source_date_label": "Uploaded 27 Sep 2013"
}
```

The referenced source slot must already have validated retained video.
`source_frame_timestamp` is required, source-relative, finite, non-negative,
and strictly inside that video. The optional crop is
`[x, y, width, height]` in encoded source pixels; rotation metadata is not
applied to those coordinates.

The derived asset is a silent, constant-frame-rate 1920×1080 H.264 MP4 whose
frame count covers the complete slot. It is probed before atomically replacing
the destination. Optional attribution and date are burned into the frame, so
the Resolve compiler does not add a duplicate source caption.

## Portable primary-video downloads

Primary-video retrieval remains gated by an exact matching source URL in
`claims.json`. The uv environment supplies `yt-dlp`; do not depend on a
machine-global installation.

RabbitHole requests a Resolve-portable MP4 profile: AVC/H.264 video with
AAC/M4A audio where available, an H.264/AAC combined MP4 fallback, and an
H.264-only fallback. It remuxes to MP4, refuses playlists, applies bounded
socket/process retries and timeouts, then uses ffprobe to require a decodable
visual stream. Failed or non-video output is removed and never recorded.

## Contact-sheet approval

After each meaningful acquisition pass, render read-only review sheets:

```text
uv run python -m rabbithole.contactsheet projects/<slug>/narration/timing.json
```

The default output is
`projects/<slug>/review/contact-sheets/`. Graphics/plates and evidence are
paginated separately in timeline order. Each cell shows slot ID, time span,
detail, asset/provider, and a sampled local frame. Derived media is labelled
`SOURCE FRAME`, `SOURCE IMAGE`, or `CITATION CARD` rather than pretending to be
a browser screenshot.

Missing or unreadable assets become red error cards instead of aborting the
remaining pages. The command writes `contact-sheets.json`; exit code `1` means
the sheets were created with QA issues, while exit code `2` means the sheet
operation itself failed.

Do not approve assets or build the EDL until:

- every intended slot has the correct evidence or explanatory graphic;
- every red error card is resolved;
- crops retain the selected evidence and enough source context;
- source titles, dates, rights notes, and editorial disclosures are accurate;
- repeated URLs show the intended distinct targets;
- source-derived frames show the intended timestamp and crop.

## Editable source captions in Resolve

For direct-source media with an original URL, the Resolve compiler derives an
editable lower-left V3 source caption from provenance. It uses the authored
source title (or provider/host fallback) and publication/source date. The
retrieval timestamp is audit metadata and never becomes an on-screen date.

Contiguous cuts from the same continuous source span share one caption.
An authored non-empty `source_caption` overlay suppresses generation over its
covered span. Media whose attribution is already burned in—evidence cards,
source frames, and source images—does not receive a second caption.

FCPXML carries generated source captions as editable Basic Title items, not
burned pixels. Review their text, placement, and duration in Resolve.

## Signal-comparison explanatory cards

An ordinary comparison remains the existing text-column card. The following
opt-in graphic headings render a reusable `REFERENCE / PROCESSED` signal motif:

```text
[SHOT:graphic signal comparison - edge baseline: REFERENCE - SHARP RED/BLUE EDGES | PROCESSED - SAME TEST SIGNAL]
[SHOT:graphic signal comparison - processed change: REFERENCE - SHARP EDGE | PROCESSED - BLUR + COLOUR SHIFT]
[SHOT:graphic signal comparison - timing and audio: EXPECTED - FRAME 00 + TONE A | PROCESSED - FRAME +02 + TONE DELTA]
[SHOT:graphic signal comparison - automated flag: EDGE FLAGGED | COLOUR FLAGGED | TIMING FLAGGED | AUDIO FLAGGED]
```

These are explanatory illustrations of general testing logic, never evidence of
a private implementation. Every signal card burns both safeguards into the
frame:

```text
LOCAL ILLUSTRATION · GENERAL TESTING LOGIC
NOT WEBDRIVER TORSO'S PUBLISHED ALGORITHM
```

The signal vocabulary is closed: unsupported variants are refused. `edge
baseline`, `processed change`, and `timing and audio` each require exactly two
pipe-separated labels; `automated flag` requires exactly four. Missing or extra
items are an error rather than being silently omitted from the rendered frame.

Verify the motif, authored labels, and both safeguards on the graphics contact
sheet before approval.
