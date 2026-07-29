"""Page and document capture.

Every external call is injected, so nothing here launches a browser or reaches
the network. The ffmpeg step is real, because a still that silently fails to
become a video is exactly the failure mode this package keeps finding.
"""

from __future__ import annotations

import base64
import json
import subprocess
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest

import rabbithole.sources.capture as capture_module
from rabbithole.sources.capture import (
    ARCHIVE_HOSTS,
    BrowserCaptureRequest,
    BrowserCaptureResponse,
    CaptureCrop,
    CaptureFraming,
    CaptureRectangle,
    CaptureResult,
    CaptureSpec,
    ScrollTarget,
    _capture_with_cdp_session,
    capture_document,
    capture_page,
    capture_to_video,
    fetch_source_bytes,
    find_browser,
    is_archived_url,
    is_pdf,
    memoized_transport,
    shared_page_capture,
    still_to_video,
)

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 200


def test_fetch_source_bytes_accepts_a_bounded_successful_body():
    assert (
        fetch_source_bytes(
            "https://example.com/image.jpg",
            lambda _url: (200, b"image"),
            max_bytes=10,
        )
        == b"image"
    )


def test_memoized_transport_fetches_an_exact_url_only_once():
    calls = []

    def transport(url):
        calls.append(url)
        return 200, f"body for {url}".encode()

    cached = memoized_transport(transport)

    assert cached("https://example.com/one") == cached("https://example.com/one")
    assert cached("https://example.com/two")[0] == 200
    assert calls == [
        "https://example.com/one",
        "https://example.com/two",
    ]


def test_memoized_transport_does_not_retry_a_failed_optional_probe():
    calls = []

    def transport(url):
        calls.append(url)
        raise RuntimeError("archive refused connection")

    cached = memoized_transport(transport)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="refused connection"):
            cached("https://web.archive.org/web/example")
    assert calls == ["https://web.archive.org/web/example"]


def test_default_source_fetch_identifies_the_project_with_contact_info(monkeypatch):
    seen = {}

    class Response:
        status_code = 200
        content = b"image"

    def fake_get(url, *, timeout, headers):
        seen.update(url=url, timeout=timeout, headers=headers)
        return Response()

    monkeypatch.setattr("requests.get", fake_get)

    status, body = capture_module._default_transport(
        "https://upload.wikimedia.org/example.jpg"
    )

    assert (status, body) == (200, b"image")
    assert seen["timeout"] == 60
    assert "NickYTPlaybookBot/1.0" in seen["headers"]["User-Agent"]
    assert "github.com/singhaniket-hue/nick-yt-playbook" in seen["headers"]["User-Agent"]


@pytest.mark.parametrize(
    "response,match",
    [
        ((404, b"missing"), "HTTP 404"),
        ((200, b""), "empty body"),
        ((200, b"too large"), "safety limit"),
    ],
)
def test_fetch_source_bytes_fails_closed(response, match):
    with pytest.raises(RuntimeError, match=match):
        fetch_source_bytes(
            "https://example.com/image.jpg",
            lambda _url: response,
            max_bytes=5,
        )


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


def _png_bytes(size=(320, 180)) -> bytes:
    from PIL import Image

    stream = BytesIO()
    Image.new("RGB", size, (80, 120, 160)).save(stream, format="PNG")
    return stream.getvalue()


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


