"""Adversarial framing checks; no OS process or private configuration access."""

import struct

import pytest

from deskpal.execution.protocol import (
    FrameDecoder, FrameLimits, ProtocolViolation, decode_payload, encode_frame,
)


@pytest.mark.parametrize("fragment_size", [1, 2, 3, 4, 7, 1000])
def test_fragmented_and_coalesced_frames(fragment_size: int) -> None:
    messages: list[dict[str, object]] = [
        {"text": "中文和括号{}[]\\\"", "n": 2**63 - 1},
        {"ok": True, "nothing": None, "array": [1, 2.5]},
    ]
    wire = b"".join(encode_frame(message) for message in messages)
    decoder = FrameDecoder()
    actual = []
    for position in range(0, len(wire), fragment_size):
        actual.extend(decoder.feed(wire[position:position + fragment_size]))
    decoder.finish()
    assert actual == messages


@pytest.mark.parametrize("payload,code", [
    (b'{"x":1,"x":2}', "duplicate_key"),
    (b'{"x":{"y":1,"y":2}}', "duplicate_key"),
    (b'{"x":NaN}', "non_finite_number"),
    (b'{"x":Infinity}', "non_finite_number"),
    (b'{"x":1e999}', "non_finite_number"),
    (b'{"x":9223372036854775808}', "integer_range"),
    (b'{"x":-9223372036854775809}', "integer_range"),
    (b'{"x":999999999999999999999999999}', "integer_range"),
    (b'{"x":"\\ud800"}', "invalid_unicode"),
    (b'{"x":"\xff"}', "invalid_json"),
    (b'{}{}', "invalid_json"),
    (b'[]', "root_object_required"),
    (b'null', "root_object_required"),
])
def test_untrusted_json_rejected_without_body_in_error(payload: bytes, code: str) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        decode_payload(payload)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_deep_payload_is_rejected_before_parser_recursion() -> None:
    payload = b'{"x":' + b'[' * 1000 + b'0' + b']' * 1000 + b'}'
    with pytest.raises(ProtocolViolation, match="depth_budget"):
        decode_payload(payload)


@pytest.mark.parametrize("length", [0, 256 * 1024 + 1, 2**32 - 1])
def test_invalid_length_fails_at_header_and_poisoned_channel(length: int) -> None:
    decoder = FrameDecoder()
    with pytest.raises(ProtocolViolation, match="frame_budget"):
        decoder.feed(struct.pack("!I", length))
    with pytest.raises(ProtocolViolation, match="decoder_failed"):
        decoder.feed(encode_frame({"ok": True}))


@pytest.mark.parametrize("wire", [b'\x00', b'\x00\x00\x00\x02', b'\x00\x00\x00\x02{'])
def test_truncated_eof_does_not_silently_succeed(wire: bytes) -> None:
    decoder = FrameDecoder()
    assert decoder.feed(wire) == []
    with pytest.raises(ProtocolViolation, match="truncated_frame"):
        decoder.finish()


def test_total_wire_budget_includes_headers() -> None:
    limits = FrameLimits(max_frame_bytes=16, max_stream_bytes=20)
    decoder = FrameDecoder(limits)
    frame = encode_frame({"x": 1}, limits)
    assert decoder.feed(frame) == [{"x": 1}]
    with pytest.raises(ProtocolViolation, match="stream_budget"):
        decoder.feed(frame)


def test_frame_count_is_bounded_across_calls() -> None:
    decoder = FrameDecoder(FrameLimits(max_frames=1))
    assert decoder.feed(encode_frame({})) == [{}]
    with pytest.raises(ProtocolViolation, match="frame_count_budget"):
        decoder.feed(encode_frame({}))


def test_eof_seals_the_decoder() -> None:
    decoder = FrameDecoder()
    decoder.finish()
    with pytest.raises(ProtocolViolation, match="decoder_finished"):
        decoder.feed(encode_frame({}))


@pytest.mark.parametrize("message,limits,code", [
    ({"x": "中文"}, FrameLimits(max_string_bytes=5), "string_budget"),
    ({"x": [1, 2, 3]}, FrameLimits(max_container_items=2), "container_budget"),
    ({"x": [1, 2, 3]}, FrameLimits(max_nodes=4), "node_budget"),
    ({"x": float("nan")}, FrameLimits(), "non_finite_number"),
    ({"x": 2**63}, FrameLimits(), "integer_range"),
])
def test_encoding_enforces_same_data_budgets(message: dict[str, object], limits: FrameLimits,
                                           code: str) -> None:
    with pytest.raises(ProtocolViolation, match=code):
        encode_frame(message, limits)


def test_encoding_order_is_canonical_and_lowest_integer_supported() -> None:
    assert encode_frame({"z": -(2**63), "a": True}) == encode_frame({"a": True, "z": -(2**63)})
    assert decode_payload(encode_frame({"x": -(2**63)})[4:]) == {"x": -(2**63)}


@pytest.mark.parametrize("value", [["x" * 1024] * 300, "\x01" * 50000],
                         ids=("many_strings", "escaped_controls"))
def test_oversized_rendering_rejected_before_json_allocation(
    value: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def should_not_render(*args: object, **kwargs: object) -> str:
        raise AssertionError("An oversized frame reached the JSON renderer")

    monkeypatch.setattr("deskpal.execution.protocol.json.dumps", should_not_render)
    with pytest.raises(ProtocolViolation, match="frame_budget"):
        encode_frame({"payload": value})
