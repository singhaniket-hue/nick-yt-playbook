from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rabbithole.resolve_runner import (
    ImmutableTimelineError,
    QueueError,
    ResolveExecutionError,
    ResolveUnavailableError,
    _marker_color,
    connect_resolve,
    enqueue_job,
    execute_build,
    execute_render,
    install_runner,
    read_status,
    run_pending_jobs,
    subtitle_import_path,
    timeline_import_path,
    timeline_name_for_plan,
)
from rabbithole.resolve_safety import ResolveBusyError, UnsafeWriteError


class FakeSubtitleItem:
    def __init__(self, start: int, end: int, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetName(self):
        return self.text


class FakeTimeline:
    def __init__(
        self,
        name: str,
        *,
        video_tracks: int = 1,
        audio_tracks: int = 1,
        subtitle_tracks: int = 0,
    ) -> None:
        self.name = name
        self.counts = {
            "video": video_tracks,
            "audio": audio_tracks,
            "subtitle": subtitle_tracks,
        }
        self.track_names: dict[tuple[str, int], str] = {}
        self.markers: list[tuple] = []
        self.marker_add_calls: list[tuple] = []
        self.rename_calls: list[str] = []
        self.subtitle_items: list[FakeSubtitleItem] = []

    def GetName(self):
        return self.name

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return 30

    def SetName(self, name):
        self.rename_calls.append(name)
        self.name = name
        return True

    def GetTrackCount(self, kind):
        return self.counts[kind]

    def AddTrack(self, kind):
        self.counts[kind] += 1
        return True

    def SetTrackName(self, kind, index, name):
        self.track_names[(kind, index)] = name
        return True

    def GetTrackName(self, kind, index):
        return self.track_names.get((kind, index))

    def GetItemListInTrack(self, kind, index):
        if kind == "subtitle" and index == 1:
            return list(self.subtitle_items)
        return []

    def AddMarker(self, *args):
        self.marker_add_calls.append(args)
        self.markers.append(args)
        return True

    def UpdateMarkerCustomData(self, frame, custom_data):
        for index, marker in enumerate(self.markers):
            if marker[0] == frame:
                self.markers[index] = (*marker[:5], custom_data)
                return True
        return False

    def DeleteMarkerAtFrame(self, frame):
        for index, marker in enumerate(self.markers):
            if marker[0] == frame:
                del self.markers[index]
                return True
        return False

    def GetMarkers(self):
        return {
            marker[0]: {
                "color": marker[1],
                "name": marker[2],
                "note": marker[3],
                "duration": marker[4],
                "customData": marker[5],
            }
            for marker in self.markers
        }


class FakeMediaPool:
    def __init__(self, project) -> None:
        self.project = project
        self.calls: list[tuple[str, dict]] = []
        self.import_media_calls: list[list[str]] = []
        self.append_calls: list[list[object]] = []

    def ImportTimelineFromFile(self, path, options):
        self.calls.append((path, dict(options)))
        timeline = FakeTimeline(options["timelineName"])
        self.project.timelines.append(timeline)
        return timeline

    def ImportMedia(self, paths):
        values = list(paths)
        self.import_media_calls.append(values)
        text = Path(values[0]).read_text(encoding="utf-8")
        cues = []
        for block in (value for value in text.strip().split("\n\n") if value.strip()):
            lines = block.splitlines()
            start_raw, end_raw = lines[1].split(" --> ")

            def frame(timestamp):
                hours, minutes, seconds_ms = timestamp.split(":")
                seconds, milliseconds = seconds_ms.split(",")
                total_ms = (
                    int(hours) * 3_600_000
                    + int(minutes) * 60_000
                    + int(seconds) * 1000
                    + int(milliseconds)
                )
                return (total_ms * 30 + 500) // 1000

            cues.append(
                FakeSubtitleItem(
                    frame(start_raw),
                    frame(end_raw),
                    "\n".join(lines[2:]),
                )
            )
        return [{"path": values[0], "subtitle_cues": cues}]

    def AppendToTimeline(self, items):
        values = list(items)
        self.append_calls.append(values)
        timeline = self.project.current
        if timeline is None:
            return []
        timeline.subtitle_items.extend(values[0]["subtitle_cues"])
        # Resolve returns one TimelineItem for the imported SRT MediaPoolItem;
        # the timeline itself expands that item into every individual cue.
        return [object()]


class FakeProject:
    def __init__(
        self,
        name: str = "User Project",
        *,
        timelines: list[FakeTimeline] | None = None,
        rendering: bool = False,
    ) -> None:
        self.name = name
        self.timelines = list(timelines or [])
        self.rendering = rendering
        self.current = None
        self.media_pool = FakeMediaPool(self)
        self.render_settings = None
        self.render_format_codec = None
        self.render_mode = None
        self.render_jobs: list[str] = []
        self.started: list[str] = []
        self.render_statuses: dict[str, dict[str, str]] = {}

    def GetName(self):
        return self.name

    def IsRenderingInProgress(self):
        return self.rendering

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        return self.timelines[index - 1]

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetMediaPool(self):
        return self.media_pool

    def SetRenderSettings(self, settings):
        self.render_settings = dict(settings)
        return True

    def SetCurrentRenderFormatAndCodec(self, render_format, render_codec):
        self.render_format_codec = (render_format, render_codec)
        return True

    def SetCurrentRenderMode(self, render_mode):
        self.render_mode = render_mode
        return True

    def AddRenderJob(self):
        job_id = f"render-{len(self.render_jobs) + 1}"
        self.render_jobs.append(job_id)
        return job_id

    def StartRendering(self, job_id):
        self.started.append(job_id)
        self.rendering = True
        self.render_statuses.setdefault(job_id, {"JobStatus": "Rendering"})
        return True

    def GetRenderJobStatus(self, job_id):
        return self.render_statuses.get(job_id)


class FakeProjectManager:
    def __init__(self, project: FakeProject | None) -> None:
        self.project = project
        self.created: list[tuple] = []
        self.save_calls = 0

    def GetCurrentProject(self):
        return self.project

    def CreateProject(self, *args):
        self.created.append(args)
        self.project = FakeProject(args[0])
        return self.project

    def SaveProject(self):
        self.save_calls += 1
        return True


class FakeResolve:
    def __init__(self, project: FakeProject | None) -> None:
        self.manager = FakeProjectManager(project)

    def GetProjectManager(self):
        return self.manager


@pytest.mark.parametrize("severity", ["warning", "human"])
def test_review_marker_severities_use_resolve_supported_yellow(
    severity: str,
) -> None:
    assert _marker_color("review", severity) == "Yellow"


def compiler_shaped_plan(project: Path) -> tuple[Path, dict]:
    build_id = "b-deadbeefcafe"
    build_dir = project / "resolve" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)
    fcpxml = build_dir / "timeline.fcpxml"
    fcpxml.write_text("<fcpxml version=\"1.10\"/>", encoding="utf-8")
    fcpxml_sha256 = hashlib.sha256(fcpxml.read_bytes()).hexdigest()
    subtitles = build_dir / "subtitles.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTest subtitle\n",
        encoding="utf-8",
    )
    subtitles_sha256 = hashlib.sha256(subtitles.read_bytes()).hexdigest()
    plan = {
        "schema_version": "resolve-plan.v1",
        "build_id": build_id,
        "timeline_name": "AUTO_BUILD_DEADBEEFCAFE",
        "output_paths": {
            "plan": f"resolve/builds/{build_id}/resolve-plan.v1.json",
            "plan_path_kind": "project-relative",
            "fcpxml": f"resolve/builds/{build_id}/timeline.fcpxml",
            "fcpxml_path_kind": "project-relative",
            "fcpxml_sha256": fcpxml_sha256,
            "subtitles": f"resolve/builds/{build_id}/subtitles.srt",
            "subtitles_path_kind": "project-relative",
            "subtitles_sha256": subtitles_sha256,
        },
        "tracks": {
            "video": [
                {"id": f"V{index}", "index": index, "intent": "video"}
                for index in range(1, 5)
            ],
            "audio": [
                {"id": f"A{index}", "index": index, "intent": "audio"}
                for index in range(1, 6)
            ],
        },
        "markers": [
            {
                "id": "marker-1",
                "kind": "highlight",
                "arg": "Evidence",
                "frame": 30,
            }
        ],
        "review_flags": [
            {
                "id": "review-1",
                "kind": "redaction_verification",
                "severity": "human",
                "frame": 30,
                "asset_id": "asset-1",
                "message": "Verify redaction",
            }
        ],
        "provenance": [
            {
                "id": "asset-1",
                "asset_id": "asset-1",
                "local_path": "assets/evidence.png",
                "license": "CC BY",
            }
        ],
        "subtitles": [
            {
                "id": "subtitle-1",
                "start_frame": 0,
                "end_frame": 30,
                "text": "Test subtitle",
            }
        ],
    }
    plan_path = build_dir / "resolve-plan.v1.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, plan