def test_capture_page_passes_the_url_and_output_to_the_sandboxed_browser(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(capture_module.BROWSER_NO_SANDBOX_ENV, raising=False)
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
    assert "--no-sandbox" not in seen["argv"]


def test_capture_page_allows_an_explicit_no_sandbox_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv(capture_module.BROWSER_NO_SANDBOX_ENV, "true")
    out = tmp_path / "shot.png"
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        _busy_png(out)
        return 0, b"", b""

    capture_page("https://example.com/x", out, runner=runner, browser=Path("b.exe"))

    assert "--no-sandbox" in seen["argv"]


def test_capture_spec_round_trips_through_plain_json():
    spec = CaptureSpec.from_value(
        {
            "full_page": True,
            "selector": "main article",
            "text": "Google acknowledged the test",
            "scroll_to": {"text": "Webdriver Torso"},
            "crop": {"x": 20, "y": 900, "width": 1280, "height": 720},
        }
    )

    restored = CaptureSpec.from_value(json.loads(json.dumps(spec.to_dict())))

    assert restored == spec
    assert restored.text_locator == "Google acknowledged the test"
    assert restored.scroll_target == ScrollTarget(text="Webdriver Torso")
    assert restored.crop == CaptureCrop(x=20, y=900, width=1280, height=720)


def test_targeted_capture_retains_a_deterministic_below_fold_crop(tmp_path):
    import numpy as np
    from PIL import Image

    out = tmp_path / "below-fold.png"
    seen = {}

    def targeted(request):
        seen["request"] = request
        full_page = np.zeros((1080, 320, 3), dtype="uint8")
        rng = np.random.default_rng(17)
        full_page[720:900] = (
            rng.random((180, 320, 3)) * 255
        ).astype("uint8")
        Image.fromarray(full_page).save(request.out_png)
        return "<html><body><p>Google acknowledged the test</p></body></html>"

    result = capture_page(
        "https://web.archive.org/web/2026/https://example.com",
        out,
        width=320,
        height=180,
        browser=Path("browser.exe"),
        spec={
            "full_page": True,
            "text": "Google acknowledged the test",
            "crop": {"x": 0, "y": 720, "width": 320, "height": 180},
        },
        targeted_capture=targeted,
    )

    assert seen["request"].spec.text == "Google acknowledged the test"
    assert seen["request"].spec.full_page is True
    assert Image.open(result.path).size == (320, 180)
    assert np.asarray(Image.open(result.path)).std() > 30


def test_targeted_capture_fails_closed_and_removes_a_stale_frame(tmp_path):
    out = _busy_png(tmp_path / "stale.png")

    def target_missing(request):
        raise RuntimeError("Capture target text 'acknowledged' was not found")

    with pytest.raises(RuntimeError, match="was not found"):
        capture_page(
            "https://example.com",
            out,
            browser=Path("browser.exe"),
            spec={"text": "acknowledged"},
            targeted_capture=target_missing,
        )

    assert not out.exists()


def test_capture_crop_outside_the_frame_fails_closed(tmp_path):
    out = tmp_path / "shot.png"

    with pytest.raises(RuntimeError, match="falls outside retained frame"):
        capture_page(
            "https://example.com",
            out,
            width=320,
            height=180,
            runner=_busy_runner(out),
            browser=Path("browser.exe"),
            spec={"crop": {"x": 0, "y": 170, "width": 320, "height": 20}},
        )

    assert not out.exists()


class _FakeCdp:
    def __init__(self, *, target_result, screenshot_size=(400, 1200)):
        self.target_result = target_result
        self.screenshot_size = screenshot_size
        self.calls = []

    def command(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression == "document.readyState":
                value = "complete"
            elif "__rabbitholeCaptureTarget" in expression:
                value = self.target_result
            else:
                value = "<html><body>evidence</body></html>"
            return {"result": {"type": "object", "value": value}}
        if method == "Page.getLayoutMetrics":
            return {
                "cssContentSize": {
                    "width": self.screenshot_size[0],
                    "height": self.screenshot_size[1],
                }
            }
        if method == "Page.captureScreenshot":
            return {
                "data": base64.b64encode(
                    _png_bytes(self.screenshot_size)
                ).decode("ascii")
            }
        return {}


def test_cdp_capture_centers_a_bounded_document_clip_on_a_below_fold_target(
    tmp_path,
):
    out = tmp_path / "page.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollY": 840,
            "targetRect": {"x": 10, "y": 900, "width": 300, "height": 80},
        }
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(full_page=True, text="acknowledged"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    capture_call = next(
        params for method, params in session.calls if method == "Page.captureScreenshot"
    )
    assert capture_call["captureBeyondViewport"] is True
    assert capture_call["clip"] == {
        "x": 0,
        "y": 827.5,
        "width": 400,
        "height": 225,
        "scale": 1,
    }
    assert isinstance(response, BrowserCaptureResponse)
    assert response.framing == CaptureFraming(
        mode="target",
        target=CaptureRectangle(x=10, y=900, width=300, height=80),
        clip=CaptureRectangle(x=0, y=827.5, width=400, height=225),
        content=CaptureRectangle(x=0, y=0, width=400, height=1200),
    )
    assert out.read_bytes().startswith(b"\x89PNG")


def test_cdp_untargeted_full_page_capture_is_unchanged(tmp_path):
    out = tmp_path / "full-page.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 0,
            "scrollY": 0,
            "targetRect": None,
        },
        screenshot_size=(400, 1200),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(full_page=True),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    capture_call = next(
        params for method, params in session.calls if method == "Page.captureScreenshot"
    )
    assert capture_call["captureBeyondViewport"] is True
    assert capture_call["clip"] == {
        "x": 0,
        "y": 0,
        "width": 400,
        "height": 1200,
        "scale": 1,
    }
    assert response.framing.mode == "full-page"
    assert response.framing.target is None


def test_cdp_explicit_crop_is_sent_directly_to_the_browser(tmp_path):
    out = tmp_path / "authored-crop-source.png"
    authored = CaptureCrop(x=20, y=700, width=300, height=160)
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollY": 840,
            "targetRect": {"x": 10, "y": 900, "width": 300, "height": 80},
        },
        screenshot_size=(400, 1200),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(
            full_page=True,
            text="acknowledged",
            crop=authored,
        ),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    capture_call = next(
        params for method, params in session.calls if method == "Page.captureScreenshot"
    )
    assert capture_call["captureBeyondViewport"] is True
    assert capture_call["clip"] == {
        "x": 20.0,
        "y": 700.0,
        "width": 300.0,
        "height": 160.0,
        "scale": 1,
    }
    assert response.framing.mode == "explicit-crop"
    assert response.framing.authored_crop == authored
    assert response.framing.clip == CaptureRectangle(
        x=20,
        y=700,
        width=300,
        height=160,
    )


