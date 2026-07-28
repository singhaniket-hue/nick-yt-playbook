"""Encoder selection and its per-backend quality flags.

The trap this guards: libx264 takes `-crf`, NVENC rejects it and wants `-cq`
with its own `p1`-`p7` preset vocabulary. Handing either backend the other's
flags fails at the ffmpeg call, and six modules encode video -- so they have to
agree.
"""

import os

import pytest

from rabbithole.encoding import (
    CPU_ENCODER,
    ENV_OVERRIDE,
    GPU_ENCODERS,
    detect_encoder,
    is_gpu,
    video_args,
)


def test_libx264_gets_crf():
    assert video_args(20, encoder="libx264") == ["-c:v", "libx264", "-crf", "20"]


def test_nvenc_gets_cq_not_crf():
    """`-crf` is not a valid NVENC option; passing it fails the encode."""
    args = video_args(20, encoder="h264_nvenc")
    assert "-cq" in args
    assert "-crf" not in args
    assert args[:2] == ["-c:v", "h264_nvenc"]


def test_nvenc_preset_is_its_own_vocabulary():
    """NVENC presets are p1-p7; x264 names like 'medium' are rejected."""
    args = video_args(20, encoder="h264_nvenc")
    preset = args[args.index("-preset") + 1]
    assert preset.startswith("p") and preset[1:].isdigit()


def test_qsv_and_amf_get_their_own_quality_flags():
    assert "-global_quality" in video_args(20, encoder="h264_qsv")
    amf = video_args(20, encoder="h264_amf")
    assert "-qp_i" in amf and "-crf" not in amf


def test_quality_number_reaches_every_backend():
    for encoder in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf"):
        assert "23" in video_args(23, encoder=encoder), encoder


def test_is_gpu_classifies_each_backend():
    assert is_gpu("libx264") is False
    for encoder in GPU_ENCODERS:
        assert is_gpu(encoder) is True, encoder


def test_env_override_is_honoured_verbatim(monkeypatch):
    """An author naming an encoder has said what they want; silently
    substituting a different one would be worse than failing at ffmpeg."""
    detect_encoder.cache_clear()
    monkeypatch.setenv(ENV_OVERRIDE, "libx264")
    try:
        assert detect_encoder() == "libx264"
    finally:
        detect_encoder.cache_clear()


def test_detection_returns_something_encodable():
    """Whatever is detected here must be a name ffmpeg accepts, since every
    encode site trusts it."""
    detect_encoder.cache_clear()
    chosen = detect_encoder()
    assert chosen == CPU_ENCODER or chosen in GPU_ENCODERS


def test_a_listed_but_broken_encoder_is_not_selected(monkeypatch):
    """`ffmpeg -encoders` lists what was compiled in, not what works. NVENC on a
    machine with no usable driver is listed and then fails at run time."""
    import rabbithole.encoding as encoding

    detect_encoder.cache_clear()
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(encoding, "_available_encoders", lambda: frozenset(GPU_ENCODERS))
    monkeypatch.setattr(encoding, "_encoder_works", lambda name: False)
    try:
        assert detect_encoder() == CPU_ENCODER
    finally:
        detect_encoder.cache_clear()
