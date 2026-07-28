"""Capturing web pages and documents as footage.

Why this exists
---------------
In one production audit, 49 slots -- 29 `screenshot` and 20 `capture` -- could not
be served by anything else this package does. `screenshot` was always a declared
gap. `capture` turned out to be a *mechanism* mismatch rather than a sourcing
failure: `ytdlp.fetch_primary` retrieves video from video hosts, but many
capture slots instead want documents -- a cancellation notice, court order,
official communique, or police advisory posted as text. In one audit, 12 of 30
catalogued artifacts were documents and three
of the X posts carry no video at all. The material exists, is cited, and simply
is not a clip.

Both groups need the same capability: render a page or a document to a frame.

Mechanism: the browser already on the machine
---------------------------------------------
Chrome and Edge both ship a headless screenshot mode, and poppler's `pdftoppm`
renders a PDF page to an image. Both are already present here, so this adds no
dependency -- in the same spirit as `graphics.py` refusing a Node/Remotion
runtime to draw title cards. Playwright would be the obvious library choice and
is deliberately not used: it would pull a second browser download and a Python
package to do what `--headless --screenshot` already does.

Every external call goes through an injected `Runner`/`Transport`, so no test
here launches a browser or touches the network.

Grading is deliberately NOT applied
-----------------------------------
`render.finish` grades the whole assembled timeline (grain, scanlines, vignette,
LUT). Plates pre-grade because they are synthesized flat colour and would
otherwise have no texture at all; real footage does not, and a capture is
standing in for real footage. Pre-grading here would double-apply and, on a
court order or a press notice, crush the very text the shot exists to show.

Archived versus live
--------------------
A documentary claiming what a page said on a date should capture the *archived*
snapshot, not today's version -- the live page can have changed, and often has.
`is_archived_url` recognises the archive hosts, and `capture_page` reports a
warning when asked to capture a live URL, rather than silently producing a frame
that may no longer show what the script says it shows.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from rabbithole.encoding import video_args

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, bytes, bytes]]
# url -> (status, body)
Transport = Callable[[str], tuple[int, bytes]]

CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080

# Headless Chrome renders before webfonts and lazy images have settled; without a
# delay a capture is routinely a blank shell of the page it is supposed to show.
DEFAULT_SETTLE_MS = 2500

# Several public-sector sites return a deliberately empty/error response to
# Chrome's literal ``HeadlessChrome`` user agent even though the same document
# is available to an ordinary browser and to the HTTP source probe.  A normal
# desktop UA keeps the visual capture and the source-text QA on the same public
# page; it does not bypass authentication, cookies, or a paywall.
_CAPTURE_PLATFORM_TOKEN = (
    "Macintosh; Intel Mac OS X 10_15_7"
    if sys.platform == "darwin"
    else (
        "X11; Linux x86_64"
        if sys.platform.startswith("linux")
        else "Windows NT 10.0; Win64; x64"
    )
)
CAPTURE_USER_AGENT = (
    f"Mozilla/5.0 ({_CAPTURE_PLATFORM_TOKEN}) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# Common native browser locations on Windows and macOS.  Nonexistent paths are
# harmless; the explicit environment override and PATH lookup run as well.
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "~/Applications/Chromium.app/Contents/MacOS/Chromium",
)
BROWSER_PATH_ENV = "RABBITHOLE_BROWSER_PATH"

ARCHIVE_HOSTS = ("web.archive.org", "archive.org/web", "archive.ph", "archive.today",
                 "perma.cc", "webcitation.org")

_PDF_MAGIC = b"%PDF"


# Hard ceiling on any single external call.
#
# Headless Chrome does not reliably exit. `--virtual-time-budget` bounds how much
# page time it simulates, not how long the process lives, and a page that never
# finishes loading leaves the browser running forever. Without a timeout that
# hangs the whole asset run: observed here as a capture pass that produced two
# files and then sat for ten minutes with orphaned browser processes still
# alive. A per-slot failure is recoverable; a wedged run is not.
CAPTURE_TIMEOUT_SECONDS = 90


def _default_runner(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=CAPTURE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the child on timeout, so this reports rather than
        # leaks -- the browser process is already gone by the time we get here.
        return 124, b"", (
            f"timed out after {CAPTURE_TIMEOUT_SECONDS}s: {' '.join(argv[:2])}"
        ).encode()
    return result.returncode, result.stdout, result.stderr


def _default_transport(url: str) -> tuple[int, bytes]:
    import requests

    response = requests.get(
        url, timeout=60,
        headers={"User-Agent": "rabbithole/1.0 (Crowley-format documentary pipeline)"},
    )
    return response.status_code, response.content


def find_browser(candidates: tuple[str, ...] | None = None) -> Path | None:
    """The first installed browser that can take a headless screenshot."""
    selected = list(candidates if candidates is not None else BROWSER_CANDIDATES)
    override = os.environ.get(BROWSER_PATH_ENV)
    if override:
        selected.insert(0, override)
    for candidate in selected:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    found = next(
        (
            value
            for name in (
                "google-chrome",
                "google-chrome-stable",
                "chrome",
                "msedge",
                "microsoft-edge",
                "chromium",
                "chromium-browser",
            )
            if (value := shutil.which(name))
        ),
        None,
    )
    return Path(found) if found else None


def is_archived_url(url: str) -> bool:
    """Whether this URL points at an archive snapshot rather than a live page."""
    lowered = (url or "").lower()
    return any(host in lowered for host in ARCHIVE_HOSTS)


def is_pdf(url: str, body: bytes | None = None) -> bool:
    """Whether this is a PDF, by magic bytes if available and extension if not.

    Magic bytes first: content type is what matters and a `.aspx` endpoint can
    perfectly well return a PDF, which is exactly what happened with two
    government URLs in this project.
    """
    if body is not None and body[:4] == _PDF_MAGIC:
        return True
    if body is not None:
        return False
    return (url or "").lower().split("?")[0].endswith(".pdf")


# A capture below both of these is not showing anything: a login wall, an error
# page, or a render that never painted. Measured over the 16 real captures in
# this project, genuine content sat at 24-124 standard deviation and 123-256
# distinct grey levels, while five blanks sat at 5-14 and 22-57 -- a wide gap,
# so the thresholds do not have to be finely tuned.
BLANK_STDDEV_MAX = 15.0
BLANK_DISTINCT_MAX = 60


@dataclass(frozen=True)
class CaptureInspection:
    """Objective readability measurements retained with a capture result."""

    grey_stddev: float
    distinct_grey_levels: int

    @property
    def looks_blank(self) -> bool:
        return bool(
            self.grey_stddev < BLANK_STDDEV_MAX
            or self.distinct_grey_levels < BLANK_DISTINCT_MAX
        )


def inspect_frame(png: Path) -> CaptureInspection:
    """Measure whether a rendered frame contains enough variation to be read."""
    from PIL import Image

    import numpy as np

    try:
        grey = np.asarray(Image.open(png).convert("L"))
    except Exception as exc:
        raise RuntimeError(f"Captured frame {png} is not a readable image: {exc}") from exc
    return CaptureInspection(
        grey_stddev=float(grey.std()),
        distinct_grey_levels=int(len(np.unique(grey))),
    )


def looks_blank(png: Path) -> bool:
    """Whether a captured frame is empty enough to be worthless as a shot.

    The file existing proves the tool ran, not that the page rendered. Five of
    this project's first sixteen captures were blank -- X login walls and pages
    that never painted -- and every one of them would have gone into the
    timeline as a valid asset.
    """
    return inspect_frame(png).looks_blank


def _page_text(value: str | bytes | None) -> tuple[str, str]:
    """Return ``(raw, visible)`` text for obstruction checks."""
    if value is None:
        return "", ""
    raw = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    visible = re.sub(
        r"<(?:script|style|noscript|svg)\b[^>]*>.*?</(?:script|style|noscript|svg)>",
        " ",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = html.unescape(visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    return raw, visible


_CONTENT_BLOCKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "CAPTCHA or human-verification challenge",
        re.compile(
            r"\b(?:verify (?:that )?you are human|i(?:'| a)m not a robot|"
            r"complete (?:the )?captcha|solve (?:the )?captcha|"
            r"human verification)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Cloudflare/browser challenge",
        re.compile(
            r"\b(?:just a moment|checking your browser|cloudflare ray id|"
            r"enable javascript and cookies to continue|attention required)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "HTTP or browser error page",
        re.compile(
            r"\b(?:404 (?:error|not found)|403 forbidden|access denied|"
            r"page not found|internal server error|bad gateway|"
            r"service unavailable|temporarily unavailable|"
            r"this site can(?:not|'t) be reached|this content (?:isn't|is not) available)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "obstructive modal or interstitial",
        re.compile(
            r"\b(?:save our backup channels|sign in to continue|log in to continue|"
            r"subscribe to continue|disable your ad blocker|"
            r"accept all cookies to continue)\b",
            re.IGNORECASE,
        ),
    ),
)


def obstruction_reason(page_source: str | bytes | None) -> str:
    """Describe a challenge, error, or blocking interstitial in page content."""
    raw, visible = _page_text(page_source)
    if not raw:
        return ""

    # Cloudflare's machine marker remains useful even if its visible copy
    # changes. It is intentionally narrower than matching the word
    # "Cloudflare", which ordinary sites can mention in their footers.
    if re.search(r"(?:cf-chl-|__cf_chl_)", raw, flags=re.IGNORECASE):
        return "Cloudflare/browser challenge (challenge marker present)"

    for label, pattern in _CONTENT_BLOCKERS:
        match = pattern.search(visible)
        if match:
            return f"{label} ({match.group(0)!r})"

    # A semantic modal alone is too broad: many sites keep hidden dialogs in
    # their DOM. Pair it with blocking copy so only an active-looking
    # interstitial is rejected.
    # Inspect the modal's own nearby copy, not the whole page. Otherwise a
    # hidden dialog in site boilerplate plus an unrelated word "continue" in
    # the article could reject a perfectly readable capture.
    for semantic_modal in re.finditer(
        r"<[^>]+(?:aria-modal\s*=\s*[\"']?true|"
        r"role\s*=\s*[\"']dialog[\"'])[^>]*>",
        raw,
        flags=re.IGNORECASE,
    ):
        modal_window = raw[semantic_modal.start(): semantic_modal.end() + 2000]
        _modal_raw, modal_visible = _page_text(modal_window)
        blocking_copy = re.search(
            r"\b(?:continue|close to view|dismiss|subscribe|sign in|log in|cookies?)\b",
            modal_visible,
            flags=re.IGNORECASE,
        )
        if blocking_copy:
            return f"obstructive modal or interstitial ({blocking_copy.group(0)!r})"
    return ""


@dataclass(frozen=True)
class CaptureResult:
    path: Path
    kind: str  # "page" or "document"
    warnings: tuple[str, ...] = ()
    inspection: CaptureInspection | None = None


def capture_page(
    url: str,
    out_png: Path,
    *,
    width: int = CAPTURE_WIDTH,
    height: int = CAPTURE_HEIGHT,
    settle_ms: int = DEFAULT_SETTLE_MS,
    runner: Runner | None = None,
    browser: Path | None = None,
    check_blank: bool = True,
    page_source: str | bytes | None = None,
    check_obstructions: bool = True,
    require_content_text: bool = False,
) -> CaptureResult:
    """Screenshot a web page with the installed browser, headless."""
    # Absolute, always. The browser is a separate process with its own working
    # directory, so a relative path is resolved against *its* cwd rather than
    # ours. Passing `projects/<slug>/assets/.capturework/x.png` produced
    # "Failed to write file ...: The system cannot find the path specified"
    # from Chrome itself, on every slot, while the identical call with an
    # absolute path succeeded -- which is why it looked like a URL problem.
    out_png = Path(out_png).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)

    chosen = browser or find_browser()
    if chosen is None:
        raise RuntimeError(
            "No headless-capable browser found. Install Chrome or Edge, or pass "
            "`browser=` explicitly."
        )

    warnings: list[str] = []
    if not is_archived_url(url):
        warnings.append(
            f"{url} is a live page, not an archive snapshot. A documentary claiming "
            f"what a page said on a date should capture the archived copy -- the live "
            f"page may have changed since."
        )

    argv = [
        str(chosen),
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--user-agent={CAPTURE_USER_AGENT}",
        f"--virtual-time-budget={settle_ms}",
        f"--window-size={width},{height}",
        f"--screenshot={out_png}",
        url,
    ]
    active = runner or _default_runner
    returncode, browser_stdout, stderr = active(argv)

    # Chrome's headless screenshot exits 0 in situations where it writes nothing,
    # so the file's existence is the real success test, not the exit code.
    if not out_png.exists() or out_png.stat().st_size == 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:] if isinstance(stderr, bytes) else str(stderr)[-500:]
        raise RuntimeError(
            f"Browser wrote no screenshot for {url!r} (exit {returncode}): {tail}"
        )

    # A browser screenshot alone cannot tell us that the colourful pixels are
    # a CAPTCHA, Cloudflare challenge, error page, or full-screen modal. The
    # HTTP probe supplies source HTML in the normal asset path. With the real
    # browser, also inspect rendered DOM so JavaScript-created interstitials
    # are visible to QA; injected test runners remain one-call and deterministic.
    sources: list[str | bytes] = []
    if page_source:
        sources.append(page_source)
    if browser_stdout:
        sources.append(browser_stdout)
    if runner is None and check_obstructions:
        dom_argv = [
            str(chosen),
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-agent={CAPTURE_USER_AGENT}",
            f"--virtual-time-budget={settle_ms}",
            "--dump-dom",
            url,
        ]
        dom_code, dom_stdout, _dom_stderr = active(dom_argv)
        if dom_code == 0 and dom_stdout:
            sources.append(dom_stdout)

    combined_source: str | bytes | None
    if any(isinstance(source, bytes) for source in sources):
        combined_source = b"\n".join(
            source if isinstance(source, bytes) else source.encode("utf-8")
            for source in sources
        )
    else:
        combined_source = "\n".join(str(source) for source in sources) if sources else None

    if check_obstructions:
        reason = obstruction_reason(combined_source)
        if reason:
            out_png.unlink(missing_ok=True)
            raise RuntimeError(
                f"Captured {url!r} but content QA found a {reason}. Not recorded; "
                "capture an unobstructed archived source instead."
            )
        if require_content_text and not combined_source:
            out_png.unlink(missing_ok=True)
            raise RuntimeError(
                f"Captured {url!r} but final-quality content QA could not inspect "
                "page text or rendered DOM. Not recorded because a screenshot "
                "cannot prove it is free of challenge and error pages."
            )

    inspection = inspect_frame(out_png)
    if check_blank and inspection.looks_blank:
        out_png.unlink(missing_ok=True)
        raise RuntimeError(
            f"Captured {url!r} but the frame is blank -- most likely a login wall, "
            f"an error page, or a render that never painted. Not recorded; a blank "
            f"frame in the timeline is worse than a known gap "
            f"(grey stddev {inspection.grey_stddev:.2f}, "
            f"{inspection.distinct_grey_levels} distinct levels)."
        )

    return CaptureResult(
        path=out_png, kind="page", warnings=tuple(warnings), inspection=inspection
    )


def capture_document(
    source: str | Path,
    out_png: Path,
    *,
    page: int = 1,
    runner: Runner | None = None,
    transport: Transport | None = None,
    work_dir: Path | None = None,
    check_blank: bool = True,
) -> CaptureResult:
    """Render one page of a PDF to an image.

    `source` may be a local path or a URL; a URL is fetched first, because
    `pdftoppm` reads files rather than URLs.
    """
    out_png = Path(out_png).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).resolve() if work_dir else out_png.parent
    work.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    local = Path(source)
    if not local.exists():
        status, body = (transport or _default_transport)(str(source))
        if status != 200:
            raise RuntimeError(f"Fetching {source!r} failed: HTTP {status}")
        if not is_pdf(str(source), body):
            raise RuntimeError(
                f"{source!r} did not return a PDF (first bytes {body[:8]!r}); "
                f"capture it as a page instead."
            )
        local = work / f"{out_png.stem}.pdf"
        local.write_bytes(body)
        if not is_archived_url(str(source)):
            warnings.append(
                f"{source} is a live document URL, not an archive snapshot; it may "
                f"change or disappear."
            )

    # pdftoppm appends "-<page>.png" to the prefix it is given, so it is pointed
    # at a stem and the result is moved into place.
    prefix = work / f"{out_png.stem}-render"
    argv = [
        "pdftoppm", "-png", "-r", "150",
        "-f", str(page), "-l", str(page),
        str(local), str(prefix),
    ]
    active = runner or _default_runner
    returncode, _stdout, stderr = active(argv)
    if returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:] if isinstance(stderr, bytes) else str(stderr)[-500:]
        raise RuntimeError(f"pdftoppm failed for {local} (exit {returncode}): {tail}")

    produced = sorted(work.glob(f"{prefix.name}*.png"))
    if not produced:
        raise RuntimeError(f"pdftoppm wrote no image for {local} page {page}")
    if out_png.exists():
        out_png.unlink()
    produced[0].replace(out_png)
    for leftover in produced[1:]:
        leftover.unlink(missing_ok=True)

    inspection = inspect_frame(out_png)
    if check_blank and inspection.looks_blank:
        out_png.unlink(missing_ok=True)
        raise RuntimeError(
            f"Rendered {local} page {page}, but the document frame is blank or "
            f"unreadable (grey stddev {inspection.grey_stddev:.2f}, "
            f"{inspection.distinct_grey_levels} distinct levels). Not recorded."
        )

    return CaptureResult(
        path=out_png, kind="document", warnings=tuple(warnings), inspection=inspection
    )


def still_to_video(
    still: Path,
    out_path: Path,
    duration: float,
    *,
    width: int = CAPTURE_WIDTH,
    height: int = CAPTURE_HEIGHT,
    fps: int = 30,
    runner: Runner | None = None,
) -> Path:
    """Turn a captured still into a slot-length video.

    Letterboxed onto the target frame rather than cropped to fill it: a document
    or a screenshot is information, and cropping a court order to 16:9 cuts off
    the part the shot exists to show. `render.cut_segment` applies the framing
    move afterwards, so this deliberately holds still.
    """
    still = Path(still).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if duration <= 0:
        raise ValueError(f"still_to_video duration must be positive, got {duration}")

    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,format=yuv420p"
    )
    argv = [
        "ffmpeg", "-y", "-v", "error",
        "-loop", "1", "-i", str(still),
        "-t", f"{duration:.6f}",
        "-r", str(fps),
        "-vf", vf,
        *video_args(20),
        str(out_path),
    ]
    active = runner or _default_runner
    returncode, _stdout, stderr = active(argv)
    if returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-600:] if isinstance(stderr, bytes) else str(stderr)[-600:]
        raise RuntimeError(f"ffmpeg failed turning {still.name} into video: {tail}")
    return out_path


def capture_to_video(
    url: str,
    out_path: Path,
    duration: float,
    work_dir: Path,
    *,
    width: int = CAPTURE_WIDTH,
    height: int = CAPTURE_HEIGHT,
    fps: int = 30,
    runner: Runner | None = None,
    transport: Transport | None = None,
    browser: Path | None = None,
    check_blank: bool = True,
    quality: str = "animatic",
) -> CaptureResult:
    """Capture `url` -- page or document -- as a slot-length video.

    Dispatches on what the URL actually returns rather than on its extension: a
    `.aspx` endpoint serving a PDF is exactly the case that produced two
    `%PDF`-headed files named `.mp4` earlier in this project.
    """
    work_dir = Path(work_dir).resolve()
    quality = (quality or "").strip().lower()
    if quality not in ("final", "animatic"):
        raise ValueError(
            f"Unknown capture quality mode {quality!r}; expected final or animatic"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path).resolve()
    still = work_dir / f"{out_path.stem}.png"

    # Dispatch on what the URL actually returns, not on how it is spelled. A
    # court-order endpoint like `/app/downloadOrderbByDate/W.P.(C)/9639/2026/...`
    # serves `%PDF-1` with no `.pdf` anywhere in it; extension-only routing sent
    # it to the browser, which rendered a blank frame. One GET settles it, and
    # the body is handed straight to the document path so it is not fetched twice.
    body: bytes | None = None
    if not is_pdf(url):
        try:
            status, fetched = (transport or _default_transport)(url)
            if status == 200:
                body = fetched
        except Exception:
            # A probe that fails is not fatal -- fall through to the browser,
            # which has its own error handling and may well succeed.
            body = None

    if is_pdf(url) or (body is not None and is_pdf(url, body)):
        if body is not None:
            (work_dir / f"{out_path.stem}.pdf").write_bytes(body)
            source: str | Path = work_dir / f"{out_path.stem}.pdf"
        else:
            source = url
        result = capture_document(
            source, still, runner=runner, transport=transport, work_dir=work_dir,
            check_blank=check_blank,
        )
        if not is_archived_url(url):
            result = CaptureResult(
                path=result.path, kind=result.kind,
                warnings=result.warnings + (
                    f"{url} is a live document URL, not an archive snapshot; it may "
                    f"change or disappear.",
                ),
                inspection=result.inspection,
            )
    else:
        result = capture_page(
            url, still, width=width, height=height,
            runner=runner, browser=browser, check_blank=check_blank,
            page_source=body,
            require_content_text=(quality == "final"),
        )

    still_to_video(
        result.path, out_path, duration,
        width=width, height=height, fps=fps, runner=runner,
    )
    return CaptureResult(
        path=out_path,
        kind=result.kind,
        warnings=result.warnings,
        inspection=result.inspection,
    )
