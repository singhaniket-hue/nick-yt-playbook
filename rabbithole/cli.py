"""Command-line entry points."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from rabbithole.assets import (
    QUALITY_MODES,
    artifact_urls_by_slot,
    check_source_quality,
    execute_plan,
    format_plan,
    load_artifacts,
    plan_assets,
)
from rabbithole.audiomix import bed_spans, build_mix, sfx_events, silence_windows
from rabbithole.config import REPO_ROOT, load_config
from rabbithole.edl import ASL_MAX_SECONDS, ASL_MIN_SECONDS, Cut, build_edl, check_edl
from rabbithole.graphics import draw_graphics
from rabbithole.highlights import (
    apply_highlights,
    load_highlights,
    window_highlights,
)
from rabbithole.jsonio import read_json
from rabbithole.markers import parse
from rabbithole.narrate import estimate_characters, plan_narration
from rabbithole.overlays import Overlay, build_overlays, check_overlays
from rabbithole.pipeline import render_narration
from rabbithole.provenance import (
    TIERS,
    AssetRecord,
    add_record,
    check_provenance,
    load_provenance,
    save_provenance,
)
from rabbithole.render import assemble_footage, deferred_audio_cues, finish
from rabbithole.resolve_cli import add_resolve_parser, cmd_legacy_resolve_render
from rabbithole.scaffold import new_project
from rabbithole.segments import (
    parse_timecode,
    segment_slug,
    trim_audio,
    validate_window,
    window_cuts,
    window_document,
    window_overlays,
)
from rabbithole.slots import build_slots, check_slots
from rabbithole.sourceaudio import load_source_audio, window_source_audio
from rabbithole.sources.plates import load_grade
from rabbithole.sources.sfx import SFX_NAMES
from rabbithole.sources.soundgen import BED_VARIANTS
from rabbithole.subtitles import burn, group_cues, pick_font, write_ass
from rabbithole.timing import WPM_BAND, rebuild_timing, timing_summary
from rabbithole.validate import (
    Finding,
    check_transliterated_english,
    check_transliteration,
    format_report,
    load_claims,
    load_sfx_names,
    validate_all,
)


def _claims_for(script_path: Path) -> list[dict]:
    """Locate claims.json for a script at projects/<slug>/script/<file>.md."""
    return load_claims(script_path.parent.parent / "claims.json")


def _portable_asset_record(record: AssetRecord, project_root: Path) -> AssetRecord:
    """Store media under an episode as project-relative POSIX paths."""

    raw = Path(record.local_path).expanduser()
    resolved = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
    try:
        local_path = resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        local_path = record.local_path.replace("\\", "/")
    return dataclasses.replace(record, local_path=local_path)


def _quality_mode(args: argparse.Namespace) -> str:
    """CLI quality mode, with compatibility for direct legacy API callers.

    Parsed CLI commands always carry ``quality`` and default to ``final``.
    Older code and focused tests construct ``argparse.Namespace`` directly;
    those remain explicitly preview-like instead of changing behaviour under
    their feet.
    """
    return getattr(args, "quality", "animatic")


def _devanagari_for(script_path: Path) -> Path:
    """The Devanagari TTS edition that pairs with a canonical script."""
    return script_path.parent / "05-devanagari.md"



def _romanized_words_for(project_root: Path) -> list[str] | None:
    """The canonical script's words, for resolving `[KEY:]` markers.

    Returns None when the canonical script is missing, which makes keyword
    matching fall back to the timing spine's own words -- correct for a script
    narrated from its canonical edition, and no worse than before for anything
    else.
    """
    script_path = project_root / "script" / "04-final.md"
    if not script_path.exists():
        return None
    return parse(script_path.read_text(encoding="utf-8")).text.split()


def cmd_validate(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    source = script_path.read_text(encoding="utf-8")
    parsed = parse(source)
    sfx_names = load_sfx_names(REPO_ROOT / "style" / "sfx.json")

    deva_path = _devanagari_for(script_path)
    deva_parsed = parse(deva_path.read_text(encoding="utf-8")) if deva_path.exists() else None

    findings = validate_all(
        parsed, sfx_names=sfx_names, wpm=args.wpm, claims=_claims_for(script_path)
    )
    findings += check_transliteration(parsed, deva_parsed)
    if deva_parsed is not None:
        findings += check_transliterated_english(deva_parsed)
    print(format_report(findings))
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_narrate(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    source = script_path.read_text(encoding="utf-8")
    cfg = load_config()

    parsed = parse(source)
    sfx_names = load_sfx_names(REPO_ROOT / "style" / "sfx.json")

    deva_path = _devanagari_for(script_path)
    deva_source = deva_path.read_text(encoding="utf-8") if deva_path.exists() else None
    deva_parsed = parse(deva_source) if deva_source is not None else None

    findings = validate_all(
        parsed, sfx_names=sfx_names, wpm=cfg.wpm, claims=_claims_for(script_path)
    )
    findings += check_transliteration(parsed, deva_parsed)
    if deva_parsed is not None:
        findings += check_transliterated_english(deva_parsed)
    if any(f.severity == "error" for f in findings) and not args.force:
        print(format_report(findings))
        print("\nRefusing to narrate a script that fails the gates. Use --force to override.")
        return 1

    # The voice clone is a Hindi voice: render from the Devanagari edition, not the
    # canonical romanized script, or eleven_multilingual_v2 has to guess per-token
    # whether Latin input is Hindi or English and sometimes guesses wrong.
    if deva_source is not None:
        narrate_source = deva_source
        narrate_parsed = deva_parsed
    else:
        narrate_source = source
        narrate_parsed = parsed
        print(
            "WARNING: no 05-devanagari.md found; narrating from the romanized "
            "script. Romanized Hinglish input mispronounces on a Hindi voice clone."
        )

    characters = estimate_characters(plan_narration(narrate_parsed))
    print(f"Billable characters: {characters:,}")
    if args.dry_run:
        return 0

    out = Path(args.out)
    result = render_narration(narrate_source, cfg, out, out.parent / "chunks")
    print(
        f"Wrote {result.output_path} - {result.duration_seconds / 60:.1f} min, "
        f"{result.chunk_count} chunks, {result.silence_count} silence gaps, "
        f"{result.characters:,} characters."
    )
    return 0


def cmd_timing(args: argparse.Namespace) -> int:
    """Summarise a timing spine. This is the tool that answers the WPM question."""
    path = Path(args.timing_json)
    document = read_json(path)
    summary = timing_summary(document)

    minutes, seconds = divmod(summary["duration_seconds"], 60)
    print(f"Duration: {int(minutes)}m {seconds:04.1f}s")
    print(f"Words: {summary['word_count']:,}")
    print(f"Measured WPM: {summary['measured_wpm']:.1f}")
    band = "within" if summary["in_band"] else "outside"
    print(f"Measured WPM is {band} the {WPM_BAND[0]}-{WPM_BAND[1]} band.")

    print("Marker counts:")
    if summary["marker_counts"]:
        for kind, count in summary["marker_counts"].items():
            print(f"  {kind}: {count}")
    else:
        print("  (none)")

    if summary["rehook_gaps_minutes"]:
        gaps = ", ".join(f"{gap:.2f}" for gap in summary["rehook_gaps_minutes"])
        print(f"REHOOK gaps (minutes): {gaps}")
    else:
        print("REHOOK gaps (minutes): none")

    return 0


def cmd_remark(args: argparse.Namespace) -> int:
    """Re-derive a timing spine after editing only the `[SHOT:]` markers.

    Markers are stripped before TTS, so adding or moving one leaves every
    spoken word and every recorded timestamp valid -- the audio does not need
    re-rendering and nothing is spent. Refuses outright if the words changed,
    because the recorded timings would no longer describe the text.
    """
    script_path = Path(args.script)
    timing_path = Path(args.timing_json)

    deva_path = _devanagari_for(script_path)
    if not deva_path.exists():
        print(
            f"No {deva_path.name} beside {script_path.name}. The timing spine is "
            f"built from the Devanagari edition that was narrated, so re-marking "
            f"needs it."
        )
        return 1

    document = read_json(timing_path)
    before = len([m for m in document.get("markers", []) if m.get("kind") == "SHOT"])

    parsed = parse(deva_path.read_text(encoding="utf-8"))
    rebuilt, problems = rebuild_timing(parsed, document)

    if problems:
        for problem in problems:
            print(f"REFUSED: {problem}")
        return 1

    after = len([m for m in rebuilt["markers"] if m.get("kind") == "SHOT"])
    print(f"Script:  {script_path}")
    print(f"Spine:   {timing_path}")
    print(f"[SHOT:] markers: {before} -> {after}  ({after - before:+d})")
    print(f"Words unchanged: {rebuilt['word_count']:,} (narration untouched, nothing spent)")

    slots = build_slots(rebuilt)
    long_holds = [
        f for f in check_slots(slots, rebuilt) if "implies roughly" in f.message
    ]
    print(f"Slots:   {len(slots)}   flagged for a long hold: {len(long_holds)}")

    # Slot ids are positional, so changing the marker count renumbers slots and
    # any existing ledger now claims different shots. Say so here rather than
    # letting it surface later as a graphic card sitting in an archival slot.
    if after != before:
        existing = load_provenance(timing_path.parent.parent / "provenance.json")
        if existing:
            print()
            print(
                f"NOTE: {len(existing)} provenance record(s) exist and slot ids are "
                f"positional, so this re-numbering invalidates any that no longer "
                f"match their slot's kind. `assets --dry-run` now reports those as "
                f"'stale'; drop them and re-source before rendering."
            )

    if args.dry_run:
        print()
        print("Dry run: timing.json not written.")
        return 0

    timing_path.write_text(json.dumps(rebuilt), encoding="utf-8")
    print()
    print(f"Wrote {timing_path}")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    """Bind one hand-picked Wikimedia Commons file to one or more slots.

    Keyword search is a poor way to source documentary footage, so hand-picking
    is the normal path for archival material. This fetches a named file with its
    real licence and attribution from the API and writes a provenance record for
    each slot given -- reuse across non-adjacent slots is legitimate and
    `check_provenance` allows it.
    """
    from rabbithole.sources import archives

    timing_path = Path(args.timing_json)
    project_root = timing_path.parent.parent
    document = read_json(timing_path)
    slots = {slot.slot_id: slot for slot in build_slots(document)}

    unknown = [slot_id for slot_id in args.slots if slot_id not in slots]
    if unknown:
        print(f"No such slot(s) in the plan: {', '.join(unknown)}")
        return 1

    wrong_kind = [
        slot_id for slot_id in args.slots if slots[slot_id].kind != "archival"
    ]
    if wrong_kind and not args.force:
        for slot_id in wrong_kind:
            print(
                f"Slot {slot_id!r} is kind {slots[slot_id].kind!r}, not 'archival'. "
                f"A retrieved file would be recorded at tier 'archival' and "
                f"`assets` would then report it as stale. Use --force to override."
            )
        return 1

    try:
        hit = archives.commons_file(args.title, _live_archive_transport)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1

    print(f"Title    : {hit.title}")
    print(f"Licence  : {hit.license}")
    print(f"Media    : {hit.mediatype}")
    print(f"Source   : {hit.original_url}")
    print(f"Slots    : {', '.join(args.slots)}")

    if hit.license == archives.UNKNOWN_LICENSE:
        print()
        print("REFUSED: licence could not be determined, so this cannot enter the edit.")
        return 1

    if args.dry_run:
        print()
        print("Dry run: nothing downloaded, nothing recorded.")
        return 0

    out_dir = Path(args.out_dir) if args.out_dir else project_root / "assets"
    suffix = Path(hit.media_url).suffix or ".bin"
    out_path = out_dir / f"{args.slots[0]}-{hit.identifier}{suffix}"
    try:
        archives.download(hit, out_path, _live_archive_transport)
    except RuntimeError as exc:
        print(f"REFUSED: {exc}")
        return 1

    fields = archives.to_record_fields(hit, f"archival-{args.slots[0]}", out_path)
    fields["used_in_slots"] = tuple(args.slots)
    record = _portable_asset_record(AssetRecord(**fields), project_root)

    records = load_provenance(project_root / "provenance.json")
    records = [r for r in records if r.asset_id != record.asset_id]
    records.append(record)
    save_provenance(project_root / "provenance.json", records)

    print()
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1e6:.2f} MB)")
    print(f"Recorded as {record.asset_id!r} for {len(args.slots)} slot(s).")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    configured = getattr(args, "projects_dir", None) or os.environ.get(
        "RABBITHOLE_PROJECTS_DIR"
    )
    projects_dir = (
        Path(configured).expanduser().resolve()
        if configured
        else REPO_ROOT / "projects"
    )
    root = new_project(projects_dir, args.slug)
    print(f"Created {root}")
    return 0


# Wikimedia's API policy requires a User-Agent identifying the client, and it is
# enforced, not advisory: requests sent with the `python-requests/x.y.z` default
# come back 403 with an empty result set. Because `search_archives` treats a
# failed provider as "no hits" and moves on, that failure is invisible -- every
# Wikimedia search silently returns nothing and the archival tier looks like it
# simply found less than it should. Measured directly: default UA 403, explicit
# UA 200. archive.org does not require one but is happier with it too.
ARCHIVE_USER_AGENT = "rabbithole/1.0 (Crowley-format documentary pipeline)"


# Minimum gap between requests to one host, and how many times to retry a 429.
#
# Sourcing 50 archival slots means 150+ requests (search, then metadata, then
# download, per slot) fired as fast as the loop runs. upload.wikimedia.org
# answers that with HTTP 429, and it did: the first live run of all 50 slots
# downloaded nothing at all for this reason. These are deliberately polite --
# the archives are free infrastructure and this is a bulk consumer of them.
ARCHIVE_MIN_REQUEST_INTERVAL = 0.75
ARCHIVE_RATE_LIMIT_RETRIES = 4
ARCHIVE_RATE_LIMIT_BACKOFF = 3.0

_last_request_at: dict[str, float] = {}


def _live_archive_transport(url: str) -> tuple[int, bytes]:
    """The real archives.Transport used outside tests: a throttled HTTP GET.

    Only reached in live (non-dry-run) `assets` runs that plan an archival
    `search` item -- never exercised by this package's test suite, which always
    injects a fake transport instead. That is exactly why the User-Agent and the
    throttling here are easy to get wrong and impossible to catch there.

    Retries a 429 with linear backoff, honouring `Retry-After` when the server
    sends one. A 429 that survives every retry is returned as-is rather than
    raised, because `search_archives` and `download` already know how to report
    a non-200 per slot and one exhausted host should not abort the whole run.
    """
    import time
    from urllib.parse import urlparse

    import requests

    host = urlparse(url).netloc
    elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
    if elapsed < ARCHIVE_MIN_REQUEST_INTERVAL:
        time.sleep(ARCHIVE_MIN_REQUEST_INTERVAL - elapsed)

    response = None
    for attempt in range(ARCHIVE_RATE_LIMIT_RETRIES):
        response = requests.get(
            url, timeout=60, headers={"User-Agent": ARCHIVE_USER_AGENT}
        )
        _last_request_at[host] = time.monotonic()
        if response.status_code != 429:
            break
        retry_after = response.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else ARCHIVE_RATE_LIMIT_BACKOFF * (attempt + 1)
        except ValueError:
            wait = ARCHIVE_RATE_LIMIT_BACKOFF * (attempt + 1)
        time.sleep(min(wait, 30.0))

    return response.status_code, response.content


def cmd_assets(args: argparse.Namespace) -> int:
    """Plan, and unless --dry-run source, every visual asset a slot plan calls for."""
    timing_path = Path(args.timing_json)
    document = read_json(timing_path)
    # projects/<slug>/narration/timing.json -> projects/<slug>
    project_root = timing_path.parent.parent

    slots = build_slots(document)
    slot_findings = check_slots(slots, document)
    print(format_report(slot_findings))

    records = load_provenance(project_root / "provenance.json")
    claims = load_claims(project_root / "claims.json")
    artifacts = load_artifacts(project_root / "research" / "artifacts.json")

    items = plan_assets(slots, records, artifacts)
    quality = _quality_mode(args)
    if args.tier:
        items = [item for item in items if item.tier == args.tier]

    print()
    print(format_plan(items))

    if args.dry_run:
        dry_findings: list[Finding] = []
        if quality == "final":
            dry_findings += check_provenance(
                records, [slot.slot_id for slot in slots]
            )
            dry_findings += check_source_quality(
                slots, records, quality=quality
            )
        if dry_findings:
            print()
            print(format_report(dry_findings))
        all_dry_findings = [*slot_findings, *dry_findings]
        return 1 if any(f.severity == "error" for f in all_dry_findings) else 0

    out_dir = Path(args.out_dir) if args.out_dir else project_root / "assets"
    grade = load_grade(REPO_ROOT / "style")

    new_records, exec_findings = execute_plan(
        items,
        slots,
        out_dir,
        grade=grade,
        claims=claims,
        archive_transport=_live_archive_transport,
        artifacts=artifact_urls_by_slot(artifacts),
        typography=read_json(REPO_ROOT / "style" / "typography.json"),
        palette=read_json(REPO_ROOT / "style" / "palette.json"),
        quality=quality,
    )
    new_records = [
        _portable_asset_record(record, project_root) for record in new_records
    ]
    if exec_findings:
        print()
        print(format_report(exec_findings))

    all_records = records
    for record in new_records:
        all_records = add_record(all_records, record)
    save_provenance(project_root / "provenance.json", all_records)

    slot_ids = [slot.slot_id for slot in slots]
    provenance_findings = check_provenance(all_records, slot_ids)
    source_quality_findings = check_source_quality(
        slots, all_records, quality=quality
    )
    print()
    print(format_report([*provenance_findings, *source_quality_findings]))

    all_findings = [
        *slot_findings,
        *exec_findings,
        *provenance_findings,
        *source_quality_findings,
    ]
    return 1 if any(f.severity == "error" for f in all_findings) else 0


def _edl_document(
    document: dict,
    cuts: list[Cut],
    overlays: list[Overlay],
    *,
    quality: str,
    mode: str,
) -> dict:
    """The serialisable EDL document written by `cmd_edl`."""
    total = sum(cut.duration for cut in cuts)
    average_shot_length = total / len(cuts) if cuts else 0.0
    return {
        "quality": quality,
        "mode": mode,
        "duration_seconds": document.get("duration_seconds", 0.0),
        "cut_count": len(cuts),
        "average_shot_length": average_shot_length,
        "cuts": [dataclasses.asdict(cut) for cut in cuts],
        "overlays": [dataclasses.asdict(overlay) for overlay in overlays],
    }


def _print_edl_summary(
    cuts: list[Cut],
    overlays: list[Overlay],
    *,
    quality: str,
    mode: str,
) -> None:
    total = sum(cut.duration for cut in cuts)
    average_shot_length = total / len(cuts) if cuts else 0.0
    band = "inside" if ASL_MIN_SECONDS <= average_shot_length <= ASL_MAX_SECONDS else "outside"

    origins = Counter(cut.origin for cut in cuts)
    overlay_kinds = Counter(overlay.kind for overlay in overlays)
    overlay_summary = (
        ", ".join(f"{kind}={count}" for kind, count in sorted(overlay_kinds.items()))
        if overlay_kinds
        else "(none)"
    )

    print(f"Quality: {quality} ({mode} EDL)")
    print(f"Cuts: {len(cuts)}")
    print(
        f"Average shot length: {average_shot_length:.2f}s "
        f"({band} the {ASL_MIN_SECONDS}-{ASL_MAX_SECONDS}s target band)"
    )
    print(f"Origins: script={origins.get('script', 0)}, asl-fill={origins.get('asl-fill', 0)}")
    print(f"Overlays: {overlay_summary}")


def cmd_edl(args: argparse.Namespace) -> int:
    """Build the cut list and overlay instructions for a timing spine, and report on them."""
    timing_path = Path(args.timing_json)
    document = read_json(timing_path)
    # projects/<slug>/narration/timing.json -> projects/<slug>
    project_root = timing_path.parent.parent

    quality = _quality_mode(args)
    mode = "editorial" if quality == "final" else "animatic"
    slots = build_slots(document)
    cuts = build_edl(slots, document, mode=mode)

    # `[KEY:]` args are written in the canonical romanized script, but the
    # timing spine's words come from whichever edition was narrated -- the
    # Devanagari one, for a Hindi voice clone. Matching romanized args against
    # Devanagari text silently loses every genuinely Hindi keyword, so the
    # romanized words are supplied and the spine is used only for timing.
    romanized_words = _romanized_words_for(project_root)
    overlays = build_overlays(document, cuts, romanized_words)

    findings = [
        *check_slots(slots, document),
        *check_edl(cuts, document),
        *check_overlays(overlays, document, romanized_words),
    ]
    print(format_report(findings))
    print()
    _print_edl_summary(cuts, overlays, quality=quality, mode=mode)

    if not args.dry_run:
        out_path = Path(args.out) if args.out else project_root / "edit" / "edl.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                _edl_document(
                    document,
                    cuts,
                    overlays,
                    quality=quality,
                    mode=mode,
                ),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {out_path}")

    return 1 if any(f.severity == "error" for f in findings) else 0


def _slots_asset_coverage(slots, records) -> tuple[list[str], list[str]]:
    """Which slot_ids have a provenance record claiming them, and which don't."""
    covered = {slot_id for record in records for slot_id in record.used_in_slots}
    with_assets = [slot.slot_id for slot in slots if slot.slot_id in covered]
    without_assets = [slot.slot_id for slot in slots if slot.slot_id not in covered]
    return with_assets, without_assets