def test_cdp_viewport_crop_is_translated_to_post_scroll_page_coordinates(tmp_path):
    out = tmp_path / "viewport-crop.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 5,
            "scrollY": 800,
            "targetRect": {"x": 20, "y": 850, "width": 200, "height": 60},
        },
        screenshot_size=(1000, 2000),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(
            text="acknowledged",
            crop=CaptureCrop(x=20, y=50, width=300, height=160),
        ),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    capture_call = next(
        params for method, params in session.calls if method == "Page.captureScreenshot"
    )
    assert capture_call["clip"] == {
        "x": 25,
        "y": 850,
        "width": 300.0,
        "height": 160.0,
        "scale": 1,
    }
    assert response.framing.clip == CaptureRectangle(
        x=25,
        y=850,
        width=300,
        height=160,
    )


def test_native_explicit_crop_is_not_cropped_a_second_time(tmp_path):
    out = tmp_path / "direct-crop.png"
    authored = CaptureCrop(x=20, y=700, width=300, height=160)

    def targeted(request):
        _busy_png(request.out_png, size=(authored.width, authored.height))
        return BrowserCaptureResponse(
            page_source="<html><body>acknowledged</body></html>",
            framing=CaptureFraming(
                mode="explicit-crop",
                target=CaptureRectangle(x=10, y=900, width=300, height=80),
                clip=CaptureRectangle(
                    x=authored.x,
                    y=authored.y,
                    width=authored.width,
                    height=authored.height,
                ),
                content=CaptureRectangle(x=0, y=0, width=400, height=1200),
                authored_crop=authored,
            ),
        )

    result = capture_page(
        "https://web.archive.org/web/2026/https://example.com",
        out,
        width=400,
        height=300,
        browser=Path("browser.exe"),
        spec=CaptureSpec(
            full_page=True,
            text="acknowledged",
            crop=authored,
        ),
        targeted_capture=targeted,
    )

    from PIL import Image

    assert Image.open(result.path).size == (300, 160)


@pytest.mark.parametrize(
    "content_size",
    [
        (capture_module.MAX_CAPTURE_DIMENSION + 1, 100),
        (10_000, 5_000),
    ],
)
def test_cdp_full_page_capture_rejects_unbounded_raster_sizes(
    tmp_path, content_size
):
    out = tmp_path / "oversized.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 0,
            "scrollY": 0,
            "targetRect": None,
        },
        screenshot_size=content_size,
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(full_page=True),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    with pytest.raises(RuntimeError, match="capture safety budget"):
        _capture_with_cdp_session(session, request)

    assert not any(
        method == "Page.captureScreenshot" for method, _params in session.calls
    )
    assert not out.exists()


def test_distinct_target_rectangles_produce_distinct_document_clips(tmp_path):
    clips = []
    for index, target_y in enumerate((300, 900), start=1):
        session = _FakeCdp(
            target_result={
                "ok": True,
                "scrollY": target_y,
                "targetRect": {
                    "x": 40,
                    "y": target_y,
                    "width": 240,
                    "height": 60,
                },
            },
            screenshot_size=(1000, 1400),
        )
        request = BrowserCaptureRequest(
            url="https://example.com",
            out_png=tmp_path / f"target-{index}.png",
            spec=CaptureSpec(text=f"evidence {index}"),
            width=400,
            height=300,
            settle_ms=0,
            browser=Path("browser.exe"),
        )

        response = _capture_with_cdp_session(session, request)
        clips.append(response.framing.clip)

    assert clips == [
        CaptureRectangle(x=0, y=228.75, width=360, height=202.5),
        CaptureRectangle(x=0, y=828.75, width=360, height=202.5),
    ]


