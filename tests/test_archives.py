import json
from datetime import datetime, timezone

import pytest

from rabbithole.provenance import AssetRecord
from rabbithole.sources.archives import (
    UNKNOWN_LICENSE,
    SearchHit,
    download,
    resolve_media_url,
    search_archive_org,
    search_archives,
    search_wikimedia,
    to_record_fields,
    is_usable_mediatype,
    refine_query,
    _pick_derivative,
    MAX_DERIVATIVE_BYTES,
    commons_file,
)


def _transport_for(url_to_response):
    """Build a fake Transport from a dict of {substring: (status, body)}.

    Matching is by substring-in-url so tests don't need to hand-build the
    exact query string the module generates.
    """

    def transport(url):
        for substring, response in url_to_response.items():
            if substring in url:
                return response
        raise AssertionError(f"no fake response registered for url: {url}")

    return transport


def _archive_org_body(docs):
    return json.dumps({"response": {"docs": docs}}).encode("utf-8")


def _metadata_body(files, identifier="id1"):
    return json.dumps(
        {
            "files": files,
            "server": "dn790000.ca.archive.org",
            "dir": f"/3/items/{identifier}",
            "metadata": {"identifier": identifier},
        }
    ).encode("utf-8")


def _wikimedia_body(pages):
    return json.dumps({"query": {"pages": pages}}).encode("utf-8")


def _hit(identifier="id1", media_url="", license="publicdomain", mediatype="movies"):
    return SearchHit(
        "archive_org",
        identifier,
        "t",
        f"https://archive.org/details/{identifier}",
        media_url,
        license,
        mediatype,
    )


# --- SearchHit.is_usable -----------------------------------------------


def test_is_usable_false_for_unknown_license():
    hit = SearchHit("archive_org", "id1", "t", "https://x", "https://x/f.mp4", UNKNOWN_LICENSE)
    assert hit.is_usable is False


def test_is_usable_false_for_empty_license():
    hit = SearchHit("archive_org", "id1", "t", "https://x", "https://x/f.mp4", "")
    assert hit.is_usable is False


def test_is_usable_true_for_a_real_license():
    hit = SearchHit("archive_org", "id1", "t", "https://x", "https://x/f.mp4", "publicdomain")
    assert hit.is_usable is True


def test_is_usable_false_for_empty_media_url_even_with_known_license():
    # A hit fresh out of search_archive_org: licensed, but not yet resolved
    # to a real file. It must not be usable until resolve_media_url runs.
    hit = SearchHit("archive_org", "id1", "t", "https://x", "", "publicdomain")
    assert hit.is_usable is False


def test_is_usable_true_when_licensed_and_resolved():
    hit = SearchHit(
        "archive_org", "id1", "t", "https://x",
        "https://archive.org/download/id1/id1.mp4", "publicdomain",
    )
    assert hit.is_usable is True


# --- search_archive_org ---------------------------------------------------


def test_archive_org_search_parses_identifier_title_and_urls():
    docs = [
        {
            "identifier": "duck_and_cover_1951",
            "title": "Duck and Cover",
            "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
            "collection": ["prelinger"],
            "mediatype": "movies",
        }
    ]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("civil defense", transport)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.provider == "archive_org"
    assert hit.identifier == "duck_and_cover_1951"
    assert hit.title == "Duck and Cover"
    assert hit.original_url == "https://archive.org/details/duck_and_cover_1951"
    # advancedsearch.php carries no file list at all, and the old synthesised
    # "<identifier>.mp4" guess was the bug: search must not claim a filename
    # it cannot know. media_url is only filled in by resolve_media_url.
    assert hit.media_url == ""
    assert hit.mediatype == "movies"


def test_archive_org_doc_with_licenseurl_yields_that_license():
    docs = [
        {
            "identifier": "id1",
            "title": "t",
            "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
            "collection": ["opensource_movies"],
            "mediatype": "movies",
        }
    ]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits[0].license == "https://creativecommons.org/licenses/by/4.0/"


def test_archive_org_doc_in_prelinger_collection_yields_publicdomain():
    docs = [{"identifier": "id1", "title": "t", "collection": ["prelinger"], "mediatype": "movies"}]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits[0].license == "publicdomain"


