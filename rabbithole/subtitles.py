"""Burned subtitles: word timings -> readable cues -> an ASS document -> pixels.

Part 1 of this module (this section) groups word-level timing into subtitle
cues. A cue is a span of consecutive words shown on screen together. Grouping
follows a strict priority order -- never span a silence drop, then break at
sentence ends, then respect the size/duration caps -- because a subtitle that
bridges a deliberate mute (see `audiomix.py`'s `[SILENCE:]` handling) or a
sentence boundary reads as wrong even when it technically fits.
"""

from __future__ import annotations

from functools import lru_cache
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rabbithole.audiomix import SilenceWindow, silence_windows
from rabbithole.overlays import _find_keyword_word
from rabbithole.sources.plates import PLATE_BUFSIZE, PLATE_CRF, PLATE_MAXRATE
from rabbithole.validate import Finding
from rabbithole.encoding import video_args

MAX_CUE_CHARS = 42
MAX_CUE_WORDS = 7
MAX_CUE_SECONDS = 5.0
MIN_CUE_SECONDS = 0.8

# Devanagari danda/double-danda plus the usual Latin sentence enders. A word
# ending in one of these closes the cue it's in -- the next word always
# starts a new cue, even if it would otherwise still fit under the size caps.
_SENTENCE_END_CHARS = ("।", "॥", ".", "?", "!")


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    words: tuple[str, ...]
    keyword_positions: tuple[int, ...]  # indices within `words` to highlight


def keyword_word_indices(
    document: dict, romanized_words: list[str] | None = None
) -> set[int]:
    """Global word indices that a [KEY:] marker names.

    Reuses `overlays._find_keyword_word` (case-insensitive, punctuation-aware,
    window-bounded matching) rather than reimplementing that search here.
    When the narration spine is Devanagari, ``romanized_words`` supplies the
    canonical edition in which the marker arguments were authored.  Only the
    match is taken from that edition; the returned index still identifies the
    timed/displayed word in ``document``.
    """
    indices: set[int] = set()
    for marker in document.get("markers", []):
        if marker.get("kind") != "KEY":
            continue
        word = _find_keyword_word(marker, document, romanized_words)
        if word is not None:
            indices.add(word["index"])
    return indices


def _ends_sentence(word: str) -> bool:
    return bool(word) and word[-1] in _SENTENCE_END_CHARS


def _silence_between(prev_end: float, next_start: float, windows: list[SilenceWindow]) -> bool:
    """Does any silence window fall in the gap between two consecutive words?

    A window overlaps the (prev_end, next_start) gap -- using non-strict
    edges, since real data has windows whose boundaries land exactly on the
    surrounding words' start/end (see `audiomix.silence_windows`'s own
    docstring for the confirmed example).
    """
    return any(w.start < next_start and w.end > prev_end for w in windows)


def _raw_group(words: list[dict], windows: list[SilenceWindow]) -> list[list[dict]]:
    """Group words into cues, ignoring the minimum-duration extension.

    Priority order per group: never span a silence window, then break at a
    sentence end, then respect the char/word/seconds caps. The very first
    word of a new group is never held back by the caps -- otherwise an
    over-long single word would never be emitted at all.
    """
    groups: list[list[dict]] = []
    current: list[dict] = []

    def flush() -> None:
        if current:
            groups.append(list(current))
            current.clear()

    for word in words:
        if not current:
            current.append(word)
            continue

        prev = current[-1]

        if _silence_between(prev["end"], word["start"], windows):
            flush()
            current.append(word)
            continue

        if _ends_sentence(prev["word"]):
            flush()
            current.append(word)
            continue

        candidate = current + [word]
        joined = " ".join(w["word"] for w in candidate)
        candidate_duration = word["end"] - current[0]["start"]
        if (
            len(joined) > MAX_CUE_CHARS
            or len(candidate) > MAX_CUE_WORDS
            or candidate_duration > MAX_CUE_SECONDS
        ):
            flush()
            current.append(word)
            continue

        current.append(word)

    flush()
    return groups


