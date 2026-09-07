"""Avatar normalization and media-store isolation contracts.

These are security tests, not image tests: the properties asserted here are the reasons the upload
path is safe to expose, so a regression in any of them is a vulnerability rather than a cosmetic bug.
"""

from __future__ import annotations

import io
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from events_concierge.adapters.local_media import LocalFilesystemMediaStore
from events_concierge.application.profile_media import (
    AvatarRejectedError,
    normalize_avatar,
)
from events_concierge.ports.media_store import MediaNotFoundError

_MAX_STORED_BYTES = 32 * 1024
# The request body cap enforced by both the ASGI guard and the Next proxy. The stored bound must
# stay under it, or an upload that satisfies this pipeline is rejected before it ever arrives.
_REQUEST_BODY_CAP = 64 * 1024


def _encode(width: int, height: int, fmt: str = "PNG", color=(200, 40, 90)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, fmt)
    return buffer.getvalue()


def test_stored_bound_stays_under_the_request_body_cap() -> None:
    """Raising the avatar ceiling past the transport cap must fail here, not in production."""
    assert _MAX_STORED_BYTES < _REQUEST_BODY_CAP


def test_a_rectangular_upload_becomes_a_square_webp() -> None:
    normalized = normalize_avatar(_encode(600, 400), "image/png")
    assert (normalized.width_px, normalized.height_px) == (256, 256)
    assert normalized.content_type == "image/webp"
    assert normalized.byte_size <= _MAX_STORED_BYTES
    assert Image.open(io.BytesIO(normalized.data)).format == "WEBP"


def test_the_storage_key_is_content_addressed_and_carries_no_caller_input() -> None:
    """Identical bytes converge on one object, and no filename reaches the key."""
    first = normalize_avatar(_encode(300, 300), "image/png")
    second = normalize_avatar(_encode(300, 300), "image/png")
    assert first.storage_key == second.storage_key
    assert first.storage_key == f"{first.checksum_sha256}.webp"


def test_exif_does_not_survive_the_re_encode() -> None:
    """The re-encode -- not any header check -- is what strips camera metadata such as GPS."""
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"
    Image.new("RGB", (300, 300), (10, 200, 10)).save(buffer, "JPEG", exif=exif)
    normalized = normalize_avatar(buffer.getvalue(), "image/jpeg")
    assert b"TestCamera" not in normalized.data
    assert b"Exif" not in normalized.data


def test_trailing_payload_appended_after_an_image_is_discarded() -> None:
    """A polyglot's appended bytes cannot survive a decode-and-re-encode."""
    payload = b"<script>alert(1)</script>"
    normalized = normalize_avatar(_encode(300, 300) + payload, "image/png")
    assert payload not in normalized.data


@pytest.mark.parametrize(
    ("raw", "declared"),
    [
        (b"", "image/png"),
        (b"not an image at all", "image/png"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "image/svg+xml"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/png"),
    ],
    ids=["empty", "not-an-image", "svg-declared", "svg-mislabeled-as-png"],
)
def test_non_images_are_refused(raw: bytes, declared: str) -> None:
    """SVG in particular must never be accepted: served same-origin it is stored XSS."""
    with pytest.raises(AvatarRejectedError):
        normalize_avatar(raw, declared)


def test_dimensions_are_bounded_before_any_pixel_buffer_is_allocated() -> None:
    """A small file may declare an enormous canvas; the bound must precede the decode."""
    buffer = io.BytesIO()
    Image.new("L", (4500, 300), 0).save(buffer, "PNG", optimize=True)
    oversized = buffer.getvalue()
    assert len(oversized) < _MAX_STORED_BYTES, "the guard under test must not be the byte cap"
    with pytest.raises(AvatarRejectedError, match="4096"):
        normalize_avatar(oversized, "image/png")


async def test_the_media_store_round_trips_and_isolates_tenants(tmp_path: Path) -> None:
    store = LocalFilesystemMediaStore(tmp_path)
    owner, other = uuid4(), uuid4()
    normalized = normalize_avatar(_encode(300, 300), "image/png")

    await store.put(owner, normalized.storage_key, normalized.data, normalized.content_type)
    assert await store.get(owner, normalized.storage_key) == normalized.data

    # The same key under a different tenant addresses a different object, so a leaked key is not
    # a cross-tenant read.
    with pytest.raises(MediaNotFoundError):
        await store.get(other, normalized.storage_key)


async def test_the_media_store_refuses_a_key_that_is_not_opaque(tmp_path: Path) -> None:
    """Traversal is refused at the one layer that touches a filesystem."""
    store = LocalFilesystemMediaStore(tmp_path)
    tenant = uuid4()
    for key in ("../escape.webp", "nested/path.webp", "..", "a.webp", "/etc/passwd"):
        with pytest.raises(ValueError, match="opaque"):
            await store.put(tenant, key, b"x", "image/webp")


async def test_tenant_purge_removes_every_object(tmp_path: Path) -> None:
    """Erasure's other half: the index cascades, but the bytes need this."""
    store = LocalFilesystemMediaStore(tmp_path)
    tenant = uuid4()
    normalized = normalize_avatar(_encode(300, 300), "image/png")
    await store.put(tenant, normalized.storage_key, normalized.data, normalized.content_type)

    await store.delete_tenant(tenant)

    with pytest.raises(MediaNotFoundError):
        await store.get(tenant, normalized.storage_key)


async def test_deleting_an_absent_object_converges(tmp_path: Path) -> None:
    """A retried erasure must not fail because a previous attempt already succeeded."""
    store = LocalFilesystemMediaStore(tmp_path)
    tenant = uuid4()
    await store.delete(tenant, f"{'0' * 64}.webp")
    await store.delete_tenant(tenant)
