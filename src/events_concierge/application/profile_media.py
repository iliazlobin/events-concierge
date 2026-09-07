"""Decode, validate and re-encode an uploaded avatar.

This module is request-path security code, not image plumbing, and its central rule is that the
uploaded bytes are never stored.  Everything the product later serves is produced by a full decode
followed by a fresh encode, which is what actually strips EXIF (including GPS coordinates a phone
photo carries), discards trailing data appended after an image's end marker, and collapses any
polyglot back into pixels.  A header inspection alone would do none of that.

The declared ``Content-Type`` is read for a fast rejection and then discarded; the format decision
comes from the decoder.  Dimensions are bounded *before* the pixel buffer is materialized, because a
few kilobytes of valid PNG can declare a 40000x40000 canvas whose decode would exhaust memory.

``image/svg+xml`` is absent from the allowlist and must stay absent.  SVG is a document format with
script semantics; served same-origin under ``img-src 'self'`` it is stored cross-site scripting
against the very origin that holds the readable CSRF cookie. It also fails this decoder outright,
so the omission is enforced by construction rather than by a check someone could relax.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageOps

# A decompression-bomb ceiling applied by Pillow itself, set well under the default so a hostile
# upload trips it before allocation rather than after.
Image.MAX_IMAGE_PIXELS = 8_000_000

_ACCEPTED_UPLOAD_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_UPLOAD_BYTES: Final = 32 * 1024
_MAX_SOURCE_EDGE: Final = 4096
_OUTPUT_EDGE: Final = 256
_OUTPUT_CONTENT_TYPE: Final = "image/webp"
# Descending quality ladder: the first rendition that fits the storage bound wins, so a detailed
# photograph degrades in quality rather than failing an upload outright.
_QUALITY_LADDER: Final = (86, 74, 62, 50)

# 'RIFF' + 4 length bytes + 'WEBP' is the shortest prefix that identifies a WebP container.
_RIFF_HEADER_BYTES: Final = 12

_MAGIC_PREFIXES: Final = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
)


class AvatarRejectedError(ValueError):
    """The upload is not an image this pipeline will accept.

    The message is a fixed, human-readable reason. Decoder exception text never reaches it: that
    text can echo attacker-controlled bytes and describes internals a caller has no business seeing.
    """


@dataclass(frozen=True, slots=True)
class NormalizedAvatar:
    """The re-encoded rendition and the facts only a real decode can establish."""

    data: bytes
    content_type: str
    width_px: int
    height_px: int
    byte_size: int
    checksum_sha256: str

    @property
    def storage_key(self) -> str:
        """Content-addressed object name.

        Addressing by digest means an identical re-upload converges on the same object, and no part
        of the key is caller-supplied -- there is no filename anywhere in this pipeline.
        """
        return f"{self.checksum_sha256}.webp"


def normalize_avatar(raw: bytes, declared_content_type: str | None) -> NormalizedAvatar:
    """Turn an untrusted upload into one bounded, metadata-free 256x256 WebP.

    Raises ``AvatarRejectedError`` for every rejection so the HTTP layer answers with one shape.
    """
    if not raw:
        raise AvatarRejectedError("The image was empty.")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise AvatarRejectedError("Images must be 32 KB or smaller after cropping.")

    declared = (declared_content_type or "").split(";")[0].strip().lower()
    if declared not in _ACCEPTED_UPLOAD_TYPES:
        raise AvatarRejectedError("Upload a PNG, JPEG, or WebP image.")
    if not _has_image_magic(raw):
        # The declared type and the bytes disagree; trusting either one over the other is how
        # content-type confusion becomes a stored payload.
        raise AvatarRejectedError("That file is not a PNG, JPEG, or WebP image.")

    try:
        with Image.open(io.BytesIO(raw)) as source:
            _reject_unsupported_shape(source)
            # ``load`` is where pixels are actually allocated, so every bound above precedes it.
            source.load()
            oriented = ImageOps.exif_transpose(source) or source
            square = _center_square(oriented.convert("RGBA"))
            resized = square.resize((_OUTPUT_EDGE, _OUTPUT_EDGE), Image.Resampling.LANCZOS)
            # Compose onto a brand-new canvas: nothing from the source image object -- no ancillary
            # chunks, no ICC profile, no EXIF block -- survives into what gets encoded.
            canvas = Image.new("RGBA", (_OUTPUT_EDGE, _OUTPUT_EDGE))
            canvas.paste(resized)
            encoded = _encode_within_budget(canvas)
    except AvatarRejectedError:
        raise
    except Exception as error:
        # Any decoder failure is one product-level rejection; decoder text never reaches a caller.
        raise AvatarRejectedError("That image could not be read.") from error

    return NormalizedAvatar(
        data=encoded,
        content_type=_OUTPUT_CONTENT_TYPE,
        width_px=_OUTPUT_EDGE,
        height_px=_OUTPUT_EDGE,
        byte_size=len(encoded),
        checksum_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _has_image_magic(raw: bytes) -> bool:
    """Confirm the leading bytes are a format this pipeline decodes."""
    if raw.startswith(_MAGIC_PREFIXES):
        return True
    # WebP is a RIFF container: 'RIFF' <4-byte length> 'WEBP'.
    return (
        len(raw) >= _RIFF_HEADER_BYTES and raw[0:4] == b"RIFF" and raw[8:12] == b"WEBP"
    )


def _reject_unsupported_shape(source: Image.Image) -> None:
    """Bound dimensions and refuse animations before any pixel buffer is created."""
    width, height = source.size
    if width <= 0 or height <= 0:
        raise AvatarRejectedError("That image could not be read.")
    if width > _MAX_SOURCE_EDGE or height > _MAX_SOURCE_EDGE:
        raise AvatarRejectedError("Images must be 4096 pixels or smaller on each side.")
    if width * height > Image.MAX_IMAGE_PIXELS:
        raise AvatarRejectedError("That image is too large to process.")
    if getattr(source, "n_frames", 1) > 1:
        # One stored still cannot represent an animation, and multi-frame decoding multiplies the
        # work an upload can demand.
        raise AvatarRejectedError("Animated images are not supported.")


def _center_square(image: Image.Image) -> Image.Image:
    """Crop to the largest centred square so the circular avatar frame never distorts a face."""
    width, height = image.size
    if width == height:
        return image
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    return image.crop((left, top, left + edge, top + edge))


def _encode_within_budget(canvas: Image.Image) -> bytes:
    """Encode down the quality ladder until the rendition fits the storage bound."""
    for quality in _QUALITY_LADDER:
        buffer = io.BytesIO()
        canvas.save(buffer, format="WEBP", quality=quality, method=4, exif=b"", icc_profile=None)
        encoded = buffer.getvalue()
        if len(encoded) <= _MAX_UPLOAD_BYTES:
            return encoded
    raise AvatarRejectedError("That image could not be compressed small enough. Try another.")