def test_narrow_paragraph_uses_a_smaller_readable_document_clip(tmp_path):
    target = CaptureRectangle(x=600, y=1000, width=600, height=80)
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollY": 800,
            "targetRect": target.to_dict(),
        },
        screenshot_size=(1920, 4000),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=tmp_path / "narrow-paragraph.png",
        spec=CaptureSpec(text="one exact archived paragraph"),
        width=1920,
        height=1080,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    clip = response.framing.clip
    assert clip == CaptureRectangle(x=420, y=770, width=960, height=540)
    assert clip.width < request.width
    assert clip.x <= target.x
    assert clip.y <= target.y
    assert clip.right >= target.right
    assert clip.bottom >= target.bottom


def test_wide_target_expands_adaptive_clip_without_omitting_target(tmp_path):
    target = CaptureRectangle(x=300, y=1800, width=1000, height=120)
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollY": 1600,
            "targetRect": target.to_dict(),
        },
        screenshot_size=(1920, 4000),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=tmp_path / "wide-target.png",
        spec=CaptureSpec(selector="#wide-comparison"),
        width=1920,
        height=1080,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    clip = response.framing.clip
    assert clip == CaptureRectangle(
        x=50,
        y=1438.125,
        width=1500,
        height=843.75,
    )
    assert clip.width > 960
    assert clip.x <= target.x
    assert clip.y <= target.y
    assert clip.right >= target.right
    assert clip.bottom >= target.bottom


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            {"x": 0, "y": 0, "width": 20, "height": 20},
            CaptureRectangle(x=0, y=0, width=200, height=112.5),
        ),
        (
            {"x": 980, "y": 1180, "width": 20, "height": 20},
            CaptureRectangle(x=800, y=1087.5, width=200, height=112.5),
        ),
    ],
)
def test_target_document_clip_stays_inside_content_near_edges(
    tmp_path, target, expected
):
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollY": target["y"],
            "targetRect": target,
        },
        screenshot_size=(1000, 1200),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=tmp_path / f"edge-{target['x']}-{target['y']}.png",
        spec=CaptureSpec(selector="#proof"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    response = _capture_with_cdp_session(session, request)

    assert response.framing.clip == expected
    assert expected.x >= 0
    assert expected.y >= 0
    assert expected.right <= 1000
    assert expected.bottom <= 1200


def test_cdp_capture_does_not_write_pixels_when_target_is_missing(tmp_path):
    out = tmp_path / "page.png"
    session = _FakeCdp(
        target_result={"ok": False, "error": "selector #proof was not found"}
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(selector="#proof"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    with pytest.raises(RuntimeError, match="selector #proof was not found"):
        _capture_with_cdp_session(session, request)

    assert not out.exists()
    assert not any(method == "Page.captureScreenshot" for method, _ in session.calls)


def test_cdp_capture_fails_closed_when_resolved_target_has_no_rectangle(tmp_path):
    out = tmp_path / "page.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 0,
            "scrollY": 0,
            "targetRect": None,
        }
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(text="resolved without geometry"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    with pytest.raises(RuntimeError, match="no resolved evidence target rectangle"):
        _capture_with_cdp_session(session, request)

    assert not out.exists()
    assert not any(method == "Page.captureScreenshot" for method, _ in session.calls)


def test_cdp_capture_fails_closed_when_target_exceeds_readable_clip(tmp_path):
    out = tmp_path / "page.png"
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 0,
            "scrollY": 100,
            "targetRect": {"x": 0, "y": 100, "width": 400, "height": 900},
        },
        screenshot_size=(400, 1200),
    )
    request = BrowserCaptureRequest(
        url="https://example.com",
        out_png=out,
        spec=CaptureSpec(selector="main"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    with pytest.raises(RuntimeError, match="author a tighter selector"):
        _capture_with_cdp_session(session, request)

    assert not out.exists()
    assert not any(method == "Page.captureScreenshot" for method, _ in session.calls)


def test_capture_result_records_native_target_framing(tmp_path, monkeypatch):
    session = _FakeCdp(
        target_result={
            "ok": True,
            "scrollX": 0,
            "scrollY": 620,
            "targetRect": {"x": 220, "y": 700, "width": 200, "height": 60},
        },
        screenshot_size=(1000, 1200),
    )

    @contextmanager
    def fake_devtools(_browser, *, width, height):
        assert (width, height) == (400, 300)
        yield session

    monkeypatch.setattr(capture_module, "_chrome_devtools", fake_devtools)
    result = capture_page(
        "https://web.archive.org/web/2026/https://example.com",
        tmp_path / "target.png",
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
        check_blank=False,
        check_obstructions=False,
        spec=CaptureSpec(text="specific evidence"),
    )

    assert result.framing == CaptureFraming(
        mode="target",
        target=CaptureRectangle(x=220, y=700, width=200, height=60),
        clip=CaptureRectangle(x=170, y=645.625, width=300, height=168.75),
        content=CaptureRectangle(x=0, y=0, width=1000, height=1200),
    )


class _SharedFakeCdp(_FakeCdp):
    def command(self, method, params=None):
        params = params or {}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if "__rabbitholeCaptureTarget" in expression:
                self.calls.append((method, params))
                if "missing evidence" in expression:
                    value = {
                        "ok": False,
                        "error": 'text "missing evidence" was not found',
                    }
                else:
                    value = {
                        "ok": True,
                        "scrollY": 600,
                        "targetRect": {
                            "x": 0,
                            "y": 600,
                            "width": 300,
                            "height": 80,
                        },
                    }
                return {"result": {"type": "object", "value": value}}
        return super().command(method, params)


def test_shared_page_capture_navigates_once_but_captures_each_authored_target(
    tmp_path, monkeypatch
):
    session = _SharedFakeCdp(target_result={})

    @contextmanager
    def fake_devtools(_browser, *, width, height):
        assert (width, height) == (400, 300)
        yield session

    monkeypatch.setattr(capture_module, "_chrome_devtools", fake_devtools)
    requests = [
        BrowserCaptureRequest(
            url="https://web.archive.org/web/2026/https://example.com",
            out_png=tmp_path / f"target-{index}.png",
            spec=CaptureSpec(full_page=True, text=text),
            width=400,
            height=300,
            settle_ms=0,
            browser=Path("browser.exe"),
        )
        for index, text in enumerate(("first evidence", "second evidence"), start=1)
    ]

    with shared_page_capture() as targeted:
        for request in requests:
            targeted(request)

    assert sum(method == "Page.navigate" for method, _ in session.calls) == 1
    assert (
        sum(method == "Page.captureScreenshot" for method, _ in session.calls)
        == 2
    )
    assert all(request.out_png.read_bytes().startswith(b"\x89PNG") for request in requests)


def test_shared_page_capture_keeps_other_targets_after_one_fails_closed(
    tmp_path, monkeypatch
):
    session = _SharedFakeCdp(target_result={})

    @contextmanager
    def fake_devtools(_browser, *, width, height):
        yield session

    monkeypatch.setattr(capture_module, "_chrome_devtools", fake_devtools)
    missing = BrowserCaptureRequest(
        url="https://web.archive.org/web/2026/https://example.com",
        out_png=tmp_path / "missing.png",
        spec=CaptureSpec(text="missing evidence"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )
    present = BrowserCaptureRequest(
        url=missing.url,
        out_png=tmp_path / "present.png",
        spec=CaptureSpec(text="present evidence"),
        width=400,
        height=300,
        settle_ms=0,
        browser=Path("browser.exe"),
    )

    with shared_page_capture() as targeted:
        with pytest.raises(RuntimeError, match="missing evidence"):
            targeted(missing)
        targeted(present)

    assert not missing.out_png.exists()
    assert present.out_png.exists()
    assert sum(method == "Page.navigate" for method, _ in session.calls) == 1
    assert (
        sum(method == "Page.captureScreenshot" for method, _ in session.calls)
        == 1
    )


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


def test_capture_to_video_forwards_a_json_capture_spec(tmp_path):
    seen = {}

    def targeted(request):
        seen["spec"] = request.spec
        _busy_png(request.out_png)
        return "<html><body>Google acknowledgement</body></html>"

    def runner(argv):
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"video")
        return 0, b"", b""

    result = capture_to_video(
        "https://example.com/page",
        tmp_path / "out.mp4",
        1.0,
        tmp_path / "work",
        width=320,
        height=180,
        runner=runner,
        transport=lambda url: (200, b"<html><body>Google acknowledgement</body></html>"),
        browser=Path("browser.exe"),
        spec={"text": "Google acknowledgement", "scroll_to": 700},
        targeted_capture=targeted,
    )

    assert seen["spec"] == CaptureSpec(
        text="Google acknowledgement", scroll_target=ScrollTarget(y=700)
    )
    assert result.path == (tmp_path / "out.mp4").resolve()


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


def _targeted_text_response() -> BrowserCaptureResponse:
    return BrowserCaptureResponse(
        page_source="<html><body><p>Google acknowledged this test.</p></body></html>",
        framing=CaptureFraming(
            mode="target",
            target=CaptureRectangle(x=40, y=65, width=320, height=85),
            clip=CaptureRectangle(x=0, y=0, width=400, height=225),
            content=CaptureRectangle(x=0, y=0, width=400, height=900),
        ),
    )


def test_targeted_sparse_black_text_on_white_passes_structural_blank_qa(tmp_path):
    from PIL import Image, ImageDraw

    out = tmp_path / "targeted-text.png"

    def targeted(request):
        image = Image.new("L", (400, 225), 255)
        draw = ImageDraw.Draw(image)
        draw.multiline_text(
            (55, 78),
            "Google acknowledged this test.\n"
            "Video quality checks were automated.",
            fill=0,
            spacing=5,
        )
        image = image.point(lambda value: 0 if value < 128 else 255)
        image.save(request.out_png)
        return _targeted_text_response()

    result = capture_page(
        "https://web.archive.org/web/2026/https://example.com",
        out,
        width=400,
        height=225,
        browser=Path("browser.exe"),
        spec={"text": "Google acknowledged this test"},
        targeted_capture=targeted,
    )

    # The broad palette rule does reject this deliberately binary fixture; the
    # resolved-target pixel structure is what conservatively rescues it.
    assert capture_module.inspect_frame(result.path).looks_blank is True
    assert result.path.exists()


@pytest.mark.parametrize(
    "fixture",
    [
        "uniform-white",
        "failed-embed-rectangle",
        "white-error-on-black",
        "dot-grid",
        "outlined-box-grid",
    ],
)
def test_targeted_blank_fallback_rejects_non_text_structure(tmp_path, fixture):
    from PIL import Image, ImageDraw

    out = tmp_path / f"{fixture}.png"

    def targeted(request):
        image = Image.new("L", (400, 225), 255)
        draw = ImageDraw.Draw(image)
        if fixture == "failed-embed-rectangle":
            draw.rectangle((55, 72, 345, 145), fill=0)
        elif fixture == "white-error-on-black":
            draw.rectangle((40, 65, 360, 150), fill=0)
            draw.text((80, 95), "CONTENT FAILED TO LOAD", fill=255)
        elif fixture == "dot-grid":
            for y in range(78, 136, 14):
                for x in range(65, 336, 18):
                    draw.ellipse((x, y, x + 4, y + 4), fill=0)
        elif fixture == "outlined-box-grid":
            for y in range(76, 137, 16):
                for x in range(65, 336, 22):
                    draw.rectangle((x, y, x + 9, y + 11), outline=0, width=1)
        image = image.point(lambda value: 0 if value < 128 else 255)
        image.save(request.out_png)
        return _targeted_text_response()

    with pytest.raises(RuntimeError, match="frame is blank"):
        capture_page(
            "https://web.archive.org/web/2026/https://example.com",
            out,
            width=400,
            height=225,
            browser=Path("browser.exe"),
            spec={"selector": "#resolved-evidence"},
            targeted_capture=targeted,
        )

    assert not out.exists()


def test_untargeted_sparse_text_keeps_the_general_fail_closed_blank_rule(tmp_path):
    from PIL import Image, ImageDraw

    out = tmp_path / "untargeted-text.png"

    def runner(_argv):
        image = Image.new("L", (400, 225), 255)
        ImageDraw.Draw(image).text((55, 78), "Sparse text is not a resolved target.", fill=0)
        image.save(out)
        return 0, b"", b""

    with pytest.raises(RuntimeError, match="frame is blank"):
        capture_page(
            "https://example.com",
            out,
            width=400,
            height=225,
            runner=runner,
            browser=Path("browser.exe"),
        )

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
        (
            "<html><body><h1>It's your choice</h1>"
            "<p>Decide how your data is used.</p>"
            "<button>Manage cookies</button><button>Accept all</button>"
            "</body></html>",
            "cookie-consent wall",
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
