"""Media type detection based on file signatures, not filename guesses.

HTTP ``Content-Type`` values and filename suffixes are useful fallbacks, but
both are routinely wrong for generated media.  The helpers in this module put
the bytes first and keep extension selection consistent across the CLI, API,
and MCP surfaces.
"""

from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path
from typing import BinaryIO


_MIME_EXTENSIONS = {
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}

_REQUESTED_IMAGE_FORMATS = {
    ".jpeg": ("image/jpeg", "JPEG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".png": ("image/png", "PNG"),
    ".webp": ("image/webp", "WEBP"),
}


class ImageConversionError(RuntimeError):
    """Raised when an image cannot be written in the requested format."""


def normalise_mime(mime: str | None) -> str:
    """Return a lowercase MIME type without parameters."""
    return str(mime or "").split(";", 1)[0].strip().lower()


def _read_header(source: bytes | bytearray | memoryview | str | os.PathLike | BinaryIO | None) -> bytes:
    if source is None:
        return b""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[:65536])
    if hasattr(source, "read"):
        stream = source
        try:
            position = stream.tell()
        except (AttributeError, OSError):
            position = None
        header = stream.read(65536)
        if position is not None:
            try:
                stream.seek(position)
            except (AttributeError, OSError):
                pass
        return bytes(header)
    try:
        with open(os.fspath(source), "rb") as media_file:
            return media_file.read(65536)
    except (FileNotFoundError, IsADirectoryError, OSError):
        return b""


def _mime_from_signature(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(header) >= 12 and header[:4] == b"RIFF":
        if header[8:12] == b"WEBP":
            return "image/webp"
        if header[8:12] == b"AVI ":
            return "video/x-msvideo"

    # ISO base media files share the ftyp container.  The major/compatible
    # brand tells image formats apart from MP4 and QuickTime video.
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brands = header[8:64]
        major_brand = header[8:12]
        if any(brand in brands for brand in (b"avif", b"avis")):
            return "image/avif"
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"hevx")):
            return "image/heic"
        if any(brand in brands for brand in (b"heif", b"heis", b"mif1", b"msf1")):
            return "image/heif"
        if major_brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"

    # WebM and Matroska both use EBML; DocType appears near the beginning.
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm" if b"webm" in header[:4096].lower() else "video/x-matroska"
    if header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"

    # SVG is text, but its root marker is still a reliable signature after an
    # optional XML declaration/whitespace.
    text_start = header[:4096].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text_start.startswith(b"<svg") or (text_start.startswith(b"<?xml") and b"<svg" in text_start):
        return "image/svg+xml"
    return None


def sniff_media_type(
    source: bytes | bytearray | memoryview | str | os.PathLike | BinaryIO | None = None,
    *,
    declared_mime: str | None = None,
    filename: str | os.PathLike | None = None,
) -> str:
    """Detect a media MIME type, preferring signature over all metadata.

    ``source`` can be bytes, an open binary stream, or a filesystem path.
    ``declared_mime`` and ``filename`` are only used when no known signature is
    present.  The function always returns a MIME string.
    """
    signature_mime = _mime_from_signature(_read_header(source))
    if signature_mime:
        return signature_mime

    fallback_mime = normalise_mime(declared_mime)
    if fallback_mime and fallback_mime != "application/octet-stream":
        return fallback_mime

    guessed_name = filename
    if guessed_name is None and isinstance(source, (str, os.PathLike)):
        guessed_name = source
    if guessed_name:
        guessed_mime = mimetypes.guess_type(os.fspath(guessed_name))[0]
        if guessed_mime:
            return normalise_mime(guessed_mime)
    return "application/octet-stream"


def extension_for_mime(mime: str | None) -> str:
    """Return the preferred extension for a MIME type, including the dot."""
    clean_mime = normalise_mime(mime)
    if clean_mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[clean_mime]
    return mimetypes.guess_extension(clean_mime, strict=False) or ""


def requested_image_mime(path: str | os.PathLike) -> str | None:
    """Return the image MIME explicitly requested by a supported suffix."""
    requested = _REQUESTED_IMAGE_FORMATS.get(Path(os.fspath(path)).suffix.lower())
    return requested[0] if requested else None


def format_name_for_mime(mime: str | None) -> str:
    """Return a concise, user-facing format name for a MIME type."""
    clean_mime = normalise_mime(mime)
    for image_mime, format_name in _REQUESTED_IMAGE_FORMATS.values():
        if clean_mime == image_mime:
            return format_name
    if "/" in clean_mime:
        return clean_mime.split("/", 1)[1].upper()
    return clean_mime.upper() or "UNKNOWN"