def test_compiler_shaped_plan_builds_tracks_markers_and_reuses_immutably(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    editorial = FakeTimeline("EDITORIAL_v1")
    project = FakeProject(timelines=[editorial])
    resolve = FakeResolve(project)

    assert timeline_name_for_plan(plan) == "AUTO_BUILD_DEADBEEFCAFE"
    assert timeline_import_path(plan, plan_path, project_root) == (
        project_root / plan["output_paths"]["fcpxml"]
    ).resolve()
    assert subtitle_import_path(plan, plan_path, project_root) == (
        project_root / plan["output_paths"]["subtitles"]
    ).resolve()

    result = execute_build(
        resolve, plan, project_root=project_root, plan_path=plan_path
    )
    assert result["project_name"] == "User Project"
    assert result["timeline_name"] == "AUTO_BUILD_DEADBEEFCAFE"
    assert result["reused"] is False
    assert result["saved"] is True
    assert resolve.manager.save_calls == 1
    assert resolve.manager.created == []
    assert len(project.media_pool.calls) == 1
    assert len(project.media_pool.import_media_calls) == 1
    assert len(project.media_pool.append_calls) == 1
    assert result["subtitles"]["status"] == "srt_imported"
    assert result["subtitles"]["count"] == 1

    generated = project.timelines[-1]
    assert generated.counts == {"video": 4, "audio": 5, "subtitle": 1}
    assert generated.track_names[("video", 4)] == "V4"
    assert generated.track_names[("audio", 5)] == "A5"
    assert generated.track_names[("subtitle", 1)] == "SUBTITLES"
    assert len(generated.markers) == 2
    assert all(call[5] == "" for call in generated.marker_add_calls)
    identity = next(value for value in generated.markers if value[0] == 0)
    assert "Fusion titles/transitions" in identity[3]
    assert json.loads(identity[5])["style"]["status"] == "disabled"
    marker = next(value for value in generated.markers if value[0] == 30)
    assert marker[1] == "Yellow"
    custom_data = json.loads(marker[5])
    assert custom_data["build_id"] == "b-deadbeefcafe"
    assert custom_data["marker_id"] == "marker-1"
    assert custom_data["review"][0]["id"] == "review-1"
    assert custom_data["provenance"][0]["asset_id"] == "asset-1"
    assert editorial.track_names == {}
    assert editorial.markers == []
    assert editorial.rename_calls == []

    second = execute_build(
        resolve, plan, project_root=project_root, plan_path=plan_path
    )
    assert second["reused"] is True
    assert resolve.manager.save_calls == 1
    assert len(project.media_pool.calls) == 1
    assert len(project.media_pool.import_media_calls) == 1
    assert len(generated.markers) == 2


def test_no_current_project_allows_only_deterministic_create(tmp_path: Path) -> None:
    project_root = tmp_path / "my episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    resolve = FakeResolve(None)
    result = execute_build(
        resolve, plan, project_root=project_root, plan_path=plan_path
    )
    assert result["project_created"] is True
    assert resolve.manager.created[0][0].startswith("RABBITHOLE_my_episode_")
    assert not hasattr(resolve.manager, "LoadProject")


def test_native_fcpxml_subtitles_skip_srt_fallback(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()

    def import_with_native_caption(path, options):
        project.media_pool.calls.append((path, dict(options)))
        timeline = FakeTimeline(options["timelineName"], subtitle_tracks=1)
        timeline.subtitle_items.append(
            FakeSubtitleItem(0, 30, "Test subtitle")
        )
        project.timelines.append(timeline)
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_native_caption
    result = execute_build(
        FakeResolve(project),
        plan,
        project_root=project_root,
        plan_path=plan_path,
    )

    assert result["subtitles"] == {"status": "fcpxml", "count": 1}
    assert project.media_pool.import_media_calls == []
    assert project.media_pool.append_calls == []


def test_native_fcpxml_subtitles_reject_wrong_text(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()

    def import_with_wrong_caption(path, options):
        project.media_pool.calls.append((path, dict(options)))
        timeline = FakeTimeline(options["timelineName"], subtitle_tracks=1)
        timeline.subtitle_items.append(
            FakeSubtitleItem(0, 30, "Wrong subtitle")
        )
        project.timelines.append(timeline)
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_wrong_caption
    resolve = FakeResolve(project)

    with pytest.raises(
        ImmutableTimelineError,
        match="subtitle text or timing does not match",
    ):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    assert project.media_pool.import_media_calls == []
    assert project.media_pool.append_calls == []
    assert resolve.manager.save_calls == 0


def test_srt_fallback_rejects_shifted_cue_timing(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()

    def append_shifted_subtitles(items):
        values = list(items)
        project.media_pool.append_calls.append(values)
        cues = values[0]["subtitle_cues"]
        project.current.subtitle_items.extend(
            FakeSubtitleItem(
                cue.GetStart() + 300,
                cue.GetEnd() + 300,
                cue.GetName(),
            )
            for cue in cues
        )
        return [object()]

    project.media_pool.AppendToTimeline = append_shifted_subtitles
    resolve = FakeResolve(project)

    with pytest.raises(
        ImmutableTimelineError,
        match="subtitle cue timing does not match",
    ):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    assert len(project.media_pool.import_media_calls) == 1
    assert len(project.media_pool.append_calls) == 1
    assert resolve.manager.save_calls == 0


def test_partial_fcpxml_subtitles_fail_without_srt_append(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    plan["subtitles"].append(
        {
            "id": "subtitle-2",
            "start_frame": 36,
            "end_frame": 60,
            "text": "Second subtitle",
        }
    )
    project = FakeProject()

    def import_with_partial_caption(path, options):
        project.media_pool.calls.append((path, dict(options)))
        timeline = FakeTimeline(options["timelineName"], subtitle_tracks=1)
        timeline.subtitle_items.append(
            FakeSubtitleItem(0, 30, "Test subtitle")
        )
        project.timelines.append(timeline)
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_partial_caption
    with pytest.raises(
        ImmutableTimelineError,
        match="partial FCPXML caption import",
    ):
        execute_build(
            FakeResolve(project),
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    assert project.media_pool.import_media_calls == []
    assert project.media_pool.append_calls == []


def test_new_build_is_not_reported_successful_when_save_fails(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    resolve = FakeResolve(FakeProject())
    resolve.manager.SaveProject = lambda: False

    with pytest.raises(ResolveExecutionError, match="SaveProject"):
        execute_build(
            resolve, plan, project_root=project_root, plan_path=plan_path
        )


def test_imported_fcpxml_marker_is_enriched_without_duplicate_collision(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile

    def import_with_marker(path, options):
        timeline = original_import(path, options)
        timeline.markers.append(
            (30, "Blue", "FCPXML marker", "Evidence", 1, "")
        )
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_marker
    execute_build(
        FakeResolve(project),
        plan,
        project_root=project_root,
        plan_path=plan_path,
    )

    generated = project.timelines[-1]
    assert [marker[0] for marker in generated.markers] == [30, 0]
    enriched = json.loads(generated.markers[0][5])
    assert enriched["build_id"] == plan["build_id"]
    assert enriched["marker_ids"] == ["marker-1"]


def test_marker_add_retries_when_resolve_is_temporarily_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile
    sleep_calls: list[float] = []

    def import_with_busy_first_marker(path, options):
        timeline = original_import(path, options)
        original_add = timeline.AddMarker
        calls = 0

        def busy_then_add(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return False
            return original_add(*args)

        timeline.AddMarker = busy_then_add
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_busy_first_marker
    monkeypatch.setattr(
        "rabbithole.resolve_runner.time.sleep",
        sleep_calls.append,
    )

    execute_build(
        FakeResolve(project),
        plan,
        project_root=project_root,
        plan_path=plan_path,
    )

    generated = project.timelines[-1]
    assert sleep_calls == [0.05]
    assert len(generated.markers) == 2
    assert all(
        json.loads(marker[5])["build_id"] == plan["build_id"]
        for marker in generated.markers
    )


def test_marker_add_permanent_rejection_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile
    add_calls = 0

    def import_with_rejected_markers(path, options):
        timeline = original_import(path, options)

        def reject_marker(*args):
            nonlocal add_calls
            add_calls += 1
            return False

        timeline.AddMarker = reject_marker
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_rejected_markers
    monkeypatch.setattr(
        "rabbithole.resolve_runner.time.sleep",
        lambda _seconds: None,
    )
    resolve = FakeResolve(project)

    with pytest.raises(ResolveExecutionError, match="AddMarker"):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    assert add_calls == 4
    assert project.timelines[-1].markers == []
    assert resolve.manager.save_calls == 0


@pytest.mark.parametrize("failed_update", [1, 2])
def test_marker_metadata_failure_rolls_back_new_markers_and_retry_fails_closed(
    tmp_path: Path,
    failed_update: int,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile

    def import_with_flaky_marker_update(path, options):
        timeline = original_import(path, options)
        original_update = timeline.UpdateMarkerCustomData
        calls = 0

        def flaky_update(frame, custom_data):
            nonlocal calls
            calls += 1
            if calls == failed_update:
                return False
            return original_update(frame, custom_data)

        timeline.UpdateMarkerCustomData = flaky_update
        return timeline

    project.media_pool.ImportTimelineFromFile = import_with_flaky_marker_update
    resolve = FakeResolve(project)

    with pytest.raises(
        ResolveExecutionError,
        match="UpdateMarkerCustomData",
    ):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    generated = project.timelines[-1]
    assert generated.markers == []
    assert resolve.manager.save_calls == 0

    with pytest.raises(
        ImmutableTimelineError,
        match="complete RabbitHole markers",
    ):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )
    assert resolve.manager.save_calls == 0


def test_marker_failure_restores_preexisting_custom_data(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile

    def import_with_existing_marker_and_flaky_update(path, options):
        timeline = original_import(path, options)
        timeline.markers.append(
            (30, "Blue", "FCPXML marker", "Evidence", 1, "original")
        )
        original_update = timeline.UpdateMarkerCustomData
        calls = 0

        def flaky_update(frame, custom_data):
            nonlocal calls
            calls += 1
            if calls == 2:
                return False
            return original_update(frame, custom_data)

        timeline.UpdateMarkerCustomData = flaky_update
        return timeline

    project.media_pool.ImportTimelineFromFile = (
        import_with_existing_marker_and_flaky_update
    )

    with pytest.raises(
        ResolveExecutionError,
        match="UpdateMarkerCustomData",
    ):
        execute_build(
            FakeResolve(project),
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    generated = project.timelines[-1]
    assert generated.markers == [
        (30, "Blue", "FCPXML marker", "Evidence", 1, "original")
    ]


def test_marker_rollback_failure_is_explicit(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    original_import = project.media_pool.ImportTimelineFromFile

    def import_with_unrecoverable_marker_update(path, options):
        timeline = original_import(path, options)
        timeline.UpdateMarkerCustomData = lambda frame, custom_data: False
        timeline.DeleteMarkerAtFrame = lambda frame: False
        return timeline

    project.media_pool.ImportTimelineFromFile = (
        import_with_unrecoverable_marker_update
    )
    resolve = FakeResolve(project)

    with pytest.raises(
        ResolveExecutionError,
        match="marker rollback incomplete",
    ):
        execute_build(
            resolve,
            plan,
            project_root=project_root,
            plan_path=plan_path,
        )

    assert resolve.manager.save_calls == 0


def test_import_integrity_validates_duration_linked_clips_and_subtitles(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    plan["clips"] = [
        {
            "id": "clip-1",
            "slot_id": "s001",
            "track": "V1",
            "start_frame": 0,
            "media_path": "assets/evidence.png",
        },
        {
            "id": "clip-2",
            "slot_id": "s002",
            "track": "V2",
            "start_frame": 15,
            "media_path": "assets/insert.png",
        }
    ]
    plan["audio"] = [
        {
            "id": "audio-1",
            "asset_id": "music-1",
            "track": "A3",
            "start_frame": 0,
            "media_path": "audio/music.wav",
        }
    ]
    plan["overlays"] = [
        {
            "id": "title-1",
            "kind": "source_caption",
            "track": "V3",
            "start_frame": 0,
            "text": "Archive source",
        }
    ]
    plan["timeline_validation"] = {
        "start_frame": 0,
        "end_frame": 30,
        "video_clip_count": 2,
        "video_title_count": 1,
        "audio_clip_count": 1,
        "subtitle_count": 1,
    }

    class LinkedItem:
        def GetMediaPoolItem(self):
            return object()

    class GeneratedTitle:
        def GetMediaPoolItem(self):
            return None

    class IntegrityTimeline(FakeTimeline):
        def GetStartFrame(self):
            return 86400

        def GetEndFrame(self):
            return 86430

        def GetItemListInTrack(self, kind, index):
            if kind == "video":
                if index in {1, 2}:
                    return [LinkedItem()]
                if index == 3:
                    return [GeneratedTitle()]
                return []
            if kind == "audio":
                return [LinkedItem()] if index == 3 else []
            if kind == "subtitle":
                return (
                    [FakeSubtitleItem(86400, 86430, "Test subtitle")]
                    if index == 1
                    else []
                )
            return []

    project = FakeProject()

    def import_integrity_timeline(path, options):
        project.media_pool.calls.append((path, dict(options)))
        timeline = IntegrityTimeline(options["timelineName"])
        project.timelines.append(timeline)
        return timeline

    project.media_pool.ImportTimelineFromFile = import_integrity_timeline
    resolve = FakeResolve(project)
    result = execute_build(
        resolve, plan, project_root=project_root, plan_path=plan_path
    )
    assert result["saved"] is True
    assert resolve.manager.save_calls == 1


def test_import_integrity_fails_before_save_for_unlinked_clip(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    plan["clips"] = [
        {
            "id": "clip-1",
            "slot_id": "s001",
            "track": "V1",
            "start_frame": 0,
            "media_path": "assets/evidence.png",
        }
    ]
    plan["timeline_validation"] = {
        "start_frame": 0,
        "end_frame": 30,
        "video_clip_count": 1,
        "audio_clip_count": 0,
        "subtitle_count": 1,
    }

    class UnlinkedItem:
        def GetMediaPoolItem(self):
            return None

    class IntegrityTimeline(FakeTimeline):
        def GetStartFrame(self):
            return 0

        def GetEndFrame(self):
            return 30

        def GetItemListInTrack(self, kind, index):
            if kind == "video":
                return [UnlinkedItem()] if index == 1 else []
            if kind == "subtitle":
                return (
                    [FakeSubtitleItem(0, 30, "Test subtitle")]
                    if index == 1
                    else []
                )
            return []

    project = FakeProject()

    def import_integrity_timeline(path, options):
        project.media_pool.calls.append((path, dict(options)))
        timeline = IntegrityTimeline(options["timelineName"])
        project.timelines.append(timeline)
        return timeline

    project.media_pool.ImportTimelineFromFile = import_integrity_timeline
    resolve = FakeResolve(project)
    with pytest.raises(ImmutableTimelineError, match="unlinked"):
        execute_build(
            resolve, plan, project_root=project_root, plan_path=plan_path
        )
    assert resolve.manager.save_calls == 0


def test_active_render_fails_before_timeline_import(tmp_path: Path) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    project = FakeProject(rendering=True)
    with pytest.raises(ResolveBusyError):
        execute_build(
            FakeResolve(project), plan, project_root=project_root, plan_path=plan_path
        )
    assert project.media_pool.calls == []


def test_external_adapter_is_explicitly_gated() -> None:
    calls: list[str] = []

    def adapter():
        calls.append("called")
        return object()

    with pytest.raises(ResolveUnavailableError, match="not enabled"):
        connect_resolve(studio_external_adapter=adapter)
    assert calls == []
    connection = connect_resolve(
        allow_studio_external=True, studio_external_adapter=adapter
    )
    assert connection.mode == "studio_external"
    assert calls == ["called"]


def test_queue_pointer_status_and_execution(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "episode"
    plan_path, _ = compiler_shaped_plan(project_root)
    local_state = tmp_path / "user-state"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    monkeypatch.setenv("XDG_STATE_HOME", str(local_state))

    job = enqueue_job(project_root, "build", plan_path)
    assert job["state"] == "queued"
    assert job["schema_version"] == 2
    assert job["fcpxml_sha256"] == hashlib.sha256(
        Path(job["fcpxml_path"]).read_bytes()
    ).hexdigest()
    assert job["media_sha256"] == {}
    assert "PROJECT_ROOT" in job["console_loader"]
    assert Path(job["runner_pointer"]).is_file()
    duplicate = enqueue_job(project_root, "build", plan_path)
    assert duplicate["job_id"] == job["job_id"]

    project = FakeProject(timelines=[FakeTimeline("EDITORIAL_v1")])
    results = run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    )
    assert [result["state"] for result in results] == ["succeeded"]
    status = read_status(project_root)
    assert status["state"] == "succeeded"
    assert status["queue"]["succeeded"] == 1
    assert status["project_name"] == "User Project"

    # A succeeded job is not implicitly rerun.
    assert run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    ) == []


def test_new_build_supersedes_only_older_queued_build_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    first_plan_path, first_plan = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))

    def plan_variant(token: str) -> tuple[Path, dict]:
        plan = json.loads(json.dumps(first_plan))
        build_id = f"b-{token}"
        build_dir = project_root / "resolve" / "builds" / build_id
        build_dir.mkdir(parents=True)
        fcpxml = build_dir / "timeline.fcpxml"
        fcpxml.write_text(
            f'<fcpxml version="1.10"><!-- {token} --></fcpxml>',
            encoding="utf-8",
        )
        plan["build_id"] = build_id
        plan["timeline_name"] = f"AUTO_BUILD_{token.upper()}"
        plan["output_paths"] = {
            "plan": f"resolve/builds/{build_id}/resolve-plan.v1.json",
            "plan_path_kind": "project-relative",
            "fcpxml": f"resolve/builds/{build_id}/timeline.fcpxml",
            "fcpxml_path_kind": "project-relative",
            "fcpxml_sha256": hashlib.sha256(fcpxml.read_bytes()).hexdigest(),
        }
        plan_path = build_dir / "resolve-plan.v1.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan_path, plan

    first_build = enqueue_job(project_root, "build", first_plan_path)
    queued_render = enqueue_job(project_root, "render", first_plan_path)
    second_plan_path, _ = plan_variant("111111111111")
    second_build = enqueue_job(project_root, "build", second_plan_path)

    retired = json.loads(Path(first_build["queue_file"]).read_text())
    assert retired["state"] == "superseded"
    assert retired["result"] == {
        "reason": "newer_build_enqueued",
        "superseded_by": second_build["job_id"],
    }
    assert json.loads(Path(queued_render["queue_file"]).read_text())["state"] == "queued"

    running = json.loads(Path(second_build["queue_file"]).read_text())
    running["state"] = "running"
    running["attempts"] = 1
    Path(second_build["queue_file"]).write_text(json.dumps(running), encoding="utf-8")
    third_plan_path, _ = plan_variant("222222222222")
    third_build = enqueue_job(project_root, "build", third_plan_path)

    assert json.loads(Path(second_build["queue_file"]).read_text())["state"] == "running"
    assert json.loads(Path(queued_render["queue_file"]).read_text())["state"] == "queued"
    assert json.loads(Path(third_build["queue_file"]).read_text())["state"] == "queued"


def test_async_render_queue_requires_positive_completion_before_success(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    local_state = tmp_path / "user-state"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    monkeypatch.setenv("XDG_STATE_HOME", str(local_state))
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(
        resolve,
        plan,
        project_root=project_root,
        plan_path=plan_path,
    )
    job = enqueue_job(project_root, "render", plan_path)

    started = run_pending_jobs(
        resolve=resolve,
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [item["state"] for item in started] == ["rendering"]
    render_job_id = started[0]["result"]["render_job_id"]
    status = read_status(project_root)
    assert status["state"] == "rendering"
    assert status["queue"]["rendering"] == 1
    assert status["queue"]["succeeded"] == 0

    project.rendering = False
    project.render_statuses[render_job_id] = {"JobStatus": "Unknown"}
    unverified = run_pending_jobs(
        resolve=resolve,
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [item["state"] for item in unverified] == ["rendering"]
    assert unverified[0]["result"]["render_status"] == "unknown"
    assert read_status(project_root)["detail"] == "render_completion_unverified"

    project.render_statuses[render_job_id] = {"JobStatus": "Complete"}
    completed = run_pending_jobs(
        resolve=resolve,
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [item["state"] for item in completed] == ["succeeded"]
    assert completed[0]["result"]["render_status"] == "complete"
    assert read_status(project_root)["queue"]["succeeded"] == 1
    assert project.started == [render_job_id]


def test_async_render_queue_records_terminal_resolve_failure(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(
        resolve,
        plan,
        project_root=project_root,
        plan_path=plan_path,
    )
    job = enqueue_job(project_root, "render", plan_path)
    started = run_pending_jobs(
        resolve=resolve,
        project_root=project_root,
        job_id=job["job_id"],
    )
    render_job_id = started[0]["result"]["render_job_id"]
    project.rendering = False
    project.render_statuses[render_job_id] = {"JobStatus": "Cancelled"}

    failed = run_pending_jobs(
        resolve=resolve,
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [item["state"] for item in failed] == ["failed"]
    assert failed[0]["result"]["render_status"] == "cancelled"
    assert failed[0]["error"]["type"] == "ResolveExecutionError"
    assert read_status(project_root)["state"] == "failed"


def test_queue_refuses_fcpxml_changed_after_enqueue(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    local_state = tmp_path / "state"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    monkeypatch.setenv("XDG_STATE_HOME", str(local_state))
    job = enqueue_job(project_root, "build", plan_path)
    fcpxml = project_root / plan["output_paths"]["fcpxml"]
    fcpxml.write_text("<fcpxml version=\"1.10\"><tampered/></fcpxml>", encoding="utf-8")
    project = FakeProject()

    results = run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [result["state"] for result in results] == ["failed"]
    assert results[0]["error"]["type"] == "QueueError"
    assert "FCPXML changed after enqueue" in results[0]["error"]["message"]
    assert project.media_pool.calls == []


def test_queue_refuses_subtitle_srt_changed_after_enqueue(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    job = enqueue_job(project_root, "build", plan_path)
    subtitles = project_root / plan["output_paths"]["subtitles"]
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTampered\n",
        encoding="utf-8",
    )
    project = FakeProject()

    results = run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [result["state"] for result in results] == ["failed"]
    assert results[0]["error"]["type"] == "QueueError"
    assert "subtitle SRT changed after enqueue" in results[0]["error"]["message"]
    assert project.media_pool.calls == []


def test_enqueue_refuses_fcpxml_not_matching_compiler_plan(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    fcpxml = project_root / plan["output_paths"]["fcpxml"]
    fcpxml.write_text("<fcpxml version=\"1.10\"><changed/></fcpxml>", encoding="utf-8")

    with pytest.raises(QueueError, match="checksum recorded in the plan"):
        enqueue_job(project_root, "build", plan_path)


def test_queue_document_fingerprint_rejects_modified_options(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, _ = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    job = enqueue_job(project_root, "build", plan_path)
    queue_path = Path(job["queue_file"])
    document = json.loads(queue_path.read_text(encoding="utf-8"))
    document["options"] = {"modified": True}
    queue_path.write_text(json.dumps(document), encoding="utf-8")
    project = FakeProject()

    with pytest.raises(QueueError, match="fingerprint"):
        run_pending_jobs(
            resolve=FakeResolve(project),
            project_root=project_root,
            job_id=job["job_id"],
        )

    assert project.media_pool.calls == []


def test_queue_refuses_linked_media_changed_after_enqueue(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    media = project_root / "assets" / "evidence.png"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"original-media")
    checksum = hashlib.sha256(media.read_bytes()).hexdigest()
    plan["clips"] = [
        {
            "asset_id": "asset-1",
            "media_path": "assets/evidence.png",
            "track": "V1",
        }
    ]
    plan["provenance"][0].update(
        {
            "path_kind": "project-relative",
            "sha256": checksum,
            "used_in_slots": ["slot-1"],
        }
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    local_state = tmp_path / "state"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    monkeypatch.setenv("XDG_STATE_HOME", str(local_state))
    job = enqueue_job(project_root, "build", plan_path)
    assert job["media_sha256"] == {"assets/evidence.png": checksum}
    media.write_bytes(b"changed-media")
    project = FakeProject()

    results = run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [result["state"] for result in results] == ["failed"]
    assert results[0]["error"]["type"] == "QueueError"
    assert "media changed after plan compilation" in results[0]["error"]["message"]
    assert project.media_pool.calls == []


def test_queue_refuses_linked_audio_changed_after_enqueue(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, plan = compiler_shaped_plan(project_root)
    audio = project_root / "audio" / "voice.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"original-audio")
    checksum = hashlib.sha256(audio.read_bytes()).hexdigest()
    plan["audio"] = [
        {
            "asset_id": "voice-1",
            "media_path": "audio/voice.wav",
            "path_kind": "project-relative",
            "sha256": checksum,
            "track": "A1",
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    job = enqueue_job(project_root, "build", plan_path)
    audio.write_bytes(b"changed-audio")
    project = FakeProject()

    results = run_pending_jobs(
        resolve=FakeResolve(project),
        project_root=project_root,
        job_id=job["job_id"],
    )

    assert [result["state"] for result in results] == ["failed"]
    assert "media changed after plan compilation" in results[0]["error"]["message"]
    assert project.media_pool.calls == []


def test_enqueue_rejects_invalid_linked_media_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))

    missing_asset_root = tmp_path / "missing-asset"
    plan_path, plan = compiler_shaped_plan(missing_asset_root)
    media = missing_asset_root / "audio.wav"
    media.write_bytes(b"audio")
    plan["audio"] = [
        {
            "media_path": "audio.wav",
            "path_kind": "project-relative",
            "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(QueueError, match="no asset_id"):
        enqueue_job(missing_asset_root, "build", plan_path)

    missing_hash_root = tmp_path / "missing-hash"
    plan_path, plan = compiler_shaped_plan(missing_hash_root)
    media = missing_hash_root / "audio.wav"
    media.write_bytes(b"audio")
    plan["audio"] = [
        {
            "asset_id": "voice-1",
            "media_path": "audio.wav",
            "path_kind": "project-relative",
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(QueueError, match="no matching plan-recorded SHA-256"):
        enqueue_job(missing_hash_root, "build", plan_path)

    invalid_hash_root = tmp_path / "invalid-hash"
    plan_path, plan = compiler_shaped_plan(invalid_hash_root)
    media = invalid_hash_root / "audio.wav"
    media.write_bytes(b"audio")
    plan["audio"] = [
        {
            "asset_id": "voice-1",
            "media_path": "audio.wav",
            "path_kind": "project-relative",
            "sha256": "not-a-sha256",
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(QueueError, match="valid SHA-256"):
        enqueue_job(invalid_hash_root, "build", plan_path)


def test_linked_media_path_policy_and_checksum_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))

    escape_root = tmp_path / "escape" / "episode"
    plan_path, plan = compiler_shaped_plan(escape_root)
    outside = escape_root.parent / "outside.wav"
    outside.write_bytes(b"outside")
    plan["audio"] = [
        {
            "asset_id": "outside-1",
            "media_path": "../outside.wav",
            "path_kind": "project-relative",
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(QueueError, match="escapes the project root"):
        enqueue_job(escape_root, "build", plan_path)

    external_root = tmp_path / "external" / "episode"
    plan_path, plan = compiler_shaped_plan(external_root)
    external = tmp_path / "shared" / "voice.wav"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"external")
    external_checksum = hashlib.sha256(external.read_bytes()).hexdigest()
    plan["audio"] = [
        {
            "asset_id": "external-1",
            "media_path": str(external.resolve()),
            "path_kind": "external-absolute",
            "sha256": external_checksum,
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    job = enqueue_job(external_root, "build", plan_path)
    assert job["media_sha256"] == {external.resolve().as_posix(): external_checksum}

    conflict_root = tmp_path / "conflict"
    plan_path, plan = compiler_shaped_plan(conflict_root)
    media = conflict_root / "same.wav"
    media.write_bytes(b"same")
    first_checksum = hashlib.sha256(media.read_bytes()).hexdigest()
    plan["clips"] = [
        {
            "asset_id": "clip-1",
            "media_path": "same.wav",
            "path_kind": "project-relative",
            "sha256": first_checksum,
        }
    ]
    plan["audio"] = [
        {
            "asset_id": "audio-1",
            "media_path": "same.wav",
            "path_kind": "project-relative",
            "sha256": "0" * 64,
        }
    ]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(QueueError, match="conflicting checksums"):
        enqueue_job(conflict_root, "build", plan_path)


def test_queue_stays_queued_when_resolve_is_already_rendering(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, _ = compiler_shaped_plan(project_root)
    local_state = tmp_path / "state"
    monkeypatch.setenv("LOCALAPPDATA", str(local_state))
    monkeypatch.setenv("XDG_STATE_HOME", str(local_state))
    job = enqueue_job(project_root, "build", plan_path)
    hash_reads: list[Path] = []
    monkeypatch.setattr(
        "rabbithole.resolve_runner._file_sha256",
        lambda path: hash_reads.append(Path(path)) or ("0" * 64),
    )
    busy_project = FakeProject(rendering=True)
    assert run_pending_jobs(
        resolve=FakeResolve(busy_project),
        project_root=project_root,
        job_id=job["job_id"],
    ) == []
    queued = json.loads(Path(job["queue_file"]).read_text())
    assert queued["state"] == "queued"
    assert queued["attempts"] == 0
    assert hash_reads == []
    status = read_status(project_root)
    assert status["detail"] == "blocked_by_resolve_state"


def test_legacy_queue_jobs_are_counted_but_not_executed(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "episode"
    plan_path, _ = compiler_shaped_plan(project_root)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))
    queue = project_root / "resolve" / "queue"
    queue.mkdir(parents=True)
    legacy_path = queue / "legacy-job.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "legacy-job",
                "action": "build",
                "state": "queued",
            }
        ),
        encoding="utf-8",
    )
    current = enqueue_job(project_root, "build", plan_path)
    status = read_status(project_root)
    assert status["queue"]["legacy"] == 1
    assert status["queue"]["queued"] == 1

    results = run_pending_jobs(
        resolve=FakeResolve(FakeProject()),
        project_root=project_root,
        job_id=current["job_id"],
    )
    assert [result["state"] for result in results] == ["succeeded"]

    with pytest.raises(QueueError, match="legacy integrity schema"):
        run_pending_jobs(
            resolve=FakeResolve(FakeProject()),
            project_root=project_root,
            job_id="legacy-job",
        )


def test_render_output_option_is_scoped_and_does_not_mutate_plan(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    _, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(resolve, plan, project_root=project_root)
    timeline = project.timelines[-1]
    original = json.loads(json.dumps(plan))

    output = project_root / "renders" / "draft_v1.mp4"
    result = execute_render(
        resolve, plan, project_root=project_root, output_path=output
    )
    assert result["render_status"] == "started"
    assert project.render_format_codec == ("mp4", "H264")
    assert project.render_mode == 1
    assert project.render_settings["TargetDir"] == str(output.parent.resolve())
    assert project.render_settings["CustomName"] == "draft_v1"
    assert plan == original

    project.rendering = False
    with pytest.raises(ResolveExecutionError, match="suffix"):
        execute_render(
            resolve,
            plan,
            project_root=project_root,
            output_path=project_root / "renders" / "bad.mov",
        )
    with pytest.raises(UnsafeWriteError):
        execute_render(
            resolve,
            plan,
            project_root=project_root,
            output_path=tmp_path / "outside" / "bad.mp4",
        )
    assert len(project.render_jobs) == 1


def test_render_revalidates_immutable_timeline_before_configuration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    _, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(resolve, plan, project_root=project_root)
    timeline = project.timelines[-1]
    timeline.track_names[("video", 1)] = "EDITED"

    with pytest.raises(ImmutableTimelineError, match="expected 'V1'"):
        execute_render(resolve, plan, project_root=project_root)

    assert project.render_format_codec is None
    assert project.render_mode is None
    assert project.render_settings is None
    assert project.render_jobs == []


def test_render_rejects_modified_subtitle_timing_before_configuration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    _, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(resolve, plan, project_root=project_root)
    timeline = project.timelines[-1]
    timeline.subtitle_items[0].start += 30
    timeline.subtitle_items[0].end += 30

    with pytest.raises(
        ImmutableTimelineError,
        match="subtitle cue timing does not match",
    ):
        execute_render(resolve, plan, project_root=project_root)

    assert project.render_format_codec is None
    assert project.render_mode is None
    assert project.render_settings is None
    assert project.render_jobs == []


def test_render_rejects_incomplete_marker_contract_before_configuration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "episode"
    _, plan = compiler_shaped_plan(project_root)
    project = FakeProject()
    resolve = FakeResolve(project)
    execute_build(resolve, plan, project_root=project_root)
    timeline = project.timelines[-1]
    timeline.markers = [
        marker for marker in timeline.markers if marker[0] == 0
    ]

    with pytest.raises(
        ImmutableTimelineError,
        match="complete RabbitHole markers",
    ):
        execute_render(resolve, plan, project_root=project_root)

    assert project.render_format_codec is None
    assert project.render_mode is None
    assert project.render_settings is None
    assert project.render_jobs == []


def test_install_runner_copies_workspace_script(tmp_path: Path) -> None:
    installed = install_runner(tmp_path / "Utility")
    assert installed.name == "RabbitHole Resolve Runner.py"
    assert "run_pending_jobs" in installed.read_text(encoding="utf-8")


def test_workspace_bootstrap_is_python36_parseable_and_guards_before_import() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "rabbithole_resolve_runner.py"
    ).read_text(encoding="utf-8")

    ast.parse(source, feature_version=(3, 6))
    assert source.index("_require_supported_python()") < source.index(
        "_fresh_run_pending_jobs(source_root)"
    )


def test_workspace_bootstrap_reloads_changed_checkout_in_same_interpreter(
    tmp_path: Path,
) -> None:
    source_script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "rabbithole_resolve_runner.py"
    )
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    package = checkout / "rabbithole"
    scripts.mkdir(parents=True)
    package.mkdir()
    bootstrap = scripts / "rabbithole_resolve_runner.py"
    bootstrap.write_text(
        source_script.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    runner_module = package / "resolve_runner.py"
    runner_module.write_text(
        "def run_pending_jobs(**_kwargs):\n"
        "    return [{'revision': 'first'}]\n",
        encoding="utf-8",
    )
    probe = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "bootstrap = Path(sys.argv[1])\n"
        "runner = Path(sys.argv[2])\n"
        "globals_ = {'RUN_RABBITHOLE_RESOLVE_RUNNER': True, "
        "'PROJECT_ROOT': 'episode', 'JOB_ID': 'job'}\n"
        "first = runpy.run_path(str(bootstrap), init_globals=globals_)['RESULTS']\n"
        "runner.write_text(\"def run_pending_jobs(**_kwargs):\\n"
        "    return [{'revision': 'second-version'}]\\n\", encoding='utf-8')\n"
        "second = runpy.run_path(str(bootstrap), init_globals=globals_)['RESULTS']\n"
        "assert first == [{'revision': 'first'}], first\n"
        "assert second == [{'revision': 'second-version'}], second\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe, str(bootstrap), str(runner_module)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_non_auto_timeline_name_is_refused() -> None:
    with pytest.raises(ImmutableTimelineError):
        timeline_name_for_plan(
            {"build_id": "b-deadbeefcafe", "timeline_name": "EDITORIAL_v1"}
        )
