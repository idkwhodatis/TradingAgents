"""Versioned, bounded application-level compression for run artifacts.

Version 1 wraps content in a three-byte marker before Zstandard compression.
The marker makes even empty artifacts non-empty frames, avoiding decoder APIs
that special-case zero-size frames without validating their checksum/tail.
``raw_length`` always describes the original content, excluding this marker.
"""

import zstandard

CODEC = "zstd"
CODEC_VERSION = 1
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
HARD_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MARKER = b"TA\x01"


class StorageError(RuntimeError):
    """Base error for a run store."""


class CorruptArtifactError(StorageError):
    """An artifact is invalid, unsupported, or exceeds the configured bound."""


class SchemaVersionError(StorageError):
    """The database is not this application's supported storage schema."""


def validate_limit(limit: int) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 < limit <= HARD_MAX_ARTIFACT_BYTES
    ):
        raise ValueError(f"max_artifact_bytes must be between 1 and {HARD_MAX_ARTIFACT_BYTES}")
    return limit


def encode(content: str | bytes, limit: int) -> tuple[bytes, int, str]:
    """Return compressed bytes, original byte length, and content encoding."""
    if isinstance(content, str):
        raw, encoding = content.encode("utf-8"), "utf-8"
    elif isinstance(content, bytes):
        raw, encoding = content, "binary"
    else:
        raise TypeError("artifact content must be str or bytes; serialize JSON explicitly")
    if len(raw) > limit:
        raise ValueError(f"artifact exceeds the {limit}-byte uncompressed limit")
    payload = zstandard.ZstdCompressor(
        level=3, write_content_size=True, write_checksum=True
    ).compress(_MARKER + raw)
    return payload, len(raw), encoding


def validate_header(codec, version, raw_length, stored_bytes, encoding, limit):
    """Validate database metadata before fetching a potentially large blob."""
    if codec != CODEC or type(version) is not int or version != CODEC_VERSION:
        raise CorruptArtifactError("unsupported artifact codec or version")
    if type(raw_length) is not int or not 0 <= raw_length <= limit:
        raise CorruptArtifactError("invalid or oversized artifact raw length")
    # Zstd can add a small amount of overhead to incompressible content.
    if type(stored_bytes) is not int or not 0 < stored_bytes <= limit + 131072:
        raise CorruptArtifactError("invalid or oversized compressed artifact")
    if encoding not in {"utf-8", "binary"}:
        raise CorruptArtifactError("unsupported artifact encoding")


def decode(payload: bytes, codec, version, raw_length, encoding, limit: int) -> str | bytes:
    validate_header(codec, version, raw_length, len(payload), encoding, limit)
    try:
        frame = zstandard.get_frame_parameters(payload)
        expected = raw_length + len(_MARKER)
        # One-shot decompression allocates from the frame header; check it first.
        # Unknown sizes, dictionaries, huge windows, and missing checksums are
        # never emitted by this codec and must not be accepted on read.
        if frame.content_size != expected or frame.window_size > limit + len(_MARKER):
            raise CorruptArtifactError(
                "artifact frame size/window does not match its bounded header"
            )
        if frame.dict_id or not frame.has_checksum:
            raise CorruptArtifactError("artifact requires a dictionary or has no checksum")
        decoded = zstandard.ZstdDecompressor().decompress(
            payload, max_output_size=expected, allow_extra_data=False
        )
        if len(decoded) != expected or not decoded.startswith(_MARKER):
            raise CorruptArtifactError("artifact content length or version marker is invalid")
        raw = decoded[len(_MARKER) :]
        return raw.decode("utf-8") if encoding == "utf-8" else raw
    except (zstandard.ZstdError, UnicodeError) as exc:
        raise CorruptArtifactError("invalid compressed artifact") from exc