def test_archive_org_prelinger_detected_when_not_first_in_collection_list():
    """Adversarial case for the collection-membership check.

    Real archive.org docs carry `prelinger` alongside dozens of `fav-*`
    entries, not necessarily first. The check must scan the whole list
    rather than indexing position 0.
    """
    docs = [
        {
            "identifier": "id1",
            "title": "t",
            "collection": ["fav-someuser", "fav-otheruser", "prelinger", "fav-thirduser"],
            "mediatype": "movies",
        }
    ]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits[0].license == "publicdomain"


def test_archive_org_doc_with_neither_yields_unknown_and_unusable():
    docs = [{"identifier": "id1", "title": "t", "collection": ["opensource_movies"], "mediatype": "movies"}]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits[0].license == UNKNOWN_LICENSE
    assert hits[0].is_usable is False


def test_archive_org_search_excludes_doc_with_non_movies_mediatype():
    docs = [{"identifier": "id1", "title": "t", "collection": ["prelinger"], "mediatype": "texts"}]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits == []


def test_archive_org_search_excludes_doc_with_missing_mediatype():
    docs = [{"identifier": "id1", "title": "t", "collection": ["prelinger"]}]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert hits == []


def test_archive_org_malformed_response_missing_response_key_yields_empty_list():
    transport = _transport_for({"advancedsearch.php": (200, json.dumps({"weird": True}).encode())})

    hits = search_archive_org("q", transport)

    assert hits == []


def test_archive_org_honours_limit():
    docs = [
        {"identifier": f"id{i}", "title": "t", "collection": ["prelinger"], "mediatype": "movies"}
        for i in range(20)
    ]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport, limit=3)

    assert len(hits) == 3


def test_archive_org_raises_on_non_200():
    transport = _transport_for({"advancedsearch.php": (500, b"server error")})

    with pytest.raises(RuntimeError):
        search_archive_org("q", transport)


def test_archive_org_doc_missing_identifier_is_skipped_not_crashed():
    docs = [
        {"title": "no id here"},
        {"identifier": "id1", "title": "t", "collection": ["prelinger"], "mediatype": "movies"},
    ]
    transport = _transport_for({"advancedsearch.php": (200, _archive_org_body(docs))})

    hits = search_archive_org("q", transport)

    assert len(hits) == 1
    assert hits[0].identifier == "id1"


# --- resolve_media_url ------------------------------------------------------


