"""Strict local image uploads.

Rules:
* only PNG, JPEG, GIF and WebP;
* the file extension AND the file's magic bytes must agree;
* size is capped (MAX_IMAGE_BYTES);
* stored under a random name, never the user-supplied one;
* SVG is rejected on purpose (it can carry scripts).
"""
import os
import re
import shutil
import uuid

from werkzeug.utils import secure_filename

EXTENSION_TYPES = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "webp": "webp"}
STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")


class UploadError(ValueError):
    pass


def detect_image_type(head):
    """Identify an image from its first bytes. Returns 'png'|'jpg'|'gif'|'webp'|None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_upload(file_storage, folder, max_bytes):
    """Validate and store an uploaded image. Returns the stored filename or None if no file."""
    if file_storage is None or not file_storage.filename:
        return None
    original = secure_filename(file_storage.filename)
    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if ext not in EXTENSION_TYPES:
        hint = " iPhone HEIC photos: export or share them as JPG first." if ext in ("heic", "heif") else ""
        raise UploadError("Only PNG, JPG, GIF or WebP images are allowed." + hint)

    data = file_storage.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadError(f"Image is too large (max {max_bytes // (1024 * 1024) or 1} MB).")
    if not data:
        raise UploadError("The uploaded file is empty.")
    detected = detect_image_type(data[:16])
    if detected is None:
        raise UploadError("That file does not look like a valid image.")
    if detected != EXTENSION_TYPES[ext]:
        raise UploadError("The file extension does not match the image contents.")

    os.makedirs(folder, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{detected}"
    with open(os.path.join(folder, name), "xb") as fh:
        fh.write(data)
    return name


def is_stored_name(filename):
    return bool(filename) and bool(STORED_NAME_RE.fullmatch(filename))


def upload_exists(folder, filename):
    return is_stored_name(filename) and os.path.isfile(os.path.join(folder, filename))


def delete_upload(folder, filename):
    if not is_stored_name(filename):
        return
    try:
        os.remove(os.path.join(folder, filename))
    except FileNotFoundError:
        pass


def copy_upload(folder, filename):
    """Copy a stored image to a new random name (used when duplicating posts)."""
    if not is_stored_name(filename):
        return None
    src = os.path.join(folder, filename)
    if not os.path.exists(src):
        return None
    new_name = f"{uuid.uuid4().hex}.{filename.rsplit('.', 1)[1]}"
    shutil.copyfile(src, os.path.join(folder, new_name))
    return new_name