def _audio_peak_dbfs(path: Path) -> float:
    """Peak level in dBFS of a rendered file's audio stream, via volumedetect."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
    )
    text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"max_volume:\s*(-?\d+\.?\d*) dB", text)
    return float(match.group(1)) if match else float("nan")


def _probe_render(path: Path) -> dict:
    """Duration, video dimensions/frame rate, and stream count, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data["format"]["duration"]),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": video.get("r_frame_rate"),
        "stream_count": len(streams),
    }


DEFAULT_SOUND_LIBRARY = REPO_ROOT / "assets" / "soundlib"


def _sound_library(args: argparse.Namespace):
    """The library `render` should mix from, or None to use local synthesis.

    Default is on-if-present: an author who has run `sound build` gets the
    generated cues without having to remember a flag, and an author who has
    not gets exactly the old synthesized behaviour rather than an error.
    `--no-sound-library` forces synthesis even when a library exists.
    """
    from rabbithole.sources.soundgen import Library

    if getattr(args, "no_sound_library", False):
        return None
    root = Path(getattr(args, "sound_library", None) or DEFAULT_SOUND_LIBRARY)
    library = Library(root=root)
    return library if library.manifest_path.exists() else None


def cmd_sound(args: argparse.Namespace) -> int:
    """Generate the SFX cues and music beds named by the style pack.

    Costs real money on the author's ElevenLabs account, so `--dry-run` is
    the default-safe way to see the plan first -- the same convention
    `narrate` follows. Generation is cached by content fingerprint, so a
    re-run after an unrelated prompt edit re-spends only on what changed.
    """
    from rabbithole.sources.music import BED_KINDS
    from rabbithole.sources.sfx import DEFAULT_DURATIONS
    from rabbithole.sources.soundgen import (
        BED_VARIANT_SECONDS,
        MAX_DURATION,
        MIN_DURATION,
        Library,
        SoundRequest,
        build_library,
        load_prompts,
    )

    prompts = load_prompts(REPO_ROOT / "style" / "sound-prompts.json")
    library = Library(root=Path(args.library or DEFAULT_SOUND_LIBRARY))
    bed_kinds = tuple(k for k in BED_KINDS if k != "silence")

    planned: list[tuple[str, float, bool]] = []
    for name, prompt in sorted(prompts["sfx"].items()):
        duration = max(MIN_DURATION, min(MAX_DURATION, float(DEFAULT_DURATIONS.get(name, 1.0))))
        request = SoundRequest(slug=f"sfx/{name}", prompt=prompt, duration=duration)
        planned.append((f"sfx/{name}", duration, library.has(f"sfx/{name}", request.fingerprint)))
    for kind in bed_kinds:
        prompt = prompts["beds"].get(kind)
        if not prompt:
            continue
        for variant in range(args.bed_variants):
            request = SoundRequest(slug=f"beds/{kind}", prompt=prompt,
                                   duration=BED_VARIANT_SECONDS, variant=variant, loop=True)
            key = f"beds/{kind}/{variant}"
            planned.append((key, BED_VARIANT_SECONDS, library.has(key, request.fingerprint)))

    to_make = [p for p in planned if not p[2]]
    seconds = sum(p[1] for p in to_make)

    print(f"Library: {library.root}")
    print(f"Prompts: {REPO_ROOT / 'style' / 'sound-prompts.json'}")
    print()
    for key, duration, cached in planned:
        print(f"  {'cached  ' if cached else 'GENERATE'}  {key:<28} {duration:>5.1f}s")
    print()
    print(f"{len(to_make)} to generate, {len(planned) - len(to_make)} cached")
    # Measured against the live endpoint at ~11 characters per generated
    # second; an estimate, not a quote, so it is labelled as one.
    print(f"{seconds:.1f} generated seconds, approximately {round(seconds * 11)} characters")

    if args.dry_run:
        print()
        print("Dry run: nothing generated, nothing spent.")
        return 0

    # No early return when everything is cached. Bed levelling is a *derived*
    # step (`normalize_bed_group`, run over the cached raws), so it still has
    # to happen on a fully-cached run -- skipping it would leave the playable
    # beds at whatever level the last build left them.
    cfg = load_config()
    print()
    counts = build_library(
        library, prompts, cfg.elevenlabs_api_key,
        sfx_durations=DEFAULT_DURATIONS,
        bed_kinds=bed_kinds,
        bed_variants=args.bed_variants,
        on_progress=lambda key, made: print(f"  {'generated' if made else 'cached   '}  {key}"),
    )
    print()
    print(f"Generated {counts['generated']}, reused {counts['reused']}.")
    print(f"Manifest: {library.manifest_path}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Render an episode: layer 1 footage assembly, layer 2 audio mix, layer 3 finish.

    `--dry-run` never touches ffmpeg: it reports the plan (cut count, total
    duration, which slots have assets, and which [SFX:]/[MUSIC:] cues cannot
    be resolved at all) and exits. Live mode assembles the cut timeline,
    builds the SFX/bed audio mix (unless `--no-audio-mix`), applies the
    style pack's grade, muxes the result, and verifies the output with
    ffprobe before printing a final report.

    `--no-audio-mix` skips `audiomix.build_mix` entirely and muxes the bare
    VO instead, the same as before this layer existed -- a fast path for
    drafts that don't need SFX/bed synthesis.
    """
    if getattr(args, "backend", "ffmpeg") == "resolve":
        return cmd_legacy_resolve_render(args)

    timing_path = Path(args.timing_json)
    document = read_json(timing_path)
    # projects/<slug>/narration/timing.json -> projects/<slug>
    project_root = timing_path.parent.parent

    start_arg = getattr(args, "start", None)
    end_arg = getattr(args, "end", None)
    if (start_arg is None) != (end_arg is None):
        print(
            format_report(
                [
                    Finding(
                        gate="render",
                        severity="error",
                        message=(
                            "Segment review requires both --start and --end. "
                            "Neither value was inferred, so a full render was not started."
                        ),
                    )
                ]
            )
        )
        return 1

    quality = _quality_mode(args)
    segment_window: tuple[float, float] | None = None
    render_document = document
    if start_arg is not None and end_arg is not None:
        try:
            start = parse_timecode(start_arg)
            end = parse_timecode(end_arg)
            validate_window(start, end, float(document.get("duration_seconds", 0.0)))
        except ValueError as exc:
            print(
                format_report(
                    [Finding(gate="render", severity="error", message=str(exc))]
                )
            )
            return 1
        segment_window = (start, end)
        render_document = window_document(document, start, end)

    source_audio_manifest = project_root / "research" / "source-audio.json"
    try:
        source_audio_bites = load_source_audio(
            source_audio_manifest,
            project_root,
            episode_duration=float(document.get("duration_seconds", 0.0)),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            format_report(
                [
                    Finding(
                        gate="source-audio",
                        severity="error",
                        message=f"Invalid source-audio manifest: {exc}",
                    )
                ]
            )
        )
        return 1
    if segment_window is not None:
        source_audio_bites = window_source_audio(
            source_audio_bites, *segment_window
        )

    try:
        highlights = load_highlights(
            project_root / "research" / "highlights.json", document
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            format_report(
                [
                    Finding(
                        gate="highlights",
                        severity="error",
                        message=f"Invalid article-highlight spec: {exc}",
                    )
                ]
            )
        )
        return 1
    if segment_window is not None:
        highlights = window_highlights(highlights, *segment_window)
    highlight_findings = [
        Finding(
            gate="highlights",
            severity="error" if quality == "final" else "warning",
            message=(
                f"Article highlight {item.label or '(unlabelled)'} at "
                f"{item.start:.2f}-{item.end:.2f}s has no source label."
            ),
        )
        for item in highlights
        if not item.source
    ]

    slots = build_slots(document)
    edl_data = read_json(project_root / "edit" / "edl.json")
    cuts = [Cut(**c) for c in edl_data["cuts"]]
    overlays = [Overlay(**o) for o in edl_data.get("overlays", [])]
    if segment_window is not None:
        start, end = segment_window
        cuts = window_cuts(cuts, start, end)
        overlays = window_overlays(overlays, start, end)

    records = load_provenance(project_root / "provenance.json")
    vo_path = project_root / "narration" / "vo.wav"

    selected_slot_ids = {cut.slot_id for cut in cuts}
    selected_slots = [slot for slot in slots if slot.slot_id in selected_slot_ids]
    selected_records = [
        dataclasses.replace(
            record,
            used_in_slots=tuple(
                slot_id
                for slot_id in record.used_in_slots
                if slot_id in selected_slot_ids
            ),
        )
        for record in records
        if any(slot_id in selected_slot_ids for slot_id in record.used_in_slots)
    ]
    with_assets, without_assets = _slots_asset_coverage(selected_slots, selected_records)
    deferred = deferred_audio_cues(render_document)
    total_duration = sum(cut.duration for cut in cuts)
    provenance_findings = check_provenance(
        selected_records, [slot.slot_id for slot in selected_slots]
    )
    source_quality_findings = check_source_quality(
        selected_slots, selected_records, quality=quality
    )
    preflight_findings = [
        *provenance_findings,
        *source_quality_findings,
        *highlight_findings,
    ]

    if args.dry_run:
        findings = [*preflight_findings, *deferred]
        print(format_report(findings))
        print()
        if segment_window is not None:
            print(
                f"Review segment: {segment_window[0]:.3f}s to "
                f"{segment_window[1]:.3f}s"
            )
        print(f"Cuts: {len(cuts)}")
        print(f"Article highlight lines: {len(highlights)}")
        if source_audio_manifest.exists():
            print(f"Source-audio bites: {len(source_audio_bites)}")
        print(f"Total duration: {total_duration:.2f}s")
        print(
            f"Slots with assets: {len(with_assets)}/{len(slots)} "
            f"({', '.join(with_assets) if with_assets else '(none)'})"
        )
        print(
            f"Slots without assets: {len(without_assets)} "
            f"({', '.join(without_assets) if without_assets else '(none)'})"
        )
        return 1 if any(f.severity == "error" for f in findings) else 0

    if any(f.severity == "error" for f in preflight_findings):
        print(format_report(preflight_findings))
        print(
            "\nRefusing to render: the selected visual timeline fails provenance "
            f"or {quality}-quality source gates. Use --quality animatic only for "
            "an intentional preview; provenance errors still require repair."
        )
        return 1

    grade = load_grade(REPO_ROOT / "style")
    named_lut = grade.get("lut")
    lut_path = (REPO_ROOT / named_lut) if named_lut else None
    lut_applied = lut_path is not None and lut_path.exists()

    if args.out:
        out_path = Path(args.out)
    elif segment_window is not None:
        out_path = (
            project_root
            / "renders"
            / "segments"
            / f"{segment_slug(*segment_window)}.mp4"
        )
    else:
        out_path = project_root / "renders" / "episode.mp4"
    work_dir = (
        out_path.parent / f"{out_path.stem}-work"
        if segment_window is not None
        else out_path.parent / "work"
    )

    footage_path, footage_findings = assemble_footage(
        cuts, slots, records, work_dir / "footage.mp4", work_dir
    )
    findings = [*preflight_findings, *deferred, *footage_findings]

    if not footage_path.exists():
        print(format_report(findings))
        return 1

    active_vo_path = vo_path
    if segment_window is not None:
        active_vo_path = trim_audio(
            vo_path,
            work_dir / "vo-segment.wav",
            start=segment_window[0],
            duration=segment_window[1] - segment_window[0],
        )

    mixed_audio_path = None
    if not args.no_audio_mix:
        library = _sound_library(args)
        if library is not None:
            print(f"Mixing from the generated sound library: {library.root}")
        mixed_audio_path, mix_findings = build_mix(
            render_document,
            active_vo_path,
            work_dir / "mix.wav",
            work_dir / "audiomix",
            REPO_ROOT / "style",
            library=library,
            source_audio_bites=source_audio_bites,
        )
        findings += mix_findings

    # Whichever of subtitles/graphics run last writes directly to `out_path`;
    # every earlier stage writes to an intermediate under `work_dir` instead,
    # exactly the pattern `finish` -> `burn` already used before graphics
    # existed. If neither runs, `finish` writes `out_path` directly, same as
    # always.
    finish_target = (
        work_dir / "finished-preoverlays.mp4"
        if (highlights or args.subtitles or args.graphics)
        else out_path
    )
    finished_path, finish_findings = finish(
        footage_path,
        active_vo_path,
        finish_target,
        grade,
        lut_path=lut_path,
        mixed_audio_path=mixed_audio_path,
    )
    findings += finish_findings

    if highlights:
        probed = _probe_render(finished_path)
        frame_rate = str(probed["frame_rate"])
        if "/" in frame_rate:
            numerator, denominator = frame_rate.split("/", 1)
            fps = float(numerator) / float(denominator)
        else:
            fps = float(frame_rate)
        highlight_target = (
            work_dir / "finished-highlighted.mp4"
            if (args.subtitles or args.graphics)
            else out_path
        )
        finished_path, applied_highlight_findings = apply_highlights(
            finished_path,
            highlights,
            highlight_target,
            width=int(probed["width"]),
            height=int(probed["height"]),
            fps=fps,
        )
        findings += applied_highlight_findings

    typography = None
    if args.subtitles or args.graphics:
        typography = read_json(REPO_ROOT / "style" / "typography.json")

    cue_count = 0
    subtitle_font = None
    if args.subtitles:
        # `[KEY:]` args belong to the canonical romanized script while the
        # narration timing/display spine can be Devanagari.  Use the canonical
        # words only to locate keyword indices; group_cues keeps subtitle text
        # and timing on the active narration spine.
        romanized_words = _romanized_words_for(project_root)
        cues = group_cues(render_document, romanized_words)
        cue_count = len(cues)
        subtitle_font, _font_findings = pick_font(typography)
        ass_path, subtitle_findings = write_ass(cues, typography, out_path.with_suffix(".ass"))
        findings += subtitle_findings
        # Graphics (chapter cards, censor boxes) composite on top of burned-in
        # subtitles, so subtitles are not yet the final stage when graphics
        # is also on.
        subtitle_target = work_dir / "finished-subbed.mp4" if args.graphics else out_path
        finished_path = burn(finished_path, ass_path, subtitle_target)

    chapter_cards_drawn = 0
    censor_boxes_drawn = 0
    if args.graphics:
        palette = read_json(REPO_ROOT / "style" / "palette.json")
        chapter_cards_drawn = sum(1 for o in overlays if o.kind == "chapter-card")
        censor_boxes_drawn = sum(1 for o in overlays if o.kind == "censor")
        finished_path, graphics_findings = draw_graphics(
            finished_path, overlays, typography, palette, out_path, work_dir / "graphics"
        )
        findings += graphics_findings

    print(format_report(findings))

    verification = _probe_render(finished_path)
    print()
    print(f"Wrote {finished_path}")
    print(f"Duration: {verification['duration']:.2f}s")
    print(f"Dimensions: {verification['width']}x{verification['height']} @ {verification['frame_rate']}")
    print(f"Streams: {verification['stream_count']}")

    if args.no_audio_mix:
        print("Audio mix skipped (--no-audio-mix): bare VO muxed.")
    else:
        windows = silence_windows(render_document)
        spans = bed_spans(render_document)
        events = sfx_events(render_document)
        placed = sum(1 for event in events if event.name in SFX_NAMES)
        peak = _audio_peak_dbfs(finished_path)
        print(f"SFX events placed: {placed}/{len(events)}")
        print(f"Bed spans laid: {len(spans)}")
        print(f"Silence windows ducked: {len(windows)}")
        if source_audio_manifest.exists():
            print(f"Source-audio bites mixed: {len(source_audio_bites)}")
        print(f"Final peak: {peak:.2f} dBFS")

    if args.subtitles:
        print(f"Subtitle cues: {cue_count}")
        print(f"Subtitle font: {subtitle_font}")
    else:
        print("Subtitles skipped (--no-subtitles).")

    if args.graphics:
        print(f"Chapter cards drawn: {chapter_cards_drawn}")
        print(f"Censor boxes drawn: {censor_boxes_drawn}")
    else:
        print("Graphics skipped (--no-graphics).")
    print(f"Article highlight lines: {len(highlights)}")
    print(f"LUT applied: {'yes' if lut_applied else 'no'}")
    if segment_window is not None:
        print(
            f"Source window: {segment_window[0]:.3f}s to "
            f"{segment_window[1]:.3f}s (segment-only render)"
        )

    return 1 if any(f.severity == "error" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rabbithole")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Run the review gates on a script")
    validate.add_argument("script", help="Path to the marked-up narration script")
    validate.add_argument("--wpm", type=int, default=177)
    validate.set_defaults(func=cmd_validate)

    narrate = sub.add_parser("narrate", help="Render a script to narration audio")
    narrate.add_argument("script", help="Path to the marked-up narration script")
    narrate.add_argument("--out", required=True, help="Output WAV path")
    narrate.add_argument("--dry-run", action="store_true", help="Report cost only")
    narrate.add_argument("--force", action="store_true", help="Narrate despite failing gates")
    narrate.set_defaults(func=cmd_narrate)

    timing = sub.add_parser("timing", help="Summarise a timing spine (timing.json)")
    timing.add_argument("timing_json", help="Path to timing.json")
    timing.set_defaults(func=cmd_timing)

    remark = sub.add_parser(
        "remark",
        help="Re-derive timing.json after editing only [SHOT:] markers (spends nothing)",
    )
    remark.add_argument("script", help="Path to projects/<slug>/script/04-final.md")
    remark.add_argument("timing_json", help="Path to projects/<slug>/narration/timing.json")
    remark.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the new marker counts and slot check, then exit without writing",
    )
    remark.set_defaults(func=cmd_remark)

    bind = sub.add_parser(
        "bind", help="Bind a hand-picked Wikimedia Commons file to one or more slots"
    )
    bind.add_argument("timing_json", help="Path to projects/<slug>/narration/timing.json")
    bind.add_argument("title", help='Commons file title, e.g. "File:Example archive photograph.jpg"')
    bind.add_argument("slots", nargs="+", help="Slot id(s) to bind it to, e.g. s136 s205")
    bind.add_argument("--out-dir", help="Where the file is written (default: <project>/assets)")
    bind.add_argument(
        "--force", action="store_true",
        help="Bind even to a slot whose kind is not 'archival'",
    )
    bind.add_argument(
        "--dry-run", action="store_true",
        help="Report the licence and attribution, then exit without downloading",
    )
    bind.set_defaults(func=cmd_bind)

    new = sub.add_parser("new", help="Scaffold a new episode project")
    new.add_argument("slug", help="Episode slug, e.g. aviloop-hindi")
    new.add_argument(
        "--projects-dir",
        help=(
            "Project workspace root (default: RABBITHOLE_PROJECTS_DIR or "
            "<checkout>/projects)"
        ),
    )
    new.set_defaults(func=cmd_new)

    assets = sub.add_parser("assets", help="Plan and source visual assets for a slot plan")
    assets.add_argument(
        "timing_json", help="Path to projects/<slug>/narration/timing.json"
    )
    assets.add_argument(
        "--dry-run", action="store_true", help="Print the plan and exit without sourcing anything"
    )
    assets.add_argument(
        "--tier",
        choices=TIERS,
        help=(
            "Limit the plan to one tier. '--tier atmospheric' is the safe first "
            "run: fully local plate generation, no network, no rights questions."
        ),
    )
    assets.add_argument(
        "--out-dir", help="Where sourced media is written (default: <project>/assets)"
    )
    assets.add_argument(
        "--quality",
        choices=QUALITY_MODES,
        default="final",
        help=(
            "Source-quality policy (default: final). 'final' rejects placeholder "
            "cards, invalid scene plates, and insufficient sourced evidence; "
            "'animatic' explicitly permits labelled preview placeholders."
        ),
    )
    assets.set_defaults(func=cmd_assets)

    edl = sub.add_parser("edl", help="Build the cut list and overlay instructions for a timing spine")
    edl.add_argument("timing_json", help="Path to projects/<slug>/narration/timing.json")
    edl.add_argument(
        "--dry-run", action="store_true", help="Print the report and exit without writing edl.json"
    )
    edl.add_argument(
        "--out", help="Where the EDL document is written (default: <project>/edit/edl.json)"
    )
    edl.add_argument(
        "--quality",
        choices=QUALITY_MODES,
        default="final",
        help=(
            "EDL policy (default: final). 'final' emits only authored evidence "
            "boundaries; 'animatic' also creates word-snapped ASL preview cuts."
        ),
    )
    edl.set_defaults(func=cmd_edl)

    render = sub.add_parser("render", help="Render an episode from its EDL and sourced assets")
    render.add_argument("timing_json", help="Path to projects/<slug>/narration/timing.json")
    render.add_argument(
        "--backend",
        choices=("resolve", "ffmpeg"),
        default="ffmpeg",
        help=(
            "Editing/render backend (default: ffmpeg until the local Resolve "
            "Console pilot passes). Choose resolve for the editable queue."
        ),
    )
    render.add_argument(
        "--out", help="Where the rendered episode is written (default: <project>/renders/episode.mp4)"
    )
    render.add_argument(
        "--dry-run", action="store_true", help="Report the render plan and exit without rendering"
    )
    render.add_argument(
        "--quality",
        choices=QUALITY_MODES,
        default="final",
        help=(
            "Source-quality policy (default: final). Use 'animatic' explicitly "
            "for a preview containing generated placeholders."
        ),
    )
    render.add_argument(
        "--start",
        help=(
            "Render a review segment beginning at seconds or MM:SS / HH:MM:SS. "
            "Requires --end; without --out writes under renders/segments/."
        ),
    )
    render.add_argument(
        "--end",
        help=(
            "Render a review segment ending at seconds or MM:SS / HH:MM:SS. "
            "Requires --start; the end is exclusive."
        ),
    )
    render.add_argument(
        "--no-audio-mix",
        action="store_true",
        help="Skip building the SFX/bed audio mix and mux the bare VO instead (faster drafts)",
    )
    render.add_argument(
        "--subtitles",
        dest="subtitles",
        action="store_true",
        default=True,
        help="Burn subtitles into the render (default: on)",
    )
    render.add_argument(
        "--no-subtitles",
        dest="subtitles",
        action="store_false",
        help="Skip building and burning subtitles",
    )
    render.add_argument(
        "--graphics",
        dest="graphics",
        action="store_true",
        default=True,
        help="Draw chapter cards and censor boxes into the render (default: on)",
    )
    render.add_argument(
        "--no-graphics",
        dest="graphics",
        action="store_false",
        help="Skip chapter cards and censor boxes",
    )
    render.add_argument(
        "--sound-library",
        dest="sound_library",
        help=f"Generated sound library to mix from (default: {DEFAULT_SOUND_LIBRARY} if it exists)",
    )
    render.add_argument(
        "--no-sound-library",
        dest="no_sound_library",
        action="store_true",
        help="Ignore the generated sound library and synthesize SFX/beds locally",
    )
    render.set_defaults(func=cmd_render)

    sound = sub.add_parser("sound", help="Generate the SFX cues and music beds the style pack names")
    sound.add_argument(
        "--library", help=f"Where the library is written (default: {DEFAULT_SOUND_LIBRARY})"
    )
    sound.add_argument(
        "--bed-variants",
        type=int,
        default=BED_VARIANTS,
        help=f"Distinct generations per bed kind (default: {BED_VARIANTS}). "
             f"More variants mean a long span repeats less audibly.",
    )
    sound.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be generated and roughly what it costs, then exit",
    )
    sound.set_defaults(func=cmd_sound)

    add_resolve_parser(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