def test_resolve_media_url_prefers_h264_over_hires_mpeg4():
    files = [
        {"name": "id1_edit.mp4", "format": "HiRes MPEG4", "size": 14909115},
        {"name": "id1.mp4", "format": "h.264", "size": 6338894},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1.mp4"


def test_resolve_media_url_falls_back_to_hires_mpeg4_when_no_h264():
    files = [
        {"name": "id1_edit.mp4", "format": "HiRes MPEG4", "size": 14909115},
        {"name": "id1.mpeg", "format": "MPEG2", "size": 28079367},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1_edit.mp4"


def test_resolve_media_url_picks_larger_file_among_same_format():
    files = [
        {"name": "id1_small.mp4", "format": "h.264", "size": 1000},
        {"name": "id1_big.mp4", "format": "h.264", "size": 9999999},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1_big.mp4"


def test_resolve_media_url_skips_thumbs_directory_file():
    files = [
        # Absurdly "large" so a size-only comparison would wrongly pick it.
        {"name": "id1.thumbs/id1_000001.jpg", "format": "h.264", "size": 999999999},
        {"name": "id1.mp4", "format": "h.264", "size": 6338894},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1.mp4"


def test_resolve_media_url_handles_string_size():
    files = [
        {"name": "id1_small.mp4", "format": "h.264", "size": "1000"},
        {"name": "id1_big.mp4", "format": "h.264", "size": "9999999"},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1_big.mp4"


def test_resolve_media_url_treats_missing_size_as_zero_not_raising():
    files = [
        {"name": "id1_nosize.mp4", "format": "h.264"},
        {"name": "id1_sized.mp4", "format": "h.264", "size": 500},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1_sized.mp4"


def test_resolve_media_url_treats_unparseable_size_as_zero_not_raising():
    files = [
        {"name": "id1_bad.mp4", "format": "h.264", "size": "not-a-number"},
        {"name": "id1_good.mp4", "format": "h.264", "size": 500},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})

    resolved = resolve_media_url(_hit(), transport)

    assert resolved.media_url == "https://archive.org/download/id1/id1_good.mp4"


def test_resolve_media_url_returns_unchanged_when_no_preferred_format_matches():
    files = [
        {"name": "id1.avi", "format": "Cinepack", "size": 3366582},
        {"name": "id1.rm", "format": "RealMedia", "size": 1000},
    ]
    transport = _transport_for({"archive.org/metadata/id1": (200, _metadata_body(files))})
    hit = _hit()

    resolved = resolve_media_url(hit, transport)

    assert resolved.media_url == ""
    assert resolved == hit
    assert resolved.is_usable is False


def test_resolve_media_url_raises_on_non_200_and_names_identifier():
    transport = _transport_for({"archive.org/metadata/id1": (404, b"not found")})

    with pytest.raises(RuntimeError, match="id1"):
        resolve_media_url(_hit(), transport)


def test_resolve_media_url_returns_exact_download_url_form():
    files = [{"name": "Cheerios1960.mp4", "format": "h.264", "size": 6338894}]
    transport = _transport_for(
        {"archive.org/metadata/Cheerios1960": (200, _metadata_body(files, identifier="Cheerios1960"))}
    )
    hit = _hit(identifier="Cheerios1960")

    resolved = resolve_media_url(hit, transport)

    assert resolved.media_url == "https://archive.org/download/Cheerios1960/Cheerios1960.mp4"


# --- search_wikimedia ------------------------------------------------------


def test_wikimedia_search_parses_title_urls_and_license():
    pages = {
        "123": {
            "pageid": 123,
            "title": "File:Operation Cue 1955.ogv",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/commons/Operation_Cue_1955.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Operation_Cue_1955.ogv",
                    "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
                }
            ],
        }
    }
    transport = _transport_for({"commons.wikimedia.org": (200, _wikimedia_body(pages))})

    hits = search_wikimedia("civil defense", transport)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.provider == "wikimedia"
    assert hit.title == "File:Operation Cue 1955.ogv"
    assert hit.original_url == "https://commons.wikimedia.org/wiki/File:Operation_Cue_1955.ogv"
    assert hit.media_url == "https://upload.wikimedia.org/commons/Operation_Cue_1955.ogv"
    assert hit.license == "Public domain"


def test_wikimedia_page_missing_imageinfo_is_skipped_without_exception():
    pages = {
        "1": {"pageid": 1, "title": "File:NoInfo.ogv"},
        "2": {
            "pageid": 2,
            "title": "File:HasInfo.ogv",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/x.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:HasInfo.ogv",
                    "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}},
                }
            ],
        },
    }
    transport = _transport_for({"commons.wikimedia.org": (200, _wikimedia_body(pages))})

    hits = search_wikimedia("q", transport)

    assert len(hits) == 1
    assert hits[0].title == "File:HasInfo.ogv"


def test_wikimedia_page_missing_license_short_name_yields_unknown():
    pages = {
        "1": {
            "pageid": 1,
            "title": "File:NoLicense.ogv",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/x.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:NoLicense.ogv",
                    "extmetadata": {},
                }
            ],
        }
    }
    transport = _transport_for({"commons.wikimedia.org": (200, _wikimedia_body(pages))})

    hits = search_wikimedia("q", transport)

    assert hits[0].license == UNKNOWN_LICENSE


def test_wikimedia_malformed_response_missing_query_key_yields_empty_list():
    transport = _transport_for({"commons.wikimedia.org": (200, json.dumps({"weird": True}).encode())})

    hits = search_wikimedia("q", transport)

    assert hits == []


def test_wikimedia_raises_on_non_200():
    transport = _transport_for({"commons.wikimedia.org": (503, b"unavailable")})

    with pytest.raises(RuntimeError):
        search_wikimedia("q", transport)


def test_wikimedia_honours_limit():
    pages = {
        str(i): {
            "pageid": i,
            "title": f"File:{i}.ogv",
            "imageinfo": [
                {
                    "url": f"https://upload.wikimedia.org/{i}.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{i}.ogv",
                    "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
                }
            ],
        }
        for i in range(20)
    }
    transport = _transport_for({"commons.wikimedia.org": (200, _wikimedia_body(pages))})

    hits = search_wikimedia("q", transport, limit=4)

    assert len(hits) == 4


# --- search_archives --------------------------------------------------