def group_cues(
    document: dict, romanized_words: list[str] | None = None
) -> list[SubtitleCue]:
    """Group word timings into readable subtitle cues.

    See the module docstring and `_raw_group` for the grouping rules. After
    grouping, any cue shorter than `MIN_CUE_SECONDS` has its `end` extended,
    but never past the next cue's `start` or the document's
    `duration_seconds` -- so extension can never create an overlap.

    ``romanized_words`` is an optional matching spine for ``[KEY:]`` markers.
    Cue words and cue timing always come from ``document`` so a Devanagari
    narration remains Devanagari on screen.
    """
    words = document.get("words", [])
    if not words:
        return []

    duration = float(document.get("duration_seconds", 0.0))
    windows = silence_windows(document)
    keyword_indices = keyword_word_indices(document, romanized_words)

    groups = _raw_group(words, windows)

    cues: list[SubtitleCue] = []
    for index, group in enumerate(groups):
        start = group[0]["start"]
        end = group[-1]["end"]
        next_start = groups[index + 1][0]["start"] if index + 1 < len(groups) else duration
        if end - start < MIN_CUE_SECONDS:
            end = min(start + MIN_CUE_SECONDS, next_start, duration)
        keyword_positions = tuple(
            position for position, word in enumerate(group) if word["index"] in keyword_indices
        )
        cues.append(
            SubtitleCue(
                index=index,
                start=start,
                end=end,
                words=tuple(w["word"] for w in group),
                keyword_positions=keyword_positions,
            )
        )
    return cues


# =============================================================================
# Part 2: ASS generation
# =============================================================================

# In priority order: the first of these actually installed on the machine is
# used. `style/typography.json` names "Inter" for subtitle text, but Inter
# has no Devanagari coverage at all -- rendering Devanagari with it produces
# tofu boxes, and ffmpeg exits 0 while doing so (the failure is silent; see
# `pick_font`'s docstring). Confirmed empirically on this Windows machine
# (see `_installed_font_families`): "Nirmala UI" is installed via
# Nirmala.ttc, "Noto Sans Devanagari" and "Mangal" are not.
DEVANAGARI_FONT_CANDIDATES = (
    "Noto Sans Devanagari",
    "Nirmala UI",
    "Kohinoor Devanagari",
    "Devanagari Sangam MN",
    "Mangal",
)

# Subtitle geometry, as a fraction of the frame so it scales with any
# width/height `build_ass` is given rather than being pinned to 1920x1080.
_FONT_SIZE_FRACTION = 0.045
_MARGIN_V_FRACTION = 0.10  # vertical offset from the bottom edge
_MARGIN_SIDE_FRACTION = 0.08
_ASS_ALIGNMENT_BOTTOM_CENTER = 2  # numpad-style ASS \an alignment code
_BOLD_WEIGHT_THRESHOLD = 600  # typography.json weights >= this render bold

_DEFAULT_OUTLINE = 2.0
_DEFAULT_SHADOW_DISTANCE = 1.0
_DEFAULT_OUTLINE_COLOUR = "&H00000000"  # opaque black

_FONT_REGISTRY_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")

_SHADOW_RE = re.compile(
    r"(-?[\d.]+)px\s+(-?[\d.]+)px\s+(-?[\d.]+)px\s+"
    r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)"
)


def _windows_font_families() -> set[str]:
    """Read the Windows font registry when it is available."""

    try:
        import winreg
    except ImportError:
        return set()

    families: set[str] = set()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        )
    except OSError:
        return families

    with key:
        index = 0
        while True:
            try:
                name, _, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            stripped = _FONT_REGISTRY_SUFFIX_RE.sub("", name)
            for part in stripped.split(" & "):
                part = part.strip()
                if part:
                    families.add(part)
    return families


def _font_directories() -> tuple[Path, ...]:
    """Return native per-user/system font roots for the current host."""

    if sys.platform == "win32":
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return (windows / "Fonts",)
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
        )
    return (
        Path.home() / ".local" / "share" / "fonts",
        Path.home() / ".fonts",
        Path("/usr/local/share/fonts"),
        Path("/usr/share/fonts"),
    )


def _font_file_families(path: Path) -> set[str]:
    """Read family names from a TTF/OTF/TTC through Pillow's FreeType bridge."""

    try:
        from PIL import ImageFont
    except ImportError:
        return set()

    families: set[str] = set()
    # Collections may contain several families.  Ordinary TTF/OTF files stop
    # after index zero; a conservative cap prevents malformed files looping.
    for index in range(16):
        try:
            face = ImageFont.truetype(str(path), size=12, index=index)
        except (OSError, ValueError):
            break
        try:
            family, _style = face.getname()
        except Exception:
            family = ""
        if family:
            families.add(str(family).strip())
        if path.suffix.lower() != ".ttc":
            break
    return families


