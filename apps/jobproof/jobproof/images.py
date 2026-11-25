"""Upload validation and safe storage for photos and logos.

Every upload is decoded with Pillow and re-encoded. That rejects anything that is not
a real image (including executables renamed to .jpg), strips EXIF/GPS metadata and
normalises orientation. Files are stored under random names chosen by the server.
"""
import io
import os
import re
import secrets
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_PIXELS = 40_000_000  # decompression-bomb guard
KEY_RE = re.compile(r"^[a-f0-9]{32}$")

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageValidationError(ValueError):
    """Raised with a user-friendly message."""


def _read_limited(file_storage, max_bytes):
    data = file_storage.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        mb = max_bytes // (1024 * 1024)
        raise ImageValidationError(f"“{_display_name(file_storage)}” is too large. The limit is {mb} MB per image.")
    if not data:
        raise ImageValidationError(f"“{_display_name(file_storage)}” is empty.")
    return data


def _display_name(file_storage):
    name = os.path.basename(file_storage.filename or "file")
    return name[:60]


def process_upload(file_storage, max_bytes, max_dimension=2000):
    """Validate an uploaded image and return (bytes, ext, width, height)."""
    name = file_storage.filename or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ImageValidationError(
            f"“{_display_name(file_storage)}” isn't a supported image. Please upload JPG, PNG or WEBP files."
        )
    data = _read_limited(file_storage, max_bytes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            probe = Image.open(io.BytesIO(data))
            fmt = probe.format
            probe.verify()
            img = Image.open(io.BytesIO(data))
            img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, SyntaxError, ValueError):
        raise ImageValidationError(
            f"“{_display_name(file_storage)}” couldn't be read as an image. Please upload a JPG, PNG or WEBP photo."
        )
    if fmt not in ALLOWED_FORMATS:
        raise ImageValidationError(
            f"“{_display_name(file_storage)}” isn't a supported image. Please upload JPG, PNG or WEBP files."
        )

    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_dimension, max_dimension))
    out = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if fmt == "PNG" and has_alpha:
        img = img.convert("RGBA")
        img.save(out, format="PNG", optimize=True)
        out_ext = "png"
    else:
        img = img.convert("RGB")
        img.save(out, format="JPEG", quality=85, optimize=True)
        out_ext = "jpg"
    return out.getvalue(), out_ext, img.width, img.height


def new_key():
    return secrets.token_hex(16)


def stored_path(upload_root, kind, key, ext):
    """Build a storage path from server-generated values only (no user input)."""
    if kind not in ("photos", "logos") or not KEY_RE.match(key or "") or ext not in ("jpg", "png"):
        raise ValueError("invalid storage reference")
    return os.path.join(upload_root, kind, f"{key}.{ext}")


def save_bytes(upload_root, kind, key, ext, data):
    path = stored_path(upload_root, kind, key, ext)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def remove_file(upload_root, kind, key, ext):
    try:
        os.remove(stored_path(upload_root, kind, key, ext))
    except (FileNotFoundError, ValueError):
        pass