def test_search_archives_returns_archive_org_hits_before_wikimedia():
    archive_docs = [{"identifier": "aid1", "title": "a", "collection": ["prelinger"], "mediatype": "movies"}]
    wiki_pages = {
        "1": {
            "pageid": 1,
            "title": "File:w.ogv",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/w.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:w.ogv",
                    "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
                }
            ],
        }
    }
    transport = _transport_for(
        {
            "advancedsearch.php": (200, _archive_org_body(archive_docs)),
            "commons.wikimedia.org": (200, _wikimedia_body(wiki_pages)),
        }
    )

    hits, failures = search_archives("q", transport)

    assert failures == []
    assert [h.provider for h in hits] == ["archive_org", "wikimedia"]


def test_search_archives_surfaces_a_provider_failure_without_aborting_the_other():
    wiki_pages = {
        "1": {
            "pageid": 1,
            "title": "File:w.ogv",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/w.ogv",
                    "mime": "image/jpeg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:w.ogv",
                    "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
                }
            ],
        }
    }
    transport = _transport_for(
        {
            "advancedsearch.php": (500, b"server error"),
            "commons.wikimedia.org": (200, _wikimedia_body(wiki_pages)),
        }
    )

    hits, failures = search_archives("q", transport)

    assert [h.provider for h in hits] == ["wikimedia"]
    assert len(failures) == 1
    assert "archive_org" in failures[0]


# --- download ------------------------------------------------------------


