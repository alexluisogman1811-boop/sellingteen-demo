"""Evidence upload validation and storage.

Only a small allow-list of types is accepted. The extension must match the file's
actual leading bytes, files are stored under random names outside the static folder,
and they are served back with explicit content types.
"""
import hashlib
import os
import re
import uuid

from werkzeug.utils import secure_filename


def _is_text(head):
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character may be cut off at the end of the sniffed block.
        return exc.start >= len(head) - 3
    return True


ALLOWED_TYPES = {
    "png": ("image/png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "jpg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "jpeg": ("image/jpeg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "gif": ("image/gif", lambda b: b[:6] in (b"GIF87a", b"GIF89a")),
    "webp": ("image/webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    "pdf": ("application/pdf", lambda b: b.startswith(b"%PDF-")),
    "txt": ("text/plain", _is_text),
    "log": ("text/plain", _is_text),
    "csv": ("text/csv", _is_text),
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
ALLOWED_LABEL = "PNG, JPG, GIF, WEBP, PDF, TXT, LOG or CSV"


class UploadError(ValueError):
    pass


def clean_display_name(filename):
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:150] or "evidence"


def validate_files(files, max_bytes, max_count, known_hashes=()):
    """Check uploaded FileStorage objects; return a list of (display_name, ext, mime, data, sha256).

    Files whose content is already attached (`known_hashes`) or repeated within the upload
    are dropped, so a double-submitted form doesn't duplicate evidence.
    """
    accepted = []
    seen = set(known_hashes)
    files = [f for f in files if f and f.filename]
    if len(files) > max_count:
        raise UploadError(f"Too many files: this record can take {max(max_count, 0)} more evidence file(s).")
    for f in files:
        display = clean_display_name(f.filename)
        safe = secure_filename(display)
        ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
        if ext not in ALLOWED_TYPES:
            raise UploadError(f'"{display}" is not an allowed file type ({ALLOWED_LABEL}).')
        data = f.stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise UploadError(f'"{display}" is larger than {max_bytes // (1024 * 1024)} MB.')
        if not data:
            raise UploadError(f'"{display}" is empty.')
        mime, sniff = ALLOWED_TYPES[ext]
        if not sniff(data[:2048]):
            raise UploadError(f'"{display}" does not look like a real .{ext} file.')
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        accepted.append((display, ext, mime, data, digest))
    return accepted


def store_files(db, occurrence_id, accepted, folder, stored, account_id):
    """Write validated files to disk and record them, appending stored names to `stored`."""
    os.makedirs(folder, exist_ok=True)
    for display, ext, mime, data, digest in accepted:
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(folder, stored_name), "xb") as fh:
            fh.write(data)
        stored.append(stored_name)
        db.execute(
            "INSERT INTO attachments (account_id, occurrence_id, original_name, stored_name, content_type, "
            "size_bytes, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, occurrence_id, display, stored_name, mime, len(data), digest),
        )
