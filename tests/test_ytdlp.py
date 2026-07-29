from datetime import datetime, timezone
from pathlib import Path

import pytest

import rabbithole.sources.ytdlp as ytdlp_module
from rabbithole.provenance import AssetRecord
from rabbithole.sources.ytdlp import claim_citing, fetch_primary, normalize_url

URL = "https://www.youtube.com/watch?v=abc123"


def _claim(claim_id="c1", sources=(URL,), **overrides):
    data = {
        "claim_id": claim_id,
        "text": "A claim",
        "confidence": "documented",
        "sources": list(sources),
    }
    data.update(overrides)
    return data


def _ok_runner(argv):
    return (0, b"stdout output", b"")


class _RecordingRunner:
    def __init__(self, result=(0, b"", b""), *, write_output=True):
        self.result = result
        self.write_output = write_output
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.result[0] == 0 and self.write_output:
            output = Path(argv[argv.index("-o") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"stand-in video")
        return self.result


class _TimedOutProcess:
    def __init__(self, *, pid=4321):
        self.pid = pid
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.communicate_calls = 0
        self.wait_calls = []
        self.kill_calls = 0
        self.terminate_calls = 0

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise ytdlp_module.subprocess.TimeoutExpired(
                cmd=["yt-dlp"],
                timeout=timeout,
                output=b"partial stdout",
                stderr=b"partial stderr",
            )
        return b"complete stdout", b"complete stderr"

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.returncode = -9
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15



def _is_video(_path):
    """Stand-in prober: these tests are about the ledger gate and the record
    shape, not about whether the bytes decode. The video check has its own
    tests below."""
    return True

# --- normalize_url ---------------------------------------------------------


def test_normalize_url_strips_whitespace_and_one_trailing_slash():
    assert normalize_url("  https://example.com/video/  ") == "https://example.com/video"


def test_normalize_url_strips_only_a_single_trailing_slash():
    assert normalize_url("https://example.com/video//") == "https://example.com/video/"


def test_normalize_url_preserves_query_parameters_and_case_in_path():
    raw = "https://www.YouTube.com/watch?v=AbC123"
    assert normalize_url(raw) == raw


def test_normalize_url_with_no_trailing_slash_is_unchanged():
    assert normalize_url("https://example.com/video") == "https://example.com/video"


# --- claim_citing ------------------------------------------------------


def test_claim_citing_finds_a_claim_citing_the_url():
    claims = [_claim("c1", sources=(URL,))]

    result = claim_citing(URL, claims)

    assert result is not None
    assert result["claim_id"] == "c1"


def test_claim_citing_matches_despite_a_trailing_slash_difference():
    claims = [_claim("c1", sources=(URL + "/",))]

    result = claim_citing(URL, claims)

    assert result is not None
    assert result["claim_id"] == "c1"


def test_claim_citing_returns_none_when_nothing_cites_it():
    claims = [_claim("c1", sources=("https://example.com/other",))]

    assert claim_citing(URL, claims) is None


def test_claim_citing_tolerates_a_claim_with_no_sources_key():
    claims = [{"claim_id": "c1", "text": "x", "confidence": "documented"}]

    assert claim_citing(URL, claims) is None


def test_claim_citing_tolerates_sources_being_a_string_rather_than_a_list():
    claims = [{"claim_id": "c1", "text": "x", "confidence": "documented", "sources": URL}]

    assert claim_citing(URL, claims) is None


def test_claim_citing_does_not_match_on_a_url_prefix():
    # Cited URL is the longer one; requested URL is a strict prefix of it.
    cited = "https://youtube.com/watch?v=abc123"
    requested = "https://youtube.com/watch?v=abc"
    claims = [_claim("c1", sources=(cited,))]

    assert claim_citing(requested, claims) is None


def test_claim_citing_does_not_match_when_requested_is_the_longer_superstring():
    # And the reverse direction: requested is a superstring of the cited URL.
    cited = "https://youtube.com/watch?v=abc"
    requested = "https://youtube.com/watch?v=abc123"
    claims = [_claim("c1", sources=(cited,))]

    assert claim_citing(requested, claims) is None


def test_claim_citing_returns_first_matching_claim():
    claims = [_claim("c1", sources=("https://x/other",)), _claim("c2", sources=(URL,))]

    result = claim_citing(URL, claims)

    assert result["claim_id"] == "c2"


# --- fetch_primary: the refusal gate ---------------------------------------


def test_fetch_primary_raises_when_ledger_is_empty_naming_the_url(tmp_path):
    with pytest.raises(RuntimeError, match="abc123"):
        fetch_primary(URL, tmp_path / "out.mp4", [], runner=_ok_runner, prober=_is_video)


def test_fetch_primary_refusal_message_states_how_many_claims_were_checked(tmp_path):
    claims = [_claim("c1", sources=("https://x/other-1",)), _claim("c2", sources=("https://x/other-2",))]

    with pytest.raises(RuntimeError, match="2"):
        fetch_primary(URL, tmp_path / "out.mp4", claims, runner=_ok_runner, prober=_is_video)


def test_fetch_primary_does_not_invoke_the_runner_when_refused(tmp_path):
    runner = _RecordingRunner()

    with pytest.raises(RuntimeError):
        fetch_primary(URL, tmp_path / "out.mp4", [], runner=runner, prober=_is_video)

    assert runner.calls == []


# --- fetch_primary: success path -------------------------------------------


def test_fetch_primary_succeeds_when_a_claim_cites_the_url_and_invokes_runner_once(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()

    fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, prober=_is_video)

    assert len(runner.calls) == 1


def test_runner_argv_uses_an_isolated_attempt_path_and_no_playlist(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()
    out_path = tmp_path / "out.mp4"
    probed = []

    def prober(path):
        probed.append(path)
        return path.read_bytes() == b"stand-in video"

    fetch_primary(URL, out_path, claims, runner=runner, prober=prober)

    argv = runner.calls[0]
    attempt_path = Path(argv[argv.index("-o") + 1])
    assert attempt_path != out_path
    assert attempt_path.name == "download.mp4"
    assert attempt_path.parent.parent == tmp_path
    assert not attempt_path.parent.exists()
    assert out_path.read_bytes() == b"stand-in video"
    assert probed == [attempt_path]
    assert "--no-playlist" in argv
    assert argv[argv.index("--socket-timeout") + 1] == "30"
    assert argv[argv.index("--retries") + 1] == "3"
    assert argv[argv.index("--format") + 1] == ytdlp_module.PORTABLE_VIDEO_FORMAT
    assert argv[argv.index("--merge-output-format") + 1] == "mp4"
    assert argv[argv.index("--remux-video") + 1] == "mp4"


def test_failed_attempt_cleans_fragments_and_preserves_existing_final(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(b"previous validated file")
    observed = {}

    def fragmented_failure(argv):
        attempt = Path(argv[argv.index("-o") + 1])
        observed["attempt_dir"] = attempt.parent
        attempt.parent.mkdir(parents=True, exist_ok=True)
        attempt.with_suffix(".mp4.part").write_bytes(b"partial")
        (attempt.parent / "download.f137.mp4").write_bytes(b"video fragment")
        (attempt.parent / "download.f140.m4a").write_bytes(b"audio fragment")
        return 1, b"", b"merge failed"

    with pytest.raises(RuntimeError, match="merge failed"):
        fetch_primary(
            URL,
            out_path,
            claims,
            runner=fragmented_failure,
            prober=_is_video,
        )

    assert out_path.read_bytes() == b"previous validated file"
    assert not observed["attempt_dir"].exists()


def test_zero_exit_without_an_output_is_refused_and_cleaned(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner(write_output=False)
    out_path = tmp_path / "out.mp4"

    with pytest.raises(RuntimeError, match="wrote no non-empty MP4"):
        fetch_primary(URL, out_path, claims, runner=runner, prober=_is_video)

    attempt_path = Path(runner.calls[0][runner.calls[0].index("-o") + 1])
    assert not attempt_path.parent.exists()
    assert not out_path.exists()


def test_portable_format_prefers_h264_aac_mp4_and_allows_silent_video():
    selector = ytdlp_module.PORTABLE_VIDEO_FORMAT

    assert "[ext=mp4]" in selector
    assert "[vcodec^=avc1]" in selector
    assert "[ext=m4a][acodec^=mp4a]" in selector
    assert selector.endswith("bv[ext=mp4][vcodec^=avc1]")


def test_default_runner_uses_an_isolated_posix_group_and_terminates_it_on_timeout(
    monkeypatch,
):
    process = _TimedOutProcess()
    observed = {}

    def popen(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return process

    def terminate_tree(value):
        observed["terminated"] = value
        value.returncode = -15

    monkeypatch.setattr(ytdlp_module, "_is_windows", lambda: False)
    monkeypatch.setattr(ytdlp_module.subprocess, "Popen", popen)
    monkeypatch.setattr(ytdlp_module, "_terminate_process_tree", terminate_tree)

    returncode, stdout, stderr = ytdlp_module._default_runner(["yt-dlp"])

    assert returncode == 124
    assert stdout == b"complete stdout"
    assert b"process timeout" in stderr
    assert observed["terminated"] is process
    assert observed["kwargs"]["start_new_session"] is True
    assert "creationflags" not in observed["kwargs"]


def test_windows_launch_uses_a_new_hidden_process_group(monkeypatch):
    observed = {}

    class CompletedProcess:
        returncode = 0

        def communicate(self, timeout=None):
            observed["timeout"] = timeout
            return b"ok", b""

    def popen(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return CompletedProcess()

    monkeypatch.setattr(ytdlp_module, "_is_windows", lambda: True)
    monkeypatch.setattr(ytdlp_module.subprocess, "Popen", popen)

    returncode, stdout, stderr = ytdlp_module._default_runner(["yt-dlp"])

    assert (returncode, stdout, stderr) == (0, b"ok", b"")
    assert observed["kwargs"]["creationflags"] & (
        ytdlp_module._WINDOWS_CREATE_NEW_PROCESS_GROUP
    )
    assert observed["kwargs"]["creationflags"] & (
        ytdlp_module._WINDOWS_CREATE_NO_WINDOW
    )
    assert "start_new_session" not in observed["kwargs"]


def test_posix_timeout_escalates_from_term_to_kill_for_the_whole_group(
    monkeypatch,
):
    process = _TimedOutProcess(pid=6789)
    signals = []

    monkeypatch.setattr(ytdlp_module, "PROCESS_TERMINATION_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        ytdlp_module.os,
        "killpg",
        lambda process_group, selected_signal: signals.append(
            (process_group, selected_signal)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        ytdlp_module,
        "_posix_process_group_exists",
        lambda _process_group: True,
    )

    ytdlp_module._terminate_posix_process_tree(process)

    assert signals == [
        (6789, ytdlp_module._POSIX_SIGTERM),
        (6789, ytdlp_module._POSIX_SIGKILL),
    ]
    assert process.wait_calls == [0]


def test_windows_timeout_uses_taskkill_tree_and_force_flags(monkeypatch):
    process = _TimedOutProcess(pid=2468)
    observed = {}

    class TaskkillResult:
        returncode = 0
        stderr = b""

    def run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        process.returncode = -9
        return TaskkillResult()

    monkeypatch.setattr(ytdlp_module.subprocess, "run", run)

    ytdlp_module._terminate_windows_process_tree(process)

    assert observed["argv"][1:] == ["/PID", "2468", "/T", "/F"]
    taskkill_path = observed["argv"][0].replace("/", "\\").lower()
    assert taskkill_path.endswith("system32\\taskkill.exe")
    assert observed["kwargs"]["creationflags"] == (
        ytdlp_module._WINDOWS_CREATE_NO_WINDOW
    )
    assert process.wait_calls == [ytdlp_module.PROCESS_TERMINATION_GRACE_SECONDS]


def test_default_timeout_cleans_attempt_fragments_and_preserves_final(
    tmp_path,
    monkeypatch,
):
    claims = [_claim("c1", sources=(URL,))]
    out_path = tmp_path / "out.mp4"
    out_path.write_bytes(b"previous validated file")
    process = _TimedOutProcess()
    observed = {}

    def popen(argv, **_kwargs):
        attempt = Path(argv[argv.index("-o") + 1])
        observed["attempt_dir"] = attempt.parent
        attempt.with_suffix(".mp4.part").write_bytes(b"partial")
        (attempt.parent / "download.f137.mp4").write_bytes(b"video fragment")
        return process

    def terminate_tree(value):
        observed["terminated"] = value
        value.returncode = -15

    monkeypatch.setattr(ytdlp_module.subprocess, "Popen", popen)
    monkeypatch.setattr(ytdlp_module, "_terminate_process_tree", terminate_tree)

    with pytest.raises(RuntimeError, match="process timeout"):
        fetch_primary(URL, out_path, claims, prober=_is_video)

    assert observed["terminated"] is process
    assert out_path.read_bytes() == b"previous validated file"
    assert not observed["attempt_dir"].exists()


def test_fetch_primary_raises_with_stderr_detail_on_non_zero_return(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner(result=(1, b"", b"ERROR: video unavailable, blocked in your region"))

    with pytest.raises(RuntimeError, match="blocked in your region"):
        fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, prober=_is_video)


def test_returned_record_has_tier_primary_and_every_asset_record_field(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()
    out_path = tmp_path / "out.mp4"

    fields = fetch_primary(URL, out_path, claims, runner=runner, prober=_is_video)

    assert fields["tier"] == "primary"
    # Must actually build an AssetRecord -- not just look plausible.
    record = AssetRecord(**fields)
    assert record.tier == "primary"
    assert record.original_url == URL
    assert record.local_path == str(out_path)


def test_returned_record_notes_names_the_authorising_claim_id(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()

    fields = fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, prober=_is_video)

    assert "c1" in fields["notes"]


def test_provider_is_derived_from_the_url_host(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()

    fields = fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, prober=_is_video)

    assert fields["provider"] == "www.youtube.com"


def test_injected_now_pins_retrieved_at(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()
    pinned = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    fields = fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, now=pinned, prober=_is_video)

    assert fields["retrieved_at"] == "2020-01-02T03:04:05Z"


def test_fetch_primary_normalizes_url_before_gating(tmp_path):
    # Ledger cites the URL with a trailing slash; request omits it.
    claims = [_claim("c1", sources=(URL + "/",))]
    runner = _RecordingRunner()

    fields = fetch_primary(URL, tmp_path / "out.mp4", claims, runner=runner, prober=_is_video)

    assert fields["original_url"] == URL

# --- what came back must actually be video ------------------------------


def _writing_runner(argv):
    """A runner that succeeds and leaves a file, like yt-dlp's generic extractor
    does when handed a page or a document."""
    Path(argv[argv.index("-o") + 1]).write_bytes(b"%PDF-1.7 not a video")
    return 0, b"", b""


def test_a_document_saved_as_mp4_is_refused(tmp_path):
    """yt-dlp's generic extractor exits 0 for pages and documents, so the exit
    code cannot distinguish a retrieved clip from a retrieved PDF. Two
    government-PDF URLs in a production episode produced files beginning
    `%PDF-1.7` named `.mp4` and were recorded as primary video assets; it
    surfaced later as ffmpeg's "moov atom not found", mid-render."""
    claims = [_claim("c1", sources=(URL,))]
    with pytest.raises(RuntimeError, match="no decodable video stream"):
        fetch_primary(URL, tmp_path / "out.mp4", claims,
                      runner=_writing_runner, prober=lambda p: False)


def test_a_refused_download_leaves_no_file_behind(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    out = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError):
        fetch_primary(URL, out, claims, runner=_writing_runner, prober=lambda p: False)
    assert not out.exists()


def test_the_refusal_explains_the_generic_extractor():
    """A bare "not video" would leave an author guessing; the cause is specific
    and worth naming."""
    claims = [_claim("c1", sources=(URL,))]
    with pytest.raises(RuntimeError, match="generic extractor"):
        fetch_primary(URL, Path("unused.mp4"), claims,
                      runner=_writing_runner, prober=lambda p: False)


def test_real_video_is_still_recorded(tmp_path):
    """The check must not break the case it guards."""
    claims = [_claim("c1", sources=(URL,))]
    record = fetch_primary(URL, tmp_path / "out.mp4", claims,
                           runner=_writing_runner, prober=lambda p: True)
    assert record["tier"] == "primary"
    assert record["notes"] == "Authorised by claim 'c1'"


def test_the_default_prober_rejects_a_pdf(tmp_path):
    """Exercises the real ffprobe path, not an injected stand-in."""
    from rabbithole.sources.ytdlp import _default_prober

    pdf = tmp_path / "doc.mp4"
    pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 400)
    assert _default_prober(pdf) is False