@lru_cache(maxsize=1)
def _installed_font_families() -> set[str]:
    """Discover font families on Windows, macOS, and Linux.

    Prefer the native Windows registry or Fontconfig.  macOS does not ship
    ``fc-list`` by default, so fall back to reading its standard font folders
    with Pillow.  The result is cached because a full system-font scan is
    stable for the duration of one render command.
    """

    families = _windows_font_families()
    fc_list = shutil.which("fc-list")
    if fc_list:
        result = subprocess.run(
            [fc_list, "--format=%{family}\n"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                for family in line.split(","):
                    cleaned = family.strip()
                    if cleaned:
                        families.add(cleaned)

    suffixes = {".ttf", ".otf", ".ttc"}
    for directory in _font_directories():
        if not directory.is_dir():
            continue
        try:
            files = directory.rglob("*")
            for path in files:
                if path.is_file() and path.suffix.lower() in suffixes:
                    families.update(_font_file_families(path))
        except OSError:
            continue
    return families


def pick_font(typography: dict, available: set[str] | None = None) -> tuple[str, list[Finding]]:
    """Choose a subtitle font that can render Devanagari.

    Returns the first of `DEVANAGARI_FONT_CANDIDATES` actually installed
    (via `available`, defaulting to `_installed_font_families()` for real
    detection). If the chosen font differs from `typography.json`'s
    requested `subtitle.family`, a `severity="warning"` Finding says so --
    the override is silent otherwise, and the failure mode it prevents
    (tofu boxes) is itself silent: ffmpeg exits 0 either way.

    If nothing on the candidate list is installed, falls back to
    'sans-serif' with a `severity="error"` Finding: a Latin/generic
    fallback still renders *something*, but Devanagari text will render as
    tofu boxes.
    """
    if available is None:
        available = _installed_font_families()

    requested = typography.get("subtitle", {}).get("family", "")
    findings: list[Finding] = []

    for candidate in DEVANAGARI_FONT_CANDIDATES:
        if candidate in available:
            if candidate != requested:
                findings.append(
                    Finding(
                        gate="subtitles",
                        severity="warning",
                        message=(
                            f"typography.json's subtitle family {requested!r} has no "
                            f"Devanagari coverage; overridden with {candidate!r}, the "
                            f"first Devanagari-capable font found installed on this "
                            f"machine."
                        ),
                    )
                )
            return candidate, findings

    findings.append(
        Finding(
            gate="subtitles",
            severity="error",
            message=(
                f"None of {', '.join(DEVANAGARI_FONT_CANDIDATES)} is installed on this "
                f"machine; falling back to 'sans-serif'. Devanagari subtitle text will "
                f"render as tofu boxes."
            ),
        )
    )
    return "sans-serif", findings


def ass_colour(hex_rgb: str) -> str:
    """#RRGGBB -> ASS colour &H00BBGGRR (ASS packs colour as alpha,B,G,R -- BGR, not RGB).

    Confirmed empirically against real ffmpeg/libass rendering, not just
    read off a spec: a [V4+ Styles] PrimaryColour of `&H0000FFFF` for
    `#FFFF00` (yellow) renders as yellow text in an extracted frame, and the
    identical value works the same way embedded in a Dialogue line's inline
    `\\c` override tag (with a trailing '&' appended there to close the hex
    literal, per ASS override-tag syntax -- see `_wrap_override`). Getting R
    and B swapped here would make every keyword highlight render blue
    instead of yellow, silently -- no other test would catch it unless the
    conversion itself is asserted on directly.
    """
    value = hex_rgb.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"ass_colour expects '#RRGGBB', got {hex_rgb!r}")
    r, g, b = value[0:2], value[2:4], value[4:6]
    return f"&H00{b}{g}{r}".upper()


def _wrap_override(hex_rgb: str) -> str:
    """`ass_colour`'s output, with the trailing '&' an inline \\c override tag needs."""
    return f"{ass_colour(hex_rgb)}&"


def _parse_shadow(shadow: str) -> tuple[float, float, str]:
    """CSS `box-shadow`-like string -> (outline_width, shadow_distance, ass_outline_colour).

    `typography.json`'s subtitle.shadow is a CSS box-shadow string (e.g.
    "0 2px 6px rgba(0,0,0,0.9)": x-offset, y-offset, blur, colour). ASS has
    no equivalent shadow model, only a border ("Outline") and a fixed-offset
    drop shadow ("Shadow") sharing one colour ("OutlineColour"), so this
    maps blur -> outline width (a soft blur reads, at ASS's border-only
    model, closest to a thicker outline) and y-offset -> shadow distance.
    The rgba colour's alpha becomes the ASS colour's alpha byte -- ASS alpha
    is transparency (00=opaque, FF=transparent), the inverse of CSS alpha
    (1=opaque), so it is inverted here.

    Malformed input falls back to a small, legible default rather than
    raising: a subtitle with a slightly-off shadow is fine, one that fails
    to render because typography.json's shadow string didn't parse is not.
    """
    match = _SHADOW_RE.search(shadow or "")
    if not match:
        return _DEFAULT_OUTLINE, _DEFAULT_SHADOW_DISTANCE, _DEFAULT_OUTLINE_COLOUR

    _offset_x, offset_y, blur, r, g, b, a = match.groups()
    outline = max(1.0, float(blur) / 3)
    shadow_distance = abs(float(offset_y))
    alpha_byte = round((1 - float(a)) * 255)
    colour = f"&H{alpha_byte:02X}{int(b):02X}{int(g):02X}{int(r):02X}"
    return outline, shadow_distance, colour


def format_ass_timestamp(seconds: float) -> str:
    """Seconds -> ASS `H:MM:SS.cc` timestamp (centiseconds, not milliseconds).

    Rounds to a single total-centisecond integer first (`round(seconds *
    100)`), then derives hours/minutes/seconds/centiseconds from that one
    value via integer division -- so a rounding carry (e.g. centiseconds
    rounding up to 100) always propagates correctly into seconds, minutes,
    and hours, rather than each field being rounded independently and
    risking an inconsistent result.

    Verified against two cases chosen to expose rounding: 1.005s, whose
    IEEE754 double value is actually 1.00499999999999989... (strictly below
    the halfway point), correctly rounds DOWN to "0:00:01.00"; 3661.5s (1h
    1m 1.5s, exactly representable in binary) rounds to "1:01:01.50",
    exercising the minute/hour carry.
    """
    total_centiseconds = round(seconds * 100)
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape_ass_text(word: str) -> str:
    """Escape ASS special characters that can appear in plain Dialogue text.

    `{` and `}` delimit an override block ANYWHERE in a Dialogue Text field,
    not just where this module intentionally emits one for keyword
    highlighting -- a narration word that happened to contain either
    character verbatim would silently open or close an override block,
    letting fragments of the surrounding line be misread as tags (this was
    confirmed directly: an unescaped `{\\c&H...&}` sitting inside a word was
    parsed as a real colour override, not literal text). There is no ASS
    escape sequence for a literal brace, so both are replaced with a
    visually similar fullwidth Unicode character rather than being dropped
    (dropping would silently swallow part of the word instead).

    A bare backslash is replaced too: `\\n`, `\\N`, and `\\h` are special
    even in plain, non-override text (soft break, hard break, hard space),
    so a word containing one of those sequences verbatim would be misread
    as a formatting directive rather than shown as text.
    """
    return word.replace("\\", "∖").replace("{", "｛").replace("}", "｝")


def _cue_text(cue: SubtitleCue, fill_hex: str, keyword_fill_hex: str) -> str:
    """A cue's Dialogue text, with each keyword word individually overridden.

    Every keyword word is wrapped in its own enter/reset tag pair rather
    than overriding once for the whole line, so multiple separate keyword
    words in one cue each highlight independently and every non-keyword
    word in between falls back to the style's own fill colour. Word text
    itself is escaped (`_escape_ass_text`) before being placed inside the
    line -- the override tags this function builds are the only '{'/'}'
    that should ever appear in the output.
    """
    keyword_positions = set(cue.keyword_positions)
    enter = "{\\c" + _wrap_override(keyword_fill_hex) + "}"
    reset = "{\\c" + _wrap_override(fill_hex) + "}"

    parts = []
    for position, word in enumerate(cue.words):
        safe_word = _escape_ass_text(word)
        if position in keyword_positions:
            parts.append(f"{enter}{safe_word}{reset}")
        else:
            parts.append(safe_word)
    return " ".join(parts)


def build_ass(cues: list[SubtitleCue], typography: dict, width: int = 1920, height: int = 1080) -> str:
    """A complete ASS subtitle document for `cues`, styled from `typography`.

    `[Script Info]` carries `PlayResX`/`PlayResY` matching `width`/`height`
    so cue geometry (font size, margins) means the same thing at whatever
    resolution the render actually runs at. `[V4+ Styles]` defines one
    `Default` style using the Devanagari-capable font `pick_font` chooses,
    `fill` as its primary colour, and `shadow` mapped via `_parse_shadow`.
    `[Events]` has exactly one `Dialogue` line per cue, in order.
    """
    subtitle_style = typography.get("subtitle", {})
    font, _findings = pick_font(typography)
    fill_hex = subtitle_style.get("fill", "#FFFFFF")
    keyword_fill_hex = subtitle_style.get("keyword_fill", "#FFFF00")
    weight = subtitle_style.get("weight", 400)
    outline, shadow_distance, outline_colour = _parse_shadow(subtitle_style.get("shadow", ""))

    fontsize = max(1, round(height * _FONT_SIZE_FRACTION))
    margin_v = max(0, round(height * _MARGIN_V_FRACTION))
    margin_side = max(0, round(width * _MARGIN_SIDE_FRACTION))
    bold = -1 if weight >= _BOLD_WEIGHT_THRESHOLD else 0
    primary_colour = ass_colour(fill_hex)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font},{fontsize},{primary_colour},{primary_colour},"
            f"{outline_colour},&H00000000,{bold},0,0,0,100,100,0,0,1,"
            f"{outline:.1f},{shadow_distance:.1f},{_ASS_ALIGNMENT_BOTTOM_CENTER},"
            f"{margin_side},{margin_side},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in cues:
        start_ts = format_ass_timestamp(cue.start)
        end_ts = format_ass_timestamp(cue.end)
        text = _cue_text(cue, fill_hex, keyword_fill_hex)
        lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text}")

    return "\n".join(lines) + "\n"