def _converted_image(image, target_mime: str):
    """Return an image mode that Pillow can reliably save to target_mime."""
    from PIL import Image

    if target_mime == "image/jpeg":
        if image.mode in {"RGB", "L", "CMYK"}:
            return image
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(background, rgba).convert("RGB")
        return image.convert("RGB")
    if target_mime == "image/webp" and image.mode not in {"RGB", "RGBA"}:
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    if target_mime == "image/png" and image.mode == "CMYK":
        return image.convert("RGB")
    return image


def convert_image_atomic(
    source_path: str | os.PathLike,
    output_path: str | os.PathLike,
    target_mime: str,
) -> str:
    """Convert an image and atomically replace ``output_path``.

    The temporary file is created beside the destination so ``os.replace`` is
    an atomic same-filesystem operation. Dimensions and useful colour/profile
    metadata are retained, while JPEG and WebP use high-quality settings.
    """
    target_mime = normalise_mime(target_mime)
    format_name = next(
        (
            image_format
            for image_mime, image_format in _REQUESTED_IMAGE_FORMATS.values()
            if image_mime == target_mime
        ),
        None,
    )
    if format_name is None:
        raise ImageConversionError(f"unsupported requested image format: {target_mime}")

    source = os.path.abspath(os.fspath(source_path))
    destination = os.path.abspath(os.fspath(output_path))
    temporary = f"{destination}.flow-convert-{uuid.uuid4().hex}"
    image = None
    prepared = None
    try:
        from PIL import Image

        with Image.open(source) as image:
            image.load()
            original_size = image.size
            prepared = _converted_image(image, target_mime)

            save_options = {}
            if target_mime == "image/jpeg":
                save_options.update(quality=95, subsampling=0, optimize=True)
            elif target_mime == "image/webp":
                # Lossless WebP preserves the decoded source pixels instead of
                # introducing another lossy generation during format conversion.
                save_options.update(lossless=True, quality=100, method=6)
            elif target_mime == "image/png":
                save_options.update(optimize=True)

            for metadata_key in ("icc_profile", "exif", "dpi"):
                metadata_value = image.info.get(metadata_key)
                if metadata_value is not None:
                    save_options[metadata_key] = metadata_value

            with open(temporary, "wb") as converted_file:
                prepared.save(converted_file, format=format_name, **save_options)
                converted_file.flush()
                os.fsync(converted_file.fileno())

        with open(temporary, "rb") as converted_file:
            converted_mime = sniff_media_type(converted_file)
        if converted_mime != target_mime:
            raise ImageConversionError(
                f"conversion produced {converted_mime}, expected {target_mime}"
            )
        with Image.open(temporary) as converted_image:
            if converted_image.size != original_size:
                raise ImageConversionError(
                    "conversion changed image dimensions from "
                    f"{original_size[0]}x{original_size[1]} to "
                    f"{converted_image.size[0]}x{converted_image.size[1]}"
                )

        os.replace(temporary, destination)
        return destination
    except ImageConversionError:
        raise
    except Exception as error:
        raise ImageConversionError(
            f"could not convert image to {format_name}: {error}"
        ) from error
    finally:
        if prepared is not None and prepared is not image:
            prepared.close()
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def extension_for_media(
    source: bytes | bytearray | memoryview | str | os.PathLike | BinaryIO | None = None,
    *,
    declared_mime: str | None = None,
    filename: str | os.PathLike | None = None,
) -> str:
    """Detect media bytes and return their preferred extension."""
    return extension_for_mime(
        sniff_media_type(source, declared_mime=declared_mime, filename=filename)
    )


def ensure_correct_extension(
    path: str | os.PathLike,
    *,
    declared_mime: str | None = None,
    preserve_explicit: bool = False,
) -> str:
    """Rename an auto-generated file to match its signature and return its path.

    Callers must pass ``preserve_explicit=True`` for a user-requested path: an
    explicit ``--output`` is a contract and is never silently changed.
    """
    original = os.path.abspath(os.fspath(path))
    extension = extension_for_media(original, declared_mime=declared_mime)
    if preserve_explicit or not extension:
        return original
    current_extension = Path(original).suffix.lower()
    if current_extension == extension:
        return original
    corrected = str(Path(original).with_suffix(extension)) if current_extension else original + extension
    if os.path.exists(corrected) and os.path.normcase(corrected) != os.path.normcase(original):
        raise FileExistsError(f"Refusing to replace existing media file: {corrected}")
    os.replace(original, corrected)
    return corrected
