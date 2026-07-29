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

import base64
import html
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit

from rabbithole.encoding import video_args

# argv -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, bytes, bytes]]
# url -> (status, body)
Transport = Callable[[str], tuple[int, bytes]]

CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080
DOCUMENT_CLIP_ASPECT = 16 / 9
# A small text target should not become a context-free macro crop, but retaining
# an entire 1920px page makes old 600px article columns unreadable. Keep at
# least half of the available viewport, and request 25% of the target's size as
# context on each side whenever document bounds allow it.
TARGET_CLIP_MIN_VIEWPORT_FRACTION = 0.5
TARGET_CLIP_CONTEXT_MULTIPLIER = 1.5

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
SOURCE_FETCH_USER_AGENT = (
    "NickYTPlaybookBot/1.0 "
    "(https://github.com/singhaniket-hue/nick-yt-playbook; "
    "documentary source acquisition)"
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
MAX_RETAINED_SOURCE_BYTES = 100 * 1024 * 1024

# Native browser sandboxes stay enabled by default. Some constrained container
# runtimes cannot start Chromium with its sandbox, but disabling it while
# visiting arbitrary research URLs is a material security downgrade. Keep that
# downgrade behind a deliberate, narrowly named environment opt-in.
BROWSER_NO_SANDBOX_ENV = "RABBITHOLE_BROWSER_NO_SANDBOX"

# Chrome can otherwise be asked to rasterize the full CSS dimensions of an
# arbitrarily tall page. Bound both axes and total decoded pixels before asking
# it to allocate/encode a PNG. Explicit crops are clipped by CDP directly and
# are checked against the same output budget.
MAX_CAPTURE_DIMENSION = 16_384
MAX_CAPTURE_PIXELS = 40_000_000


@dataclass(frozen=True)
class CaptureCrop:
    """A deterministic pixel crop in the retained screenshot's coordinate space.

    For a full-page capture the coordinates are page-relative. Otherwise they
    are relative to the post-scroll viewport.
    """

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        values = {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Capture crop {name} must be an integer, got {value!r}")
        if self.x < 0 or self.y < 0:
            raise ValueError("Capture crop x and y must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Capture crop width and height must be positive")

    @classmethod
    def from_value(cls, value: CaptureCrop | Mapping[str, Any]) -> CaptureCrop:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("capture crop must be a CaptureCrop or mapping")
        allowed = {"x", "y", "width", "height"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                f"Unknown capture crop field(s): {', '.join(sorted(unknown))}"
            )
        missing = allowed - set(value)
        if missing:
            raise ValueError(
                f"Capture crop is missing: {', '.join(sorted(missing))}"
            )
        return cls(
            x=value["x"],
            y=value["y"],
            width=value["width"],
            height=value["height"],
        )


@dataclass(frozen=True)
class ScrollTarget:
    """A page offset, selector, or text locator to center before capture."""

    y: int | None = None
    selector: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        modes = (self.y is not None, bool(self.selector), bool(self.text))
        if sum(modes) != 1:
            raise ValueError(
                "ScrollTarget requires exactly one of y, selector, or text"
            )
        if self.y is not None:
            if isinstance(self.y, bool) or not isinstance(self.y, int):
                raise TypeError(f"Scroll target y must be an integer, got {self.y!r}")
            if self.y < 0:
                raise ValueError("Scroll target y must be non-negative")
        for name, value in (("selector", self.selector), ("text", self.text)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"Scroll target {name} must be non-empty text")

    @classmethod
    def from_value(
        cls, value: ScrollTarget | Mapping[str, Any] | str | int
    ) -> ScrollTarget:
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise TypeError("scroll target cannot be a boolean")
        if isinstance(value, int):
            return cls(y=value)
        if isinstance(value, str):
            return cls(selector=value)
        if not isinstance(value, Mapping):
            raise TypeError(
                "scroll target must be an integer, selector string, or mapping"
            )
        aliases = dict(value)
        if "text_locator" in aliases:
            if "text" in aliases:
                raise ValueError("scroll target cannot contain both text and text_locator")
            aliases["text"] = aliases.pop("text_locator")
        allowed = {"y", "selector", "text"}
        unknown = set(aliases) - allowed
        if unknown:
            raise ValueError(
                f"Unknown scroll target field(s): {', '.join(sorted(unknown))}"
            )
        return cls(
            y=aliases.get("y"),
            selector=aliases.get("selector"),
            text=aliases.get("text"),
        )


@dataclass(frozen=True)
class CaptureSpec:
    """Serializable author intent for a browser screenshot.

    The default preserves the original one-shot viewport capture. Any
    selector/text/scroll/full-page request switches to browser control so
    below-fold evidence is located before pixels are retained.
    """

    full_page: bool = False
    selector: str | None = None
    text: str | None = None
    scroll_target: ScrollTarget | None = None
    crop: CaptureCrop | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.full_page, bool):
            raise TypeError("capture full_page must be a boolean")
        for name, value in (("selector", self.selector), ("text", self.text)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"Capture {name} must be non-empty text")
        if self.scroll_target is not None and not isinstance(
            self.scroll_target, ScrollTarget
        ):
            object.__setattr__(
                self, "scroll_target", ScrollTarget.from_value(self.scroll_target)
            )
        if self.crop is not None and not isinstance(self.crop, CaptureCrop):
            object.__setattr__(self, "crop", CaptureCrop.from_value(self.crop))

    @property
    def needs_browser_control(self) -> bool:
        return bool(
            self.full_page or self.selector or self.text or self.scroll_target
        )

    @property
    def text_locator(self) -> str | None:
        return self.text

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable artifact-catalogue mapping."""
        scroll: dict[str, Any] | None = None
        if self.scroll_target is not None:
            scroll = {
                key: value
                for key, value in {
                    "y": self.scroll_target.y,
                    "selector": self.scroll_target.selector,
                    "text": self.scroll_target.text,
                }.items()
                if value is not None
            }
        crop: dict[str, int] | None = None
        if self.crop is not None:
            crop = {
                "x": self.crop.x,
                "y": self.crop.y,
                "width": self.crop.width,
                "height": self.crop.height,
            }
        return {
            key: value
            for key, value in {
                "full_page": self.full_page,
                "selector": self.selector,
                "text": self.text,
                "scroll_target": scroll,
                "crop": crop,
            }.items()
            if value is not None
        }

    @classmethod
    def from_value(
        cls, value: CaptureSpec | Mapping[str, Any] | None
    ) -> CaptureSpec:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("capture spec must be a CaptureSpec, mapping, or None")
        fields = dict(value)
        if "text_locator" in fields:
            if "text" in fields:
                raise ValueError("capture spec cannot contain both text and text_locator")
            fields["text"] = fields.pop("text_locator")
        if "scroll_to" in fields:
            if "scroll_target" in fields:
                raise ValueError(
                    "capture spec cannot contain both scroll_to and scroll_target"
                )
            fields["scroll_target"] = fields.pop("scroll_to")
        if "crop_rectangle" in fields:
            if "crop" in fields:
                raise ValueError(
                    "capture spec cannot contain both crop and crop_rectangle"
                )
            fields["crop"] = fields.pop("crop_rectangle")
        allowed = {"full_page", "selector", "text", "scroll_target", "crop"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"Unknown capture spec field(s): {', '.join(sorted(unknown))}"
            )
        if fields.get("scroll_target") is not None:
            fields["scroll_target"] = ScrollTarget.from_value(fields["scroll_target"])
        if fields.get("crop") is not None:
            fields["crop"] = CaptureCrop.from_value(fields["crop"])
        return cls(**fields)


@dataclass(frozen=True)
class BrowserCaptureRequest:
    url: str
    out_png: Path
    spec: CaptureSpec
    width: int
    height: int
    settle_ms: int
    browser: Path


@dataclass(frozen=True)
class CaptureRectangle:
    """One page-coordinate rectangle retained for capture inspection."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("Capture rectangle values must be finite")
        if self.x < 0 or self.y < 0:
            raise ValueError("Capture rectangle x and y must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Capture rectangle width and height must be positive")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def to_dict(self) -> dict[str, float]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class CaptureFraming:
    """What browser pixels were retained and why.

    ``target`` and ``clip`` are page-coordinate CSS-pixel rectangles. For an
    explicit native CDP crop, ``clip`` is the exact authored region retained by
    the browser rather than an unbounded full-page intermediate. Callers can
    therefore distinguish automatic evidence framing from an author-owned crop
    and fail closed when a browser returns inconsistent geometry.
    """

    mode: str
    target: CaptureRectangle | None = None
    clip: CaptureRectangle | None = None
    content: CaptureRectangle | None = None
    authored_crop: CaptureCrop | None = None

    def to_dict(self) -> dict[str, Any]:
        crop = self.authored_crop
        return {
            "mode": self.mode,
            "target": self.target.to_dict() if self.target else None,
            "clip": self.clip.to_dict() if self.clip else None,
            "content": self.content.to_dict() if self.content else None,
            "authored_crop": (
                {
                    "x": crop.x,
                    "y": crop.y,
                    "width": crop.width,
                    "height": crop.height,
                }
                if crop
                else None
            ),
        }


@dataclass(frozen=True)
class BrowserCaptureResponse:
    """Rendered DOM plus the exact browser framing used for its PNG."""

    page_source: str | bytes | None
    framing: CaptureFraming


TargetedCaptureOutput = str | bytes | BrowserCaptureResponse | None
TargetedCapture = Callable[[BrowserCaptureRequest], TargetedCaptureOutput]


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


def _browser_sandbox_override_args() -> tuple[str, ...]:
    """Return Chrome's sandbox override only after an explicit environment opt-in."""

    value = os.environ.get(BROWSER_NO_SANDBOX_ENV, "").strip().lower()
    return ("--no-sandbox",) if value in {"1", "true", "yes"} else ()


def _default_transport(url: str) -> tuple[int, bytes]:
    import requests

    response = requests.get(
        url, timeout=60,
        headers={"User-Agent": SOURCE_FETCH_USER_AGENT},
    )
    return response.status_code, response.content


def memoized_transport(transport: Transport | None = None) -> Transport:
    """Return a per-run transport that probes each exact URL at most once.

    Browser capture first performs a lightweight source probe so PDF endpoints
    are routed to the document renderer and final-quality page captures can
    inspect source text.  Several authored targets can legitimately share one
    page; probing that page once per target wastes traffic and can trip archive
    rate limits before the browser gets to the later evidence.  Cache both
    successful responses and failures so a failed optional probe also remains
    one request -- the browser path is still allowed to proceed.
    """

    active = transport or _default_transport
    responses: dict[str, tuple[int, bytes]] = {}
    failures: dict[str, Exception] = {}

    def fetch(url: str) -> tuple[int, bytes]:
        if url in responses:
            return responses[url]
        if url in failures:
            raise failures[url]
        try:
            response = active(url)
        except Exception as exc:
            failures[url] = exc
            raise
        responses[url] = response
        return response

    return fetch


def fetch_source_bytes(
    url: str,
    transport: Transport | None = None,
    *,
    max_bytes: int = MAX_RETAINED_SOURCE_BYTES,
) -> bytes:
    """Fetch one retained still/document body with a bounded, injectable client."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    status, body = (transport or _default_transport)(url)
    if status != 200:
        raise RuntimeError(f"Source fetch returned HTTP {status} for {url!r}")
    if not body:
        raise RuntimeError(f"Source fetch returned an empty body for {url!r}")
    if len(body) > max_bytes:
        raise RuntimeError(
            f"Source fetch returned {len(body)} bytes for {url!r}, exceeding "
            f"the {max_bytes}-byte safety limit"
        )
    return body


class _CdpCommands(Protocol):
    def command(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        ...


class _WebSocket:
    """Minimal local WebSocket client for Chrome's DevTools endpoint."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "ws" or not parts.hostname:
            raise RuntimeError(f"Unsupported DevTools WebSocket URL {url!r}")
        port = parts.port or 80
        self._socket = socket.create_connection(
            (parts.hostname, port), timeout=CAPTURE_TIMEOUT_SECONDS
        )
        self._socket.settimeout(CAPTURE_TIMEOUT_SECONDS)
        self._buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        resource = parts.path or "/"
        if parts.query:
            resource += f"?{parts.query}"
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {parts.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise RuntimeError("DevTools WebSocket closed during handshake")
            response.extend(chunk)
            if len(response) > 64 * 1024:
                raise RuntimeError("DevTools WebSocket handshake was unexpectedly large")
        header, remainder = bytes(response).split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(
                "DevTools WebSocket handshake failed: "
                + header.decode("utf-8", errors="replace")[-500:]
            )
        self._buffer.extend(remainder)

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            self._socket.close()

    def _read_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            chunk = self._socket.recv(max(4096, length - len(self._buffer)))
            if not chunk:
                raise RuntimeError("DevTools WebSocket closed unexpectedly")
            self._buffer.extend(chunk)
        value = bytes(self._buffer[:length])
        del self._buffer[:length]
        return value

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        size = len(payload)
        if size < 126:
            header = bytes((first, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", size)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_text(self, value: str) -> None:
        self._send_frame(0x1, value.encode("utf-8"))

    def receive_text(self) -> str:
        fragments = bytearray()
        expecting_continuation = False
        while True:
            first, second = self._read_exact(2)
            finished = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise RuntimeError("DevTools WebSocket closed before replying")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                expecting_continuation = not finished
                if finished:
                    return fragments.decode("utf-8")
                continue
            if opcode == 0x0 and expecting_continuation:
                fragments.extend(payload)
                if finished:
                    return fragments.decode("utf-8")
                continue
            raise RuntimeError(f"Unexpected DevTools WebSocket opcode {opcode}")


class _CdpSession:
    def __init__(self, websocket_url: str) -> None:
        self._websocket = _WebSocket(websocket_url)
        self._next_id = 1

    def close(self) -> None:
        self._websocket.close()

    def command(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        command_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": command_id, "method": method}
        if params:
            payload["params"] = dict(params)
        self._websocket.send_text(json.dumps(payload, separators=(",", ":")))
        while True:
            message = json.loads(self._websocket.receive_text())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = (
                    error.get("message", error)
                    if isinstance(error, Mapping)
                    else error
                )
                raise RuntimeError(f"DevTools {method} failed: {detail}")
            result = message.get("result", {})
            return result if isinstance(result, Mapping) else {}


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _devtools_pages(port: int) -> list[Mapping[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/list",
        headers={"Connection": "close"},
    )
    with urllib.request.urlopen(request, timeout=1.0) as response:
        value = json.loads(response.read())
    return value if isinstance(value, list) else []


@contextmanager
def _chrome_devtools(
    browser: Path, *, width: int, height: int
) -> Iterator[_CdpSession]:
    """Launch an isolated headless browser and yield its page DevTools session."""
    port = _free_local_port()
    with tempfile.TemporaryDirectory(
        prefix="rabbithole-capture-", ignore_cleanup_errors=True
    ) as profile:
        argv = [
            str(browser),
            "--headless",
            "--disable-gpu",
            *_browser_sandbox_override_args(),
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--user-agent={CAPTURE_USER_AGENT}",
            f"--window-size={width},{height}",
            "about:blank",
        ]
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(argv, **popen_kwargs)
        session: _CdpSession | None = None
        try:
            deadline = time.monotonic() + min(20.0, CAPTURE_TIMEOUT_SECONDS)
            websocket_url: str | None = None
            last_error = ""
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Browser exited before DevTools was ready (exit {process.returncode})"
                    )
                try:
                    page = next(
                        (
                            item
                            for item in _devtools_pages(port)
                            if item.get("type") == "page"
                            and item.get("webSocketDebuggerUrl")
                        ),
                        None,
                    )
                    if page:
                        websocket_url = str(page["webSocketDebuggerUrl"])
                        break
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    last_error = str(exc)
                time.sleep(0.1)
            if not websocket_url:
                raise RuntimeError(
                    "Browser DevTools endpoint did not become ready"
                    + (f": {last_error}" if last_error else "")
                )
            session = _CdpSession(websocket_url)
            yield session
        finally:
            if session is not None:
                session.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _runtime_value(response: Mapping[str, Any], *, purpose: str) -> Any:
    if response.get("exceptionDetails"):
        raise RuntimeError(
            f"Browser JavaScript failed during {purpose}: "
            f"{response['exceptionDetails']}"
        )
    result = response.get("result", {})
    if not isinstance(result, Mapping):
        raise RuntimeError(f"Browser returned no JavaScript result during {purpose}")
    if result.get("subtype") == "error":
        raise RuntimeError(
            f"Browser JavaScript failed during {purpose}: "
            f"{result.get('description', 'unknown error')}"
        )
    return result.get("value")


def _evaluate(session: _CdpCommands, expression: str, *, purpose: str) -> Any:
    return _runtime_value(
        session.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        ),
        purpose=purpose,
    )


def _capture_rectangle(
    value: Mapping[str, Any] | None,
    *,
    purpose: str,
) -> CaptureRectangle:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Browser returned no {purpose} rectangle")
    try:
        rectangle = CaptureRectangle(
            x=float(value.get("x", 0)),
            y=float(value.get("y", 0)),
            width=float(value["width"]),
            height=float(value["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Browser returned an invalid {purpose} rectangle: {value!r}"
        ) from exc
    return rectangle


def _layout_content_rectangle(session: _CdpCommands) -> CaptureRectangle:
    metrics = session.command("Page.getLayoutMetrics")
    content = metrics.get("cssContentSize") or metrics.get("contentSize")
    if not isinstance(content, Mapping):
        raise RuntimeError("Browser returned no full-page layout metrics")
    return _capture_rectangle(content, purpose="full-page content")


def _validate_capture_budget(
    rectangle: CaptureRectangle,
    *,
    purpose: str,
) -> None:
    """Reject a browser raster request that exceeds the bounded PNG budget."""

    width = math.ceil(rectangle.width)
    height = math.ceil(rectangle.height)
    pixels = width * height
    if (
        width > MAX_CAPTURE_DIMENSION
        or height > MAX_CAPTURE_DIMENSION
        or pixels > MAX_CAPTURE_PIXELS
    ):
        raise RuntimeError(
            f"{purpose} {width}x{height} exceeds the browser capture safety "
            f"budget ({MAX_CAPTURE_DIMENSION}px per axis and "
            f"{MAX_CAPTURE_PIXELS} total pixels). Author a target or a smaller "
            "explicit crop. No screenshot retained."
        )


def _require_clip_inside(
    clip: CaptureRectangle,
    bounds: CaptureRectangle,
    *,
    purpose: str,
) -> None:
    """Fail closed when a page-coordinate clip escapes its retained bounds."""

    tolerance = 1e-6
    if (
        clip.x < bounds.x - tolerance
        or clip.y < bounds.y - tolerance
        or clip.right > bounds.right + tolerance
        or clip.bottom > bounds.bottom + tolerance
    ):
        raise RuntimeError(
            f"{purpose} ({clip.x:g}, {clip.y:g}, {clip.width:g}, "
            f"{clip.height:g}) falls outside browser bounds "
            f"{bounds.width:g}x{bounds.height:g}. No screenshot retained."
        )


def _target_document_clip(
    target: CaptureRectangle,
    content: CaptureRectangle,
    *,
    viewport_width: int,
    viewport_height: int,
) -> CaptureRectangle:
    """Return an adaptive readable 16:9 viewport centered on ``target``.

    Small paragraph targets receive a half-viewport document window instead of
    being lost inside a full 1920x1080 page. Wider or taller evidence expands
    the window to include the target plus 25% context on each side. The clip
    never exceeds either the authored viewport or the page's content bounds.
    A target larger than that bounded window is ambiguous: silently clipping it
    can omit the exact sentence the author selected, so the capture fails and
    asks for a tighter selector or explicit crop instead.
    """
    if viewport_width <= 0 or viewport_height <= 0:
        raise RuntimeError(
            f"Browser capture viewport is invalid: "
            f"{viewport_width}x{viewport_height}"
        )
    tolerance = 1e-6
    if (
        target.x < content.x - tolerance
        or target.y < content.y - tolerance
        or target.right > content.right + tolerance
        or target.bottom > content.bottom + tolerance
    ):
        raise RuntimeError(
            "Browser resolved a target outside the document content bounds; "
            "no screenshot retained."
        )

    maximum_width = min(float(viewport_width), content.width)
    maximum_height = min(float(viewport_height), content.height)
    largest_clip_width = min(
        maximum_width,
        maximum_height * DOCUMENT_CLIP_ASPECT,
    )
    largest_clip_height = largest_clip_width / DOCUMENT_CLIP_ASPECT
    if largest_clip_width <= 0 or largest_clip_height <= 0:
        raise RuntimeError("Browser returned no usable 16:9 document clip")
    if (
        target.width > largest_clip_width + tolerance
        or target.height > largest_clip_height + tolerance
    ):
        raise RuntimeError(
            f"Browser target {target.width:.2f}x{target.height:.2f} exceeds "
            f"the bounded 16:9 document clip "
            f"{largest_clip_width:.2f}x{largest_clip_height:.2f}; "
            "author a tighter selector "
            "or an explicit crop. No screenshot retained."
        )

    minimum_context_width = (
        largest_clip_width * TARGET_CLIP_MIN_VIEWPORT_FRACTION
    )
    target_context_width = target.width * TARGET_CLIP_CONTEXT_MULTIPLIER
    target_context_height_as_width = (
        target.height
        * TARGET_CLIP_CONTEXT_MULTIPLIER
        * DOCUMENT_CLIP_ASPECT
    )
    clip_width = min(
        largest_clip_width,
        max(
            minimum_context_width,
            target_context_width,
            target_context_height_as_width,
        ),
    )
    clip_height = clip_width / DOCUMENT_CLIP_ASPECT

    ideal_x = target.x + target.width / 2 - clip_width / 2
    ideal_y = target.y + target.height / 2 - clip_height / 2
    maximum_x = content.right - clip_width
    maximum_y = content.bottom - clip_height
    clip_x = min(max(ideal_x, content.x), maximum_x)
    clip_y = min(max(ideal_y, content.y), maximum_y)
    return CaptureRectangle(
        x=round(clip_x, 6),
        y=round(clip_y, 6),
        width=round(clip_width, 6),
        height=round(clip_height, 6),
    )


def _targeting_expression(spec: CaptureSpec) -> str:
    selector = json.dumps(spec.selector)
    text = json.dumps(spec.text)
    scroll = spec.scroll_target
    scroll_value = (
        {"y": scroll.y, "selector": scroll.selector, "text": scroll.text}
        if scroll is not None
        else None
    )
    scroll_json = json.dumps(scroll_value, separators=(",", ":"))
    return f"""
(() => {{
  const __rabbitholeCaptureTarget = true;
  const selector = {selector};
  const textNeedle = {text};
  const scrollTarget = {scroll_json};
  const normalize = value => String(value || "").replace(/\\s+/g, " ").trim();
  const visible = element => {{
    if (!element || !element.getBoundingClientRect) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== "hidden" && style.display !== "none";
  }};
  const findSelector = value => {{
    try {{
      return {{element: document.querySelector(value), error: null}};
    }} catch (error) {{
      return {{element: null, error: `invalid selector ${{value}}: ${{error.message}}`}};
    }}
  }};
  const findText = (root, value) => {{
    const needle = normalize(value);
    const candidates = [root, ...root.querySelectorAll("*")]
      .filter(element => visible(element) &&
        normalize(element.innerText || element.textContent).includes(needle));
    candidates.sort((left, right) => {{
      const a = left.getBoundingClientRect();
      const b = right.getBoundingClientRect();
      return (a.width * a.height) - (b.width * b.height);
    }});
    return candidates[0] || null;
  }};
  let evidence = null;
  if (selector) {{
    const resolved = findSelector(selector);
    if (resolved.error) return {{ok: false, error: resolved.error}};
    if (!resolved.element || !visible(resolved.element)) {{
      return {{ok: false, error: `selector ${{selector}} was not found or visible`}};
    }}
    evidence = resolved.element;
  }}
  if (textNeedle) {{
    if (evidence) {{
      const textEvidence = findText(evidence, textNeedle);
      if (!textEvidence) {{
        return {{
          ok: false,
          error: `text ${{JSON.stringify(textNeedle)}} was not found inside selector ${{selector}}`
        }};
      }}
      evidence = textEvidence;
    }} else {{
      evidence = findText(document.body, textNeedle);
      if (!evidence) {{
        return {{
          ok: false,
          error: `text ${{JSON.stringify(textNeedle)}} was not found`
        }};
      }}
    }}
  }}
  let scrollElement = evidence;
  if (scrollTarget && scrollTarget.selector) {{
    const resolved = findSelector(scrollTarget.selector);
    if (resolved.error) return {{ok: false, error: resolved.error}};
    if (!resolved.element || !visible(resolved.element)) {{
      return {{
        ok: false,
        error: `scroll selector ${{scrollTarget.selector}} was not found or visible`
      }};
    }}
    scrollElement = resolved.element;
  }} else if (scrollTarget && scrollTarget.text) {{
    scrollElement = findText(document.body, scrollTarget.text);
    if (!scrollElement) {{
      return {{
        ok: false,
        error: `scroll text ${{JSON.stringify(scrollTarget.text)}} was not found`
      }};
    }}
  }}
  if (scrollTarget && scrollTarget.y !== null) {{
    window.scrollTo(0, scrollTarget.y);
  }} else if (scrollElement) {{
    scrollElement.scrollIntoView({{block: "center", inline: "center", behavior: "auto"}});
  }}
  const rect = evidence ? evidence.getBoundingClientRect() : null;
  return {{
    ok: true,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    targetRect: rect ? {{
      x: rect.x + window.scrollX,
      y: rect.y + window.scrollY,
      width: rect.width,
      height: rect.height
    }} : null
  }};
}})()
""".strip()


def _prepare_cdp_page(
    session: _CdpCommands, request: BrowserCaptureRequest
) -> None:
    """Navigate once and prepare a loaded page for one or more target captures."""

    session.command("Page.enable")
    session.command("Runtime.enable")
    session.command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": request.width,
            "height": request.height,
            "deviceScaleFactor": 1,
            "mobile": False,
            "screenWidth": request.width,
            "screenHeight": request.height,
        },
    )
    navigation = session.command("Page.navigate", {"url": request.url})
    if navigation.get("errorText"):
        raise RuntimeError(
            f"Browser navigation failed for {request.url!r}: {navigation['errorText']}"
        )
    deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS
    while True:
        ready_state = _evaluate(
            session, "document.readyState", purpose="page readiness"
        )
        if ready_state in ("interactive", "complete"):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Browser page did not become ready for capture: {request.url!r}"
            )
        time.sleep(0.05)
    if request.settle_ms:
        time.sleep(request.settle_ms / 1000.0)


def _capture_loaded_page_with_cdp_session(
    session: _CdpCommands, request: BrowserCaptureRequest
) -> BrowserCaptureResponse:
    """Resolve one authored target and retain its pixels from an open page."""

    targeting = _evaluate(
        session, _targeting_expression(request.spec), purpose="capture targeting"
    )
    if not isinstance(targeting, Mapping) or not targeting.get("ok"):
        reason = (
            targeting.get("error", "target could not be resolved")
            if isinstance(targeting, Mapping)
            else "target script returned no result"
        )
        raise RuntimeError(
            f"Capture target was not found for {request.url!r}: {reason}. "
            "No screenshot retained."
        )
    if request.spec.selector or request.spec.text or request.spec.scroll_target:
        time.sleep(0.1)

    has_evidence_target = bool(request.spec.selector or request.spec.text)
    target: CaptureRectangle | None = None
    if has_evidence_target:
        target = _capture_rectangle(
            targeting.get("targetRect"),
            purpose="resolved evidence target",
        )
    auto_target_clip = bool(target is not None and request.spec.crop is None)

    content: CaptureRectangle | None = None
    clip: CaptureRectangle | None = None
    if request.spec.crop is not None:
        crop = request.spec.crop
        content = _layout_content_rectangle(session)
        if request.spec.full_page:
            clip = CaptureRectangle(
                x=float(crop.x),
                y=float(crop.y),
                width=float(crop.width),
                height=float(crop.height),
            )
            bounds = content
        else:
            try:
                scroll_x = float(targeting.get("scrollX", 0))
                scroll_y = float(targeting.get("scrollY", 0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Browser returned invalid scroll coordinates for explicit "
                    "crop. No screenshot retained."
                ) from exc
            if (
                not math.isfinite(scroll_x)
                or not math.isfinite(scroll_y)
                or scroll_x < 0
                or scroll_y < 0
            ):
                raise RuntimeError(
                    "Browser returned invalid scroll coordinates for explicit "
                    "crop. No screenshot retained."
                )
            viewport = CaptureRectangle(
                x=scroll_x,
                y=scroll_y,
                width=float(request.width),
                height=float(request.height),
            )
            clip = CaptureRectangle(
                x=scroll_x + crop.x,
                y=scroll_y + crop.y,
                width=float(crop.width),
                height=float(crop.height),
            )
            _require_clip_inside(
                clip,
                viewport,
                purpose="Capture crop",
            )
            bounds = content
        _require_clip_inside(clip, bounds, purpose="Capture crop")
        _validate_capture_budget(clip, purpose="Capture crop")
    elif auto_target_clip:
        assert target is not None
        content = _layout_content_rectangle(session)
        clip = _target_document_clip(
            target,
            content,
            viewport_width=request.width,
            viewport_height=request.height,
        )
        _validate_capture_budget(clip, purpose="Targeted document clip")
    elif request.spec.full_page:
        content = _layout_content_rectangle(session)
        _validate_capture_budget(content, purpose="Full-page capture")
        clip = content

    screenshot_params: dict[str, Any] = {
        "format": "png",
        "fromSurface": True,
        "captureBeyondViewport": bool(clip is not None),
    }
    if clip is not None:
        screenshot_params["clip"] = {
            **clip.to_dict(),
            "scale": 1,
        }

    if request.spec.crop is not None:
        framing_mode = "explicit-crop"
    elif auto_target_clip:
        framing_mode = "target"
    elif request.spec.full_page:
        framing_mode = "full-page"
    else:
        framing_mode = "viewport"
    framing = CaptureFraming(
        mode=framing_mode,
        target=target,
        clip=clip,
        content=content,
        authored_crop=request.spec.crop,
    )

    screenshot = session.command("Page.captureScreenshot", screenshot_params)
    encoded = screenshot.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise RuntimeError("Browser returned no PNG data for targeted capture")
    try:
        png = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError("Browser returned invalid screenshot data") from exc
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Browser screenshot data is not a PNG")
    request.out_png.parent.mkdir(parents=True, exist_ok=True)
    request.out_png.write_bytes(png)
    page_source = _evaluate(
        session,
        "document.documentElement ? document.documentElement.outerHTML : ''",
        purpose="rendered DOM inspection",
    )
    return BrowserCaptureResponse(
        page_source=page_source,
        framing=framing,
    )


def _capture_with_cdp_session(
    session: _CdpCommands, request: BrowserCaptureRequest
) -> BrowserCaptureResponse:
    _prepare_cdp_page(session, request)
    return _capture_loaded_page_with_cdp_session(session, request)


def _capture_page_targeted(request: BrowserCaptureRequest) -> BrowserCaptureResponse:
    with _chrome_devtools(
        request.browser, width=request.width, height=request.height
    ) as session:
        return _capture_with_cdp_session(session, request)


@contextmanager
def shared_page_capture() -> Iterator[TargetedCapture]:
    """Yield a targeted capture callback that keeps one exact page loaded.

    The callback is intentionally stricter than a URL-keyed screenshot cache:
    it re-runs DOM targeting and captures fresh pixels for every request.  It
    merely shares the native browser process and the page navigation.  This
    preserves distinct selector/text/scroll/crop authoring while avoiding a
    burst of repeated requests to the same archive snapshot.

    A callback instance accepts one exact URL, browser, and viewport.  Callers
    create a new context for another URL, which prevents accidental cross-page
    reuse and guarantees the headless browser is closed at the end of a batch.
    """

    context = None
    session: _CdpSession | None = None
    identity: tuple[str, str, int, int] | None = None
    loaded = False
    load_failure: Exception | None = None

    def targeted(request: BrowserCaptureRequest) -> str | bytes | None:
        nonlocal context, session, identity, loaded, load_failure
        request_identity = (
            request.url,
            str(request.browser.resolve()),
            request.width,
            request.height,
        )
        if identity is None:
            identity = request_identity
            context = _chrome_devtools(
                request.browser, width=request.width, height=request.height
            )
            session = context.__enter__()
        elif request_identity != identity:
            raise RuntimeError(
                "Shared page capture received a different URL, browser, or "
                "viewport; start a separate shared_page_capture() context."
            )
        assert session is not None
        if not loaded:
            if load_failure is not None:
                raise load_failure
            try:
                _prepare_cdp_page(session, request)
            except Exception as exc:
                # A navigation failure is a batch-level source failure.  Do not
                # hammer the same archive URL again for every remaining slot.
                load_failure = exc
                raise
            # Target resolution may fail for one slot.  Navigation itself has
            # still succeeded, so keep the page for the remaining authored
            # targets rather than hitting the source again.
            loaded = True
        return _capture_loaded_page_with_cdp_session(session, request)

    try:
        yield targeted
    finally:
        if context is not None:
            context.__exit__(None, None, None)


def _apply_capture_crop(path: Path, crop: CaptureCrop) -> None:
    from PIL import Image

    temporary = path.with_name(f".{path.name}.crop.png")
    try:
        with Image.open(path) as image:
            frame_width, frame_height = image.size
            right = crop.x + crop.width
            bottom = crop.y + crop.height
            if right > frame_width or bottom > frame_height:
                raise RuntimeError(
                    f"Capture crop ({crop.x}, {crop.y}, {crop.width}, {crop.height}) "
                    f"falls outside retained frame {frame_width}x{frame_height}. "
                    "No screenshot retained."
                )
            image.crop((crop.x, crop.y, right, bottom)).save(
                temporary, format="PNG"
            )
        os.replace(temporary, path)
    except Exception:
        path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise


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

# A browser's target-centric document clip can legitimately be almost binary:
# black article text on a flat white page often has only 2-5 grey levels.  Such
# a frame fails the broad ``BLANK_DISTINCT_MAX`` rule even though its pixels are
# useful evidence.  The fallback below is intentionally narrow.  It applies
# only when the native browser reports an exact resolved target/clip geometry,
# and it requires page-like background dominance plus several small connected
# ink components and a high boundary-to-ink ratio.  A uniform frame or one
# large failed-embed rectangle therefore remains blank.
TARGETED_TEXT_MIN_BACKGROUND_FRACTION = 0.50
TARGETED_TEXT_MIN_BACKGROUND_LEVEL = 224
TARGETED_TEXT_MIN_INK_FRACTION = 0.002
TARGETED_TEXT_MAX_INK_FRACTION = 0.35
TARGETED_TEXT_MIN_COMPONENTS = 4
TARGETED_TEXT_MIN_STRUCTURED_INK_FRACTION = 0.35
TARGETED_TEXT_MIN_EDGE_PER_INK = 0.20
TARGETED_TEXT_MIN_SHAPE_VARIATION = 0.15
_TARGETED_TEXT_MIN_CONTRAST = 32
_TARGETED_TEXT_MAX_RUNS = 100_000


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


def _target_pixel_region(
    image_size: tuple[int, int], framing: CaptureFraming
) -> tuple[int, int, int, int] | None:
    """Map a resolved page target into its retained screenshot pixels.

    CDP target and clip rectangles use page-coordinate CSS pixels.  The PNG can
    have a different raster size, so mapping by the recorded clip is safer than
    assuming one CSS pixel always equals one image pixel.  A little surrounding
    context is retained so a tightly fitted text element still exposes its page
    background for the ink test.
    """

    target = framing.target
    clip = framing.clip
    if framing.mode != "target" or target is None or clip is None:
        return None

    image_width, image_height = image_size
    if image_width <= 0 or image_height <= 0:
        return None
    scale_x = image_width / clip.width
    scale_y = image_height / clip.height
    left = math.floor((target.x - clip.x) * scale_x)
    top = math.floor((target.y - clip.y) * scale_y)
    right = math.ceil((target.right - clip.x) * scale_x)
    bottom = math.ceil((target.bottom - clip.y) * scale_y)
    if right <= 0 or bottom <= 0 or left >= image_width or top >= image_height:
        return None

    target_width = max(1, right - left)
    target_height = max(1, bottom - top)
    pad_x = max(2, round(target_width * 0.05))
    pad_y = max(2, round(target_height * 0.08))
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(image_width, right + pad_x)
    bottom = min(image_height, bottom + pad_y)
    if right - left < 3 or bottom - top < 3:
        return None
    return left, top, right, bottom


def _ink_component_stats(mask: Any) -> tuple[list[tuple[int, int, int]], int]:
    """Return ``(area, width, height)`` for 8-connected ink components.

    A run-length union-find avoids a Python flood fill over every screenshot
    pixel.  It also gives us a hard run-count ceiling so pathological noise is
    rejected instead of turning a QA check into an expensive image-analysis
    job.  ``mask`` is a two-dimensional NumPy boolean array.
    """

    import numpy as np

    parent: list[int] = []
    runs: list[tuple[int, int, int, int]] = []
    previous: list[tuple[int, int, int]] = []
    rich_rows = 0

    def find(value: int) -> int:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for y, row in enumerate(mask):
        padded = np.pad(row.astype(np.int8, copy=False), (1, 1))
        changes = np.diff(padded)
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1) - 1
        if len(starts) >= TARGETED_TEXT_MIN_COMPONENTS:
            rich_rows += 1

        current: list[tuple[int, int, int]] = []
        previous_index = 0
        for start_value, end_value in zip(starts, ends):
            start = int(start_value)
            end = int(end_value)
            run_id = len(parent)
            parent.append(run_id)
            runs.append((run_id, y, start, end))
            current.append((start, end, run_id))
            if len(runs) > _TARGETED_TEXT_MAX_RUNS:
                return [], rich_rows

            while (
                previous_index < len(previous)
                and previous[previous_index][1] < start - 1
            ):
                previous_index += 1
            overlap_index = previous_index
            while (
                overlap_index < len(previous)
                and previous[overlap_index][0] <= end + 1
            ):
                union(run_id, previous[overlap_index][2])
                overlap_index += 1
        previous = current

    aggregated: dict[int, list[int]] = {}
    for run_id, y, start, end in runs:
        root = find(run_id)
        stats = aggregated.setdefault(root, [0, start, end, y, y])
        stats[0] += end - start + 1
        stats[1] = min(stats[1], start)
        stats[2] = max(stats[2], end)
        stats[3] = min(stats[3], y)
        stats[4] = max(stats[4], y)
    return [
        (area, right - left + 1, bottom - top + 1)
        for area, left, right, top, bottom in aggregated.values()
    ], rich_rows


def _has_targeted_text_structure(
    png: Path,
    capture_spec: CaptureSpec,
    framing: CaptureFraming | None,
) -> bool:
    """Whether a low-palette target crop has pixel structure resembling text.

    DOM resolution grants eligibility for this fallback, never acceptance.
    Acceptance comes only from the retained target pixels: a dominant flat
    background, a bounded amount of contrasting ink, several separate glyph-
    sized components, repeated row structure, and enough edge for thin strokes
    rather than one solid panel.
    """

    if (
        framing is None
        or not (capture_spec.text or capture_spec.selector)
        or capture_spec.crop is not None
    ):
        return False

    from PIL import Image

    import numpy as np

    try:
        with Image.open(png) as image:
            grey_image = image.convert("L")
            region_box = _target_pixel_region(grey_image.size, framing)
            if region_box is None:
                return False
            grey = np.asarray(grey_image.crop(region_box), dtype=np.uint8)
    except Exception:
        return False
    if grey.ndim != 2 or grey.size == 0:
        return False

    histogram = np.bincount(grey.ravel(), minlength=256)
    background = int(histogram.argmax())
    background_fraction = int(histogram[background]) / int(grey.size)
    if (
        background < TARGETED_TEXT_MIN_BACKGROUND_LEVEL
        or background_fraction < TARGETED_TEXT_MIN_BACKGROUND_FRACTION
    ):
        return False

    # This exception is deliberately for dark document text on a bright page.
    # Symmetric contrast would also rescue white error copy on a failed black
    # embed, which is exactly the kind of low-palette capture that must remain
    # fail-closed.
    ink = (
        grey.astype(np.int16)
        <= background - _TARGETED_TEXT_MIN_CONTRAST
    )
    ink_pixels = int(np.count_nonzero(ink))
    if ink_pixels == 0:
        return False
    ink_fraction = ink_pixels / int(grey.size)
    if not (
        TARGETED_TEXT_MIN_INK_FRACTION
        <= ink_fraction
        <= TARGETED_TEXT_MAX_INK_FRACTION
    ):
        return False

    horizontal_edges = int(np.count_nonzero(ink[:, 1:] != ink[:, :-1]))
    vertical_edges = int(np.count_nonzero(ink[1:, :] != ink[:-1, :]))
    edge_per_ink = (horizontal_edges + vertical_edges) / ink_pixels
    if edge_per_ink < TARGETED_TEXT_MIN_EDGE_PER_INK:
        return False

    components, rich_rows = _ink_component_stats(ink)
    height, width = ink.shape
    text_components = [
        (area, component_width, component_height)
        for area, component_width, component_height in components
        if area >= 3
        and component_height >= 2
        and component_width <= max(12, round(width * 0.45))
        and component_height <= max(12, round(height * 0.60))
        and area / (component_width * component_height) < 0.95
    ]
    if len(text_components) < TARGETED_TEXT_MIN_COMPONENTS or rich_rows < 2:
        return False
    structured_ink = sum(area for area, _width, _height in text_components)
    if (
        structured_ink / ink_pixels
        < TARGETED_TEXT_MIN_STRUCTURED_INK_FRACTION
    ):
        return False

    # Identical dot/box grids have many edges and components but do not resemble
    # a line of glyphs.  Rendered text normally varies in component width,
    # height, area, or fill even in a monospace font.  Require modest variation
    # across those pixel shapes rather than accepting component count alone.
    component_shapes = np.asarray(
        [
            (
                component_width,
                component_height,
                area,
                area / (component_width * component_height),
            )
            for area, component_width, component_height in text_components
        ],
        dtype=float,
    )
    relative_spreads = np.ptp(component_shapes, axis=0) / np.maximum(
        np.mean(component_shapes, axis=0), 1e-9
    )
    return bool(float(np.max(relative_spreads)) >= TARGETED_TEXT_MIN_SHAPE_VARIATION)


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
        "cookie-consent wall",
        re.compile(
            r"\bit(?:'|\N{RIGHT SINGLE QUOTATION MARK})s your choice\b"
            r".{0,1600}\b(?:accept all|manage cookies|reject all)\b",
            re.IGNORECASE,
        ),
    ),
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
    framing: CaptureFraming | None = None


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
    spec: CaptureSpec | Mapping[str, Any] | None = None,
    targeted_capture: TargetedCapture | None = None,
) -> CaptureResult:
    """Screenshot a page, using DevTools when authored targeting is requested."""
    # Absolute, always. The browser is a separate process with its own working
    # directory, so a relative path is resolved against *its* cwd rather than
    # ours. Passing `projects/<slug>/assets/.capturework/x.png` produced
    # "Failed to write file ...: The system cannot find the path specified"
    # from Chrome itself, on every slot, while the identical call with an
    # absolute path succeeded -- which is why it looked like a URL problem.
    out_png = Path(out_png).resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    capture_spec = CaptureSpec.from_value(spec)

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

    active = runner or _default_runner
    framing: CaptureFraming | None = None
    if capture_spec.needs_browser_control:
        out_png.unlink(missing_ok=True)
        if runner is not None and targeted_capture is None:
            raise RuntimeError(
                "A targeted capture cannot use the one-shot injected runner. "
                "Pass targeted_capture= for tests, or omit runner to use the "
                "installed browser's DevTools endpoint."
            )
        request = BrowserCaptureRequest(
            url=url,
            out_png=out_png,
            spec=capture_spec,
            width=width,
            height=height,
            settle_ms=settle_ms,
            browser=chosen,
        )
        try:
            targeted_output = (targeted_capture or _capture_page_targeted)(request)
        except Exception:
            out_png.unlink(missing_ok=True)
            raise
        if isinstance(targeted_output, BrowserCaptureResponse):
            browser_stdout = targeted_output.page_source
            framing = targeted_output.framing
        else:
            # Backward-compatible injected test and integration callbacks may
            # return rendered DOM directly. The native CDP path always returns
            # BrowserCaptureResponse with exact target/clip inspection state.
            browser_stdout = targeted_output
        returncode, stderr = 0, b""
    else:
        argv = [
            str(chosen),
            "--headless",
            "--disable-gpu",
            *_browser_sandbox_override_args(),
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--user-agent={CAPTURE_USER_AGENT}",
            f"--virtual-time-budget={settle_ms}",
            f"--window-size={width},{height}",
            f"--screenshot={out_png}",
            url,
        ]
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
    if runner is None and check_obstructions and not capture_spec.needs_browser_control:
        dom_argv = [
            str(chosen),
            "--headless",
            "--disable-gpu",
            *_browser_sandbox_override_args(),
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

    if capture_spec.crop is not None:
        crop_was_clipped_by_browser = bool(
            framing is not None
            and framing.mode == "explicit-crop"
            and framing.clip is not None
        )
        if crop_was_clipped_by_browser:
            from PIL import Image

            try:
                with Image.open(out_png) as image:
                    retained_size = image.size
            except Exception as exc:
                out_png.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Browser explicit crop is not a readable image: {exc}"
                ) from exc
            expected_size = (
                capture_spec.crop.width,
                capture_spec.crop.height,
            )
            if retained_size != expected_size:
                out_png.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Browser explicit crop retained {retained_size[0]}x"
                    f"{retained_size[1]} pixels; expected "
                    f"{expected_size[0]}x{expected_size[1]}. No screenshot "
                    "retained."
                )
        else:
            _apply_capture_crop(out_png, capture_spec.crop)

    inspection = inspect_frame(out_png)
    if check_blank and inspection.looks_blank:
        targeted_text_structure = _has_targeted_text_structure(
            out_png, capture_spec, framing
        )
        if not targeted_text_structure:
            out_png.unlink(missing_ok=True)
            raise RuntimeError(
                f"Captured {url!r} but the frame is blank -- most likely a login wall, "
                f"an error page, or a render that never painted. Not recorded; a blank "
                f"frame in the timeline is worse than a known gap "
                f"(grey stddev {inspection.grey_stddev:.2f}, "
                f"{inspection.distinct_grey_levels} distinct levels)."
            )

    return CaptureResult(
        path=out_png,
        kind="page",
        warnings=tuple(warnings),
        inspection=inspection,
        framing=framing,
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
    spec: CaptureSpec | Mapping[str, Any] | None = None,
    targeted_capture: TargetedCapture | None = None,
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
    capture_spec = CaptureSpec.from_value(spec)

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
        if capture_spec != CaptureSpec():
            raise ValueError(
                "Browser CaptureSpec targeting cannot be applied to a PDF document"
            )
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
                framing=result.framing,
            )
    else:
        result = capture_page(
            url, still, width=width, height=height,
            runner=runner, browser=browser, check_blank=check_blank,
            page_source=body,
            require_content_text=(quality == "final"),
            spec=capture_spec,
            targeted_capture=targeted_capture,
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
        framing=result.framing,
    )