def test_download_writes_body_to_disk(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", "publicdomain")
    transport = _transport_for({"id1.mp4": (200, b"fake video bytes")})
    out = tmp_path / "sub" / "id1.mp4"

    result = download(hit, out, transport)

    assert result == out
    assert out.read_bytes() == b"fake video bytes"


def test_download_raises_when_unusable_and_names_license(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", UNKNOWN_LICENSE)
    transport = _transport_for({"id1.mp4": (200, b"bytes")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError, match=UNKNOWN_LICENSE):
        download(hit, out, transport)
    assert "id1" in str(_last_error(download, hit, out, transport))


def _last_error(fn, *args):
    try:
        fn(*args)
    except RuntimeError as exc:
        return exc
    return None


def test_download_raises_on_non_200(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", "publicdomain")
    transport = _transport_for({"id1.mp4": (404, b"")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError):
        download(hit, out, transport)


def test_download_raises_on_empty_body(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", "publicdomain")
    transport = _transport_for({"id1.mp4": (200, b"")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError):
        download(hit, out, transport)


def test_download_leaves_no_partial_file_when_it_raises(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", "publicdomain")
    transport = _transport_for({"id1.mp4": (500, b"")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError):
        download(hit, out, transport)

    assert not out.exists()


def test_download_leaves_no_partial_file_when_unusable(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", UNKNOWN_LICENSE)
    transport = _transport_for({"id1.mp4": (200, b"bytes")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError):
        download(hit, out, transport)

    assert not out.exists()


def test_download_refuses_unresolved_but_licensed_hit_with_message_distinct_from_unlicensed(tmp_path):
    # Licensed (publicdomain) but never resolved: media_url is still "".
    # download must refuse, and the message must not be mistakeable for the
    # "no licence" case -- a caller catching this needs to know whether to
    # go fix the licence or go call resolve_media_url.
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "", "publicdomain")
    transport = _transport_for({})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError) as excinfo:
        download(hit, out, transport)

    message = str(excinfo.value)
    assert "id1" in message
    assert "resolved" in message.lower()
    assert "licence" not in message.lower()
    assert not out.exists()


def test_download_refuses_unlicensed_hit_with_message_distinct_from_unresolved(tmp_path):
    hit = SearchHit("archive_org", "id1", "t", "https://x/details/id1", "https://x/id1.mp4", UNKNOWN_LICENSE)
    transport = _transport_for({"id1.mp4": (200, b"bytes")})
    out = tmp_path / "id1.mp4"

    with pytest.raises(RuntimeError) as excinfo:
        download(hit, out, transport)

    message = str(excinfo.value)
    assert "id1" in message
    assert "licence" in message.lower()
    assert "resolved" not in message.lower()


# --- to_record_fields -----------------------------------------------------


def test_to_record_fields_has_every_key_asset_record_needs_and_tier_archival(tmp_path):
    hit = SearchHit(
        "archive_org", "id1", "Duck and Cover", "https://archive.org/details/id1",
        "https://archive.org/download/id1/id1.mp4", "publicdomain",
    )
    local_path = tmp_path / "id1.mp4"

    fields = to_record_fields(hit, "a001", local_path, now=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert fields["asset_id"] == "a001"
    assert fields["tier"] == "archival"
    assert fields["provider"] == "archive_org"
    assert fields["original_url"] == "https://archive.org/details/id1"
    assert fields["license"] == "publicdomain"
    assert fields["local_path"] == str(local_path)
    assert "notes" in fields
    assert "retrieved_at" in fields

    # Must actually build an AssetRecord -- not just look plausible.
    record = AssetRecord(**fields)
    assert record.tier == "archival"


def test_to_record_fields_honours_injected_now(tmp_path):
    hit = SearchHit(
        "archive_org", "id1", "t", "https://archive.org/details/id1",
        "https://archive.org/download/id1/id1.mp4", "publicdomain",
    )
    pinned = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    fields = to_record_fields(hit, "a001", tmp_path / "id1.mp4", now=pinned)

    assert fields["retrieved_at"] == "2020-01-02T03:04:05Z"


def test_to_record_fields_records_chosen_filename_in_notes(tmp_path):
    # Six derivatives exist for a real Prelinger item; the ledger must say
    # which one was actually taken, not just point at the item page.
    hit = SearchHit(
        "archive_org", "Cheerios1960", "Cheerios Ad", "https://archive.org/details/Cheerios1960",
        "https://archive.org/download/Cheerios1960/Cheerios1960.mp4", "publicdomain",
    )
    local_path = tmp_path / "Cheerios1960.mp4"

    fields = to_record_fields(hit, "a001", local_path, now=datetime(2026, 7, 1, tzinfo=timezone.utc))

    assert "Cheerios1960.mp4" in fields["notes"]


# --- media-type gate ----------------------------------------------------


@pytest.mark.parametrize("mime,usable", [
    ("image/jpeg", True),
    ("image/png", True),
    ("video/webm", True),
    ("video/mp4", True),
    ("application/pdf", False),
    ("image/vnd.djvu", False),
    ("audio/ogg", False),
    ("image/svg+xml", False),
    ("", False),
])
def test_is_usable_mediatype(mime, usable):
    assert is_usable_mediatype(mime) is usable


def test_wikimedia_drops_a_pdf_however_well_licensed():
    """The failure this gate exists for. Commons namespace 6 holds PDF and DjVu
    book scans, and a full-text title search matches them: 'courtroom sketch,
    hand-drawn' returned the scanned title page of a 19th-century lecture
    collection -- public domain, resolvable, downloadable, and useless as
    footage. A cut can contain a still or a clip; it cannot contain a PDF."""
    body = _wikimedia_body({
        "1": {
            "pageid": 1,
            "title": "File:A selection from the addresses.pdf",
            "imageinfo": [{
                "url": "https://upload.wikimedia.org/x.pdf",
                "mime": "application/pdf",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:x.pdf",
                "extmetadata": {"LicenseShortName": {"value": "Public domain"}},
            }],
        }
    })
    hits = search_wikimedia("courtroom sketch", _transport_for({"api.php": (200, body)}))
    assert hits == []


def test_wikimedia_keeps_an_image():
    body = _wikimedia_body({
        "1": {
            "pageid": 1,
            "title": "File:Barricade.jpg",
            "imageinfo": [{
                "url": "https://upload.wikimedia.org/b.jpg",
                "mime": "image/jpeg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:b.jpg",
                "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}},
            }],
        }
    })
    hits = search_wikimedia("barricade", _transport_for({"api.php": (200, body)}))
    assert len(hits) == 1
    assert hits[0].mediatype == "image/jpeg"


# --- query refinement ---------------------------------------------------


@pytest.mark.parametrize("seed,expected", [
    ("archival courtroom sketch, hand-drawn", "courtroom sketch"),
    ("archival generic campaign war-room footage", "campaign war-room"),
    ("archival barricade wide shot, police line", "barricade police line"),
    ("screenshot archived homepage", "archived homepage"),
    ("archival observatory, empty at dusk", "observatory empty at dusk"),
])
def test_refine_query_drops_shot_vocabulary(seed, expected):
    """`slots.build_slots` documents its query as a seed and says refinement
    belongs to whatever retrieves the asset. Nothing refined it, so Commons was
    receiving this pipeline's own tier vocabulary as search terms. Measured on 13
    real slot queries: 5 usable hits raw, 43 refined."""
    assert refine_query(seed) == expected


def test_refine_query_keeps_a_seed_made_entirely_of_shot_words():
    """Refining to nothing would search for everything, which is worse."""
    assert refine_query("archival wide shot") == "archival wide shot"


def test_search_archives_refines_before_sending():
    sent = []

    def transport(url):
        sent.append(url)
        return 200, b"{}"

    search_archives("archival courtroom sketch, hand-drawn", transport)

    assert sent, "no request was made"
    for url in sent:
        assert "archival" not in url.lower()
        assert "hand-drawn" not in url.lower()


# --- derivative size selection ------------------------------------------


def test_picks_the_largest_file_under_the_ceiling_not_the_master():
    """It used to sort by size descending and take the first, i.e. deliberately
    choose archive.org's preservation master. One 12-second slot pulled 182 MB
    and 50 slots were on course for ~8 GB, to be downscaled to 1080p and cut to
    a few seconds."""
    candidates = [
        ("small.mp4", 5 * 1024 * 1024),
        ("good.mp4", 40 * 1024 * 1024),
        ("master.mp4", 900 * 1024 * 1024),
    ]
    assert _pick_derivative(candidates) == "good.mp4"


def test_falls_back_to_the_smallest_when_everything_exceeds_the_ceiling():
    candidates = [
        ("big.mp4", 500 * 1024 * 1024),
        ("bigger.mp4", 900 * 1024 * 1024),
    ]
    assert _pick_derivative(candidates) == "big.mp4"


def test_a_missing_size_does_not_win_by_looking_smallest():
    """archive.org omits `size` on some entries; treated as 0 it would beat every
    real file in a smallest-wins comparison."""
    candidates = [
        ("unknown.mp4", 0),
        ("real.mp4", 30 * 1024 * 1024),
    ]
    assert _pick_derivative(candidates) == "real.mp4"


def test_all_sizes_unknown_still_returns_something():
    assert _pick_derivative([("a.mp4", 0), ("b.mp4", 0)]) == "a.mp4"


def test_resolve_media_url_prefers_a_derivative_over_the_master():
    metadata = _metadata_body([
        {"name": "master.mp4", "format": "MPEG4", "size": str(900 * 1024 * 1024)},
        {"name": "derivative.mp4", "format": "MPEG4", "size": str(30 * 1024 * 1024)},
    ])
    hit = _hit("id1")
    resolved = resolve_media_url(hit, _transport_for({"metadata": (200, metadata)}))
    assert "derivative.mp4" in resolved.media_url


# --- commons_file: hand-picking one named file --------------------------


def _commons_page(mime="image/jpeg", licence="CC BY-SA 3.0", missing=False):
    page = {"pageid": 26365031, "title": "File:Example Observatory at Dusk.jpg"}
    if missing:
        page["missing"] = ""
        return _wikimedia_body({"-1": page})
    page["imageinfo"] = [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/4/4a/View.jpg",
        "mime": mime,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:View.jpg",
        "extmetadata": {"LicenseShortName": {"value": licence}},
    }]
    return _wikimedia_body({"26365031": page})


def test_commons_file_returns_the_named_file_with_its_real_licence():
    hit = commons_file("File:Example Observatory at Dusk.jpg",
                       _transport_for({"api.php": (200, _commons_page())}))
    assert hit.license == "CC BY-SA 3.0"
    assert hit.mediatype == "image/jpeg"
    assert hit.media_url.endswith("View.jpg")
    assert hit.provider == "wikimedia"


def test_commons_file_raises_naming_a_title_that_does_not_exist():
    with pytest.raises(RuntimeError, match="no file titled"):
        commons_file("File:Nope.jpg",
                     _transport_for({"api.php": (200, _commons_page(missing=True))}))


def test_commons_file_refuses_an_unusable_media_type():
    """Hand-picking must not bypass the gate that keeps book scans out."""
    with pytest.raises(RuntimeError, match="media type"):
        commons_file("File:Book.pdf",
                     _transport_for({"api.php": (200, _commons_page(mime="application/pdf"))}))


def test_commons_file_raises_on_a_non_200():
    with pytest.raises(RuntimeError, match="HTTP 503"):
        commons_file("File:X.jpg", _transport_for({"api.php": (503, b"")}))


def test_commons_file_surfaces_an_undeterminable_licence_rather_than_inventing_one():
    hit = commons_file("File:X.jpg",
                       _transport_for({"api.php": (200, _commons_page(licence=""))}))
    assert hit.license == UNKNOWN_LICENSE
    assert hit.is_usable is False
