from datetime import datetime, timezone
from pathlib import Path

import pytest

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
    def __init__(self, result=(0, b"", b"")):
        self.result = result
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return self.result



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


def test_runner_argv_includes_out_path_and_no_playlist(tmp_path):
    claims = [_claim("c1", sources=(URL,))]
    runner = _RecordingRunner()
    out_path = tmp_path / "out.mp4"

    fetch_primary(URL, out_path, claims, runner=runner, prober=_is_video)

    argv = runner.calls[0]
    assert str(out_path) in argv
    assert "--no-playlist" in argv


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