def write_ass(
    cues: list[SubtitleCue], typography: dict, out_path: Path, width: int = 1920, height: int = 1080
) -> tuple[Path, list[Finding]]:
    """Write `build_ass`'s output to `out_path` as UTF-8, alongside `pick_font`'s findings.

    `build_ass` itself only returns the document text (matching its given
    signature), so the font-selection findings that `pick_font` produces
    (the Devanagari-override warning, or the no-candidate-found error) are
    collected here, via a second `pick_font` call -- cheap and
    deterministic, not a real duplicate work, since it's a font
    lookup, not an ffmpeg run.
    """
    out_path = Path(out_path)
    _font, findings = pick_font(typography)
    content = build_ass(cues, typography, width=width, height=height)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return out_path, findings


# =============================================================================
# Part 3: burn-in
# =============================================================================


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, cwd=cwd)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:4])} ...\n{detail}")
    return result


def burn(video_path: Path, ass_path: Path, out_path: Path) -> Path:
    """Burn subtitles into a video, hard-coded.

    The `ass=` filter has the exact same problem `sources/plates.py`'s
    `movie=` filter already hit: ffmpeg's filtergraph option parser treats
    `:` as an option separator, which collides with a Windows drive letter
    (`C:\\...`) in an absolute path. Confirmed empirically (both directions)
    before writing this function: escaping the colon
    (`C\\:/Users/.../subs.ass`) still fails -- libass reports "Unable to
    parse 'original_size' option value" because downstream option parsing
    still trips on it -- while running ffmpeg with `cwd` set to the ASS
    file's own directory and passing just its bare filename (`ass=subs.ass`)
    works cleanly. That's the approach used here, same as `movie=` before
    it. Only the ASS path needs this: `-i`/output-filename arguments are
    plain CLI args, not filtergraph option values, so `video_path` and
    `out_path` are passed as ordinary absolute paths.

    Applies the same bitrate cap `render.finish` uses for its own encode
    (`sources/plates.PLATE_CRF`/`PLATE_MAXRATE`/`PLATE_BUFSIZE`). The audio
    stream is copied (`-c:a copy`), not re-encoded, so a mix already placed
    by `audiomix.build_mix` is untouched.
    """
    video_path = Path(video_path).resolve()
    ass_path = Path(ass_path).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vf", f"ass={ass_path.name}",
            *video_args(PLATE_CRF, maxrate=PLATE_MAXRATE, bufsize=PLATE_BUFSIZE),
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            str(out_path),
        ],
        cwd=ass_path.parent,
    )
    return out_path
