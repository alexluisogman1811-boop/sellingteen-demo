"""Photo storage on local disk.

Uploads are validated by extension, size and by actually decoding the image with Pillow,
then re-encoded. Re-encoding drops EXIF metadata (including GPS location of the customer's
home) and anything smuggled after the image data. Files get random names and live outside
the static folder; they are only served through ownership-checked routes.

For production, swap this module for object storage (S3/R2) with private buckets.
"""
import logging
import os
import secrets

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}  # HEIC needs an extra decoder; not in MVP
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}  # MPO = multi-picture JPEG from some phones
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 2400
THUMB_SIZE = (480, 480)

Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # Pillow raises on decompression bombs above this
log = logging.getLogger(__name__)


class PhotoError(Exception):
    """Validation failure; message is safe to show to the user."""


def _business_dir(business_ref: str) -> str:
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], business_ref)
    os.makedirs(path, exist_ok=True)
    return path


def photo_path(business_ref: str, stored_name: str) -> str:
    # stored_name is always server-generated, but refuse anything path-like regardless.
    if os.path.basename(stored_name) != stored_name or stored_name.startswith("."):
        raise PhotoError("Invalid file name.")
    return os.path.join(_business_dir(business_ref), stored_name)


def save_photo(file_storage, business_ref: str) -> dict:
    """Validate and store one upload. Returns fields for a JobPhoto row."""
    original = file_storage.filename or ""
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise PhotoError(f"\"{_display_name(original)}\" isn't a supported photo. Use JPG, PNG or WebP.")

    data = file_storage.stream.read(current_app.config["MAX_PHOTO_BYTES"] + 1)
    if len(data) > current_app.config["MAX_PHOTO_BYTES"]:
        mb = current_app.config["MAX_PHOTO_BYTES"] // (1024 * 1024)
        raise PhotoError(f"\"{_display_name(original)}\" is larger than {mb} MB.")
    if not data:
        raise PhotoError(f"\"{_display_name(original)}\" is empty.")

    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in ALLOWED_FORMATS:
            raise PhotoError(f"\"{_display_name(original)}\" isn't a supported photo. Use JPG, PNG or WebP.")
        img = Image.open(BytesIO(data))
        img.load()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PhotoError(f"\"{_display_name(original)}\" isn't a valid image file.")

    img = ImageOps.exif_transpose(img)  # respect phone rotation before EXIF is dropped
    keep_png = fmt == "PNG"
    if keep_png:
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    else:
        img = img.convert("RGB")
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))

    out_ext, out_fmt, ctype = ("png", "PNG", "image/png") if keep_png else ("jpg", "JPEG", "image/jpeg")
    token = secrets.token_hex(16)
    stored_name, thumb_name = f"{token}.{out_ext}", f"{token}_t.{out_ext}"
    folder = _business_dir(business_ref)

    save_kwargs = {} if keep_png else {"quality": 85, "optimize": True}
    try:
        img.save(os.path.join(folder, stored_name), out_fmt, **save_kwargs)
        thumb = img.copy()
        thumb.thumbnail(THUMB_SIZE)
        thumb.save(os.path.join(folder, thumb_name), out_fmt, **save_kwargs)
    except OSError:
        # Disk full, permissions, path too long, etc. Log it; tell the user something useful.
        log.exception("Could not write photo to upload folder")
        delete_photo_files(business_ref, stored_name, thumb_name)
        raise PhotoError(f"We couldn't save \"{_display_name(original)}\". Please try again.")

    return {
        "stored_name": stored_name,
        "thumb_name": thumb_name,
        "content_type": ctype,
        "size_bytes": os.path.getsize(os.path.join(folder, stored_name)),
    }


def delete_photo_files(business_ref: str, *names: str) -> None:
    for name in names:
        try:
            os.remove(photo_path(business_ref, name))
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Could not delete photo file")


def _display_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")) or "File"
    return name[:60]
