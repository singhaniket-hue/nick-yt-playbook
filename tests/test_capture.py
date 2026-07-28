"""Page and document capture.

Every external call is injected, so nothing here launches a browser or reaches
the network. The ffmpeg step is real, because a still that silently fails to
become a video is exactly the failure mode this package keeps finding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rabbithole.sources.capture import (
    ARCHIVE_HOSTS,
    CaptureResult,
    capture_document,
    capture_page,
    capture_to_video,
    find_browser,
    is_archived_url,
    is_pdf,
    still_to_video,
)

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 200


def _png(path: Path, size=(320, 180), colour=(200, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return path


def _busy_png(path: Path, size=(320, 180)) -> Path:
    """A frame with real variation, so the blank-capture check passes it.

    A solid colour is exactly what `looks_blank` exists to reject, so a fake
    that writes one is testing the wrong thing.
    """
    import numpy as np
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    Image.fromarray((rng.random((size[1], size[0], 3)) * 255).astype("uint8")).save(path)
    return path


def _writing_runner(target: Path, payload: bytes = b"x"):
    """A runner that behaves like the tool it stands in for: it writes a file."""

    def runner(argv):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return 0, b"", b""

    return runner


def _busy_runner(target: Path):
    """Like _writing_runner, but the file it writes is a real, non-blank frame."""

    def runner(argv):
        _busy_png(target)
        return 0, b"", b""

    return runner


# --- URL classification ----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://web.archive.org/web/20260517114417/https://example.com",
    "https://archive.ph/abc12",
    "https://perma.cc/L4BE-U9BL",
])
def test_archive_snapshots_are_recognised(url):
    assert is_archived_url(url) is True


@pytest.mark.parametrize("url", [
    "https://x.com/example/status/1234567890",
    "https://example.com",
    "",
])
def test_live_urls_are_not_archives(url):
    assert is_archived_url(url) is False


def test_every_archive_host_is_matched_by_its_own_constant():
    for host in ARCHIVE_HOSTS:
        assert is_archived_url(f"https://{host}/something")


def test_pdf_detected_by_magic_bytes_over_extension():
    """The case that produced two `%PDF`-headed files named `.mp4`: a government
    `.aspx` endpoint that serves a PDF."""
    assert is_pdf("https://pib.gov.in/PressReleasePage.aspx?PRID=1", PDF_BYTES) is True


def test_non_pdf_body_beats_a_pdf_extension():
    assert is_pdf("https://example.com/thing.pdf", b"<!DOCTYPE html>") is False


def test_pdf_extension_used_when_no_body_available():
    assert is_pdf("https://example.com/notice.pdf") is True
    assert is_pdf("https://example.com/page") is False


def test_find_browser_returns_none_when_nothing_is_installed(tmp_path):
    assert find_browser(candidates=(str(tmp_path / "nope.exe"),)) is None


def test_find_browser_picks_the_first_installed_candidate(tmp_path):
    second = tmp_path / "edge.exe"
    second.write_bytes(b"")
    found = find_browser(candidates=(str(tmp_path / "chrome.exe"), str(second)))
    assert found == second


# --- capture_page ----------------------------------------------------------------


def test_capture_page_warns_when_asked_to_capture_a_live_page(tmp_path):
    """A documentary claiming what a page said on a date needs the archived copy;
    the live page may have changed since."""
    out = tmp_path / "shot.png"
    result = capture_page(
        "https://example.com", out,
        runner=_busy_runner(out), browser=Path("browser.exe"),
    )
    assert any("archive snapshot" in w for w in result.warnings)


def test_capture_page_does_not_warn_for_an_archive_snapshot(tmp_path):
    out = tmp_path / "shot.png"
    result = capture_page(
        "https://web.archive.org/web/2026/https://x", out,
        runner=_busy_runner(out), browser=Path("browser.exe"),
    )
    assert result.warnings == ()


def test_capture_page_raises_when_the_browser_writes_nothing(tmp_path):
    """Headless Chrome exits 0 in cases where it produces no file, so the exit
    code alone cannot be the success test."""
    def silent_runner(argv):
        return 0, b"", b"some warning"

    with pytest.raises(RuntimeError, match="wrote no screenshot"):
        capture_page("https://example.com", tmp_path / "shot.png",
                     runner=silent_runner, browser=Path("browser.exe"))


def test_capture_page_raises_when_no_browser_is_installed(tmp_path, monkeypatch):
    """Passing browser=None is not enough to test this: on a machine that has
    Chrome, find_browser() succeeds and the run gets further. The absence has to
    be simulated."""
    import rabbithole.sources.capture as capture_module

    monkeypatch.setattr(capture_module, "find_browser", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="No headless-capable browser"):
        capture_page("https://example.com", tmp_path / "shot.png",
                     runner=lambda argv: (0, b"", b""), browser=None)


def test_capture_page_passes_the_url_and_output_to_the_browser(tmp_path):
    out = tmp_path / "shot.png"
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        _busy_png(out)
        return 0, b"", b""

    capture_page("https://example.com/x", out, runner=runner, browser=Path("b.exe"))

    assert "https://example.com/x" in seen["argv"]
    assert any(a.startswith("--screenshot=") for a in seen["argv"])
    assert "--headless" in seen["argv"]


# --- capture_document ------------------------------------------------------------


def test_capture_document_fetches_then_renders_a_pdf_url(tmp_path):
    out = tmp_path / "doc.png"
    rendered = tmp_path / "doc-render-1.png"

    def runner(argv):
        assert argv[0] == "pdftoppm"
        _busy_png(rendered)
        return 0, b"", b""

    result = capture_document(
        "https://gov.example/notice.pdf", out,
        runner=runner, transport=lambda url: (200, PDF_BYTES), work_dir=tmp_path,
    )

    assert result.kind == "document"
    assert out.exists()


def test_capture_document_refuses_a_url_that_is_not_a_pdf(tmp_path):
    with pytest.raises(RuntimeError, match="did not return a PDF"):
        capture_document(
            "https://gov.example/page", tmp_path / "doc.png",
            runner=lambda argv: (0, b"", b""),
            transport=lambda url: (200, b"<!DOCTYPE html>"), work_dir=tmp_path,
        )


def test_capture_document_reports_a_failed_fetch(tmp_path):
    with pytest.raises(RuntimeError, match="HTTP 404"):
        capture_document(
            "https://gov.example/gone.pdf", tmp_path / "doc.png",
            runner=lambda argv: (0, b"", b""),
            transport=lambda url: (404, b""), work_dir=tmp_path,
        )


def test_capture_document_raises_when_pdftoppm_produces_nothing(tmp_path):
    with pytest.raises(RuntimeError, match="wrote no image"):
        capture_document(
            "https://gov.example/notice.pdf", tmp_path / "doc.png",
            runner=lambda argv: (0, b"", b""),
            transport=lambda url: (200, PDF_BYTES), work_dir=tmp_path,
        )


# --- still_to_video (real ffmpeg) ------------------------------------------------


def test_still_becomes_a_video_of_the_requested_length(tmp_path):
    from rabbithole.sources.soundgen import probe_duration

    still = _png(tmp_path / "s.png")
    out = still_to_video(still, tmp_path / "v.mp4", 2.0, width=320, height=180, fps=12)
    assert probe_duration(out) == pytest.approx(2.0, abs=0.15)


def test_the_video_carries_a_decodable_video_stream(tmp_path):
    still = _png(tmp_path / "s.png")
    out = still_to_video(still, tmp_path / "v.mp4", 1.0, width=320, height=180, fps=12)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
        capture_output=True,
    )
    assert b"h264" in probe.stdout


def test_a_tall_document_is_letterboxed_not_cropped(tmp_path):
    """Cropping a court order to 16:9 cuts off the part the shot exists to show,
    so the frame is padded instead. Black bars top and bottom prove it."""
    import numpy as np
    from PIL import Image

    still = _png(tmp_path / "tall.png", size=(400, 1200), colour=(255, 0, 0))
    out = still_to_video(still, tmp_path / "v.mp4", 1.0, width=320, height=180, fps=12)

    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(out),
                    "-frames:v", "1", str(frame)], check=True)
    arr = np.asarray(Image.open(frame).convert("RGB"))

    # The source is pure red; padding is black. A crop-to-fill would leave no
    # black at all.
    assert (arr.sum(axis=2) == 0).any(), "no padding present -- the still was cropped"
    assert (arr[:, :, 0] > 200).any(), "the document itself is missing"


def test_still_to_video_rejects_a_nonpositive_duration(tmp_path):
    still = _png(tmp_path / "s.png")
    with pytest.raises(ValueError, match="positive"):
        still_to_video(still, tmp_path / "v.mp4", 0.0)


# --- capture_to_video dispatch ---------------------------------------------------


def test_capture_to_video_routes_a_pdf_url_to_the_document_path(tmp_path):
    calls = []

    def runner(argv):
        calls.append(argv[0])
        if argv[0] == "pdftoppm":
            _busy_png(tmp_path / "work" / "out-render-1.png")
        elif argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        return 0, b"", b""

    result = capture_to_video(
        "https://gov.example/notice.pdf", tmp_path / "out.mp4", 1.0, tmp_path / "work",
        width=320, height=180, fps=12,
        runner=runner, transport=lambda url: (200, PDF_BYTES),
    )
    assert result.kind == "document"
    assert "pdftoppm" in calls


def test_capture_to_video_routes_a_page_url_to_the_browser_path(tmp_path):
    calls = []

    def runner(argv):
        calls.append(Path(argv[0]).name)
        if argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        else:
            _busy_png(tmp_path / "work" / "out.png")
        return 0, b"", b""

    result = capture_to_video(
        "https://web.archive.org/web/2026/https://x", tmp_path / "out.mp4", 1.0,
        tmp_path / "work", width=320, height=180, fps=12,
        runner=runner, browser=Path("browser.exe"),
    )
    assert result.kind == "page"
    assert "pdftoppm" not in calls


def test_capture_to_video_carries_the_live_page_warning_through(tmp_path):
    def runner(argv):
        if argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        else:
            _busy_png(tmp_path / "work" / "out.png")
        return 0, b"", b""

    result = capture_to_video(
        "https://example.com", tmp_path / "out.mp4", 1.0, tmp_path / "work",
        width=320, height=180, fps=12, runner=runner, browser=Path("b.exe"),
    )
    assert any("archive snapshot" in w for w in result.warnings)


# --- the runner must not be able to wedge a run ---------------------------------


def test_the_default_runner_times_out_instead_of_hanging(monkeypatch):
    """Headless Chrome does not reliably exit: `--virtual-time-budget` bounds
    simulated page time, not process lifetime. A live capture pass produced two
    files then sat for ten minutes with orphaned browsers still running. A
    per-slot failure is recoverable; a wedged run is not."""
    import rabbithole.sources.capture as capture_module

    def fake_run(argv, capture_output=None, timeout=None):
        assert timeout == capture_module.CAPTURE_TIMEOUT_SECONDS, "no timeout passed"
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(capture_module.subprocess, "run", fake_run)

    code, _out, err = capture_module._default_runner(["browser.exe", "--headless"])

    assert code != 0
    assert b"timed out" in err


def test_a_timed_out_browser_surfaces_as_a_capture_failure(tmp_path):
    """The timeout has to reach the caller as a normal per-slot error, so the
    rest of the run continues."""
    def timing_out_runner(argv):
        return 124, b"", b"timed out after 90s"

    with pytest.raises(RuntimeError, match="wrote no screenshot"):
        capture_page("https://slow.example", tmp_path / "shot.png",
                     runner=timing_out_runner, browser=Path("b.exe"))


def test_the_browser_is_given_an_absolute_screenshot_path(tmp_path, monkeypatch):
    """The browser is a separate process with its own working directory, so a
    relative path resolves against *its* cwd. Passing
    `projects/<slug>/assets/.capturework/x.png` made Chrome report "The system
    cannot find the path specified" on every single slot, while the identical
    call with an absolute path worked -- which made it look like a URL problem."""
    monkeypatch.chdir(tmp_path)
    seen = {}

    def runner(argv):
        seen["screenshot"] = next(a for a in argv if a.startswith("--screenshot="))
        _busy_png(Path(seen["screenshot"].split("=", 1)[1]))
        return 0, b"", b""

    capture_page("https://example.com", Path("rel/dir/shot.png"),
                 runner=runner, browser=Path("b.exe"))

    written = Path(seen["screenshot"].split("=", 1)[1])
    assert written.is_absolute(), f"relative path handed to the browser: {written}"


# --- blank captures ------------------------------------------------------------


def test_looks_blank_flags_a_uniform_frame(tmp_path):
    from rabbithole.sources.capture import looks_blank

    blank = _png(tmp_path / "blank.png", size=(400, 300), colour=(10, 10, 10))
    assert looks_blank(blank) is True


def test_looks_blank_accepts_a_frame_with_real_content(tmp_path):
    import numpy as np
    from PIL import Image
    from rabbithole.sources.capture import looks_blank

    rng = np.random.default_rng(7)
    noisy = (rng.random((300, 400, 3)) * 255).astype("uint8")
    path = tmp_path / "busy.png"
    Image.fromarray(noisy).save(path)
    assert looks_blank(path) is False


def test_a_blank_capture_is_refused_not_recorded(tmp_path):
    """Five of this project's first sixteen captures were blank -- X login walls
    and pages that never painted -- and every one would have entered the
    timeline as a valid asset. A blank frame on screen is worse than a gap."""
    out = tmp_path / "shot.png"

    def runner(argv):
        _png(out, size=(400, 300), colour=(10, 10, 10))
        return 0, b"", b""

    with pytest.raises(RuntimeError, match="frame is blank"):
        capture_page("https://x.com/someone/status/1", out,
                     runner=runner, browser=Path("b.exe"))
    assert not out.exists()


def test_the_blank_check_can_be_turned_off(tmp_path):
    """A deliberately minimal page is a legitimate shot; the author can override."""
    out = tmp_path / "shot.png"

    def runner(argv):
        _png(out, size=(400, 300), colour=(10, 10, 10))
        return 0, b"", b""

    result = capture_page("https://example.com", out, runner=runner,
                          browser=Path("b.exe"), check_blank=False)
    assert result.path.exists()


@pytest.mark.parametrize(
    ("page_source", "expected"),
    [
        (
            "<html><body>Please verify you are human to continue.</body></html>",
            "CAPTCHA or human-verification",
        ),
        (
            "<html><title>Just a moment...</title><body>Checking your browser</body></html>",
            "Cloudflare/browser challenge",
        ),
        (
            "<html><body><h1>404 Not Found</h1></body></html>",
            "HTTP or browser error page",
        ),
        (
            '<html><body><div role="dialog" aria-modal="true">'
            "Sign in to continue</div></body></html>",
            "obstructive modal or interstitial",
        ),
    ],
)
def test_browser_content_blockers_are_refused(tmp_path, page_source, expected):
    out = tmp_path / "shot.png"

    with pytest.raises(RuntimeError) as raised:
        capture_page(
            "https://example.com/source",
            out,
            runner=_busy_runner(out),
            browser=Path("b.exe"),
            page_source=page_source,
        )

    assert expected in str(raised.value)
    assert not out.exists()


def test_final_capture_fails_closed_when_page_text_cannot_be_inspected(tmp_path):
    out = tmp_path / "shot.png"

    with pytest.raises(RuntimeError, match="could not inspect page text"):
        capture_page(
            "https://example.com/source",
            out,
            runner=_busy_runner(out),
            browser=Path("b.exe"),
            require_content_text=True,
        )

    assert not out.exists()


def test_a_blank_pdf_page_is_refused_not_recorded(tmp_path):
    source = tmp_path / "notice.pdf"
    source.write_bytes(PDF_BYTES)
    out = tmp_path / "doc.png"

    def runner(argv):
        _png(tmp_path / "doc-render-1.png", colour=(255, 255, 255))
        return 0, b"", b""

    with pytest.raises(RuntimeError, match="document frame is blank or unreadable"):
        capture_document(source, out, runner=runner, work_dir=tmp_path)

    assert not out.exists()


def test_final_capture_to_video_rejects_a_cloudflare_probe_before_ffmpeg(tmp_path):
    calls = []

    def runner(argv):
        calls.append(argv[0])
        _busy_png(tmp_path / "work" / "out.png")
        return 0, b"", b""

    with pytest.raises(RuntimeError, match="Cloudflare/browser challenge"):
        capture_to_video(
            "https://example.com/source",
            tmp_path / "out.mp4",
            1.0,
            tmp_path / "work",
            runner=runner,
            transport=lambda url: (
                200,
                b"<html><title>Just a moment...</title>"
                b"<body>Checking your browser</body></html>",
            ),
            browser=Path("b.exe"),
            quality="final",
        )

    assert "ffmpeg" not in calls


def test_a_pdf_served_without_a_pdf_extension_is_routed_to_the_document_path(tmp_path):
    """Some document endpoints serve ``%PDF-1`` with no ``.pdf`` anywhere in
    the URL. Extension-only routing sent them to the browser, which produced a
    blank frame."""
    calls = []

    def runner(argv):
        calls.append(argv[0])
        if argv[0] == "pdftoppm":
            _busy_png(tmp_path / "work" / "out-render-1.png")
        elif argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        return 0, b"", b""

    result = capture_to_video(
        "https://court.example/app/downloadOrderbByDate/W.P.(C)/9639/2026",
        tmp_path / "out.mp4", 1.0, tmp_path / "work",
        width=320, height=180, fps=12,
        runner=runner, transport=lambda url: (200, PDF_BYTES),
    )

    assert result.kind == "document"
    assert "pdftoppm" in calls
    assert not any(c.endswith(".exe") for c in calls), "the browser was used for a PDF"


def test_an_html_url_still_goes_to_the_browser_after_probing(tmp_path):
    def runner(argv):
        if argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        else:
            _busy_png(tmp_path / "work" / "out.png")
        return 0, b"", b""

    result = capture_to_video(
        "https://example.com/page", tmp_path / "out.mp4", 1.0, tmp_path / "work",
        width=320, height=180, fps=12,
        runner=runner, transport=lambda url: (200, b"<!DOCTYPE html><p>hi"),
        browser=Path("b.exe"),
    )
    assert result.kind == "page"


def test_a_failed_probe_falls_through_to_the_browser(tmp_path):
    """A probe that errors must not abort the capture; the browser has its own
    error handling and may well succeed."""
    def boom(url):
        raise OSError("network down")

    def runner(argv):
        if argv[0] == "ffmpeg":
            subprocess.run(argv, capture_output=True)
        else:
            _busy_png(tmp_path / "work" / "out.png")
        return 0, b"", b""

    result = capture_to_video(
        "https://example.com/page", tmp_path / "out.mp4", 1.0, tmp_path / "work",
        width=320, height=180, fps=12,
        runner=runner, transport=boom, browser=Path("b.exe"),
    )
    assert result.kind == "page"
