"""CSRF protection, portal tokens, and upload validation."""
import hmac
import io
import os
import re
import secrets
import unicodedata
import zipfile

from flask import abort, current_app, request, session

# ---------------------------------------------------------------- CSRF

CSRF_SESSION_KEY = "_csrf_token"


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_protect():
    """before_request hook: every state-changing request needs a matching token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    expected = session.get(CSRF_SESSION_KEY)
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not sent or not hmac.compare_digest(expected, sent):
        abort(400, description="Invalid or missing CSRF token. Reload the page and try again.")


# ---------------------------------------------------------------- Portal tokens

# 32 random bytes = 256 bits of entropy, URL-safe base64 (43 chars).
PORTAL_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_portal_token() -> str:
    return secrets.token_urlsafe(PORTAL_TOKEN_BYTES)


def is_well_formed_token(token: str) -> bool:
    return bool(token) and bool(_TOKEN_RE.match(token))


# ---------------------------------------------------------------- Uploads

# extension -> (mime type, list of acceptable leading-byte signatures or None for text)
ALLOWED_UPLOADS = {
    "pdf": ("application/pdf", [b"%PDF-"]),
    "png": ("image/png", [b"\x89PNG\r\n\x1a\n"]),
    "jpg": ("image/jpeg", [b"\xff\xd8\xff"]),
    "jpeg": ("image/jpeg", [b"\xff\xd8\xff"]),
    "gif": ("image/gif", [b"GIF87a", b"GIF89a"]),
    "webp": ("image/webp", [b"RIFF"]),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", [b"PK\x03\x04"]),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", [b"PK\x03\x04"]),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", [b"PK\x03\x04"]),
    "txt": ("text/plain", None),
    "csv": ("text/csv", None),
    "md": ("text/markdown", None),
}

# Signatures of executables/scripts we refuse regardless of the claimed extension.
EXECUTABLE_SIGNATURES = (
    b"MZ",               # Windows PE (.exe, .dll, .scr)
    b"\x7fELF",          # Linux ELF
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",  # Mach-O
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # Mach-O fat / Java class
    b"#!",               # shell/script shebang
)


class UploadError(ValueError):
    pass


# Office Open XML files are zip archives; check they really are the claimed document type.
OOXML_PARTS = {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}
# Macros, embedded OLE objects (can wrap programs), and executables. Plain *.bin parts such as
# printerSettings1.bin are normal in Excel files and allowed.
_RISKY_MEMBER = re.compile(
    r"(?i)(vbaproject\.bin|oleobject\d*\.bin|\.(exe|dll|bat|cmd|com|scr|msi|ps1|sh|js|vbs|jar)$)")


def _check_office_file(data: bytes, ext: str):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, ValueError):
        raise UploadError(f"File content does not match the .{ext} file type.")
    if "[Content_Types].xml" not in names or not any(n.startswith(OOXML_PARTS[ext]) for n in names):
        raise UploadError(f"File content does not match the .{ext} file type.")
    if any(_RISKY_MEMBER.search(n) for n in names):
        raise UploadError("Documents containing macros or embedded programs are not allowed.")


def safe_display_name(filename: str) -> str:
    """Sanitised original filename, used only for display and download names.

    Files are never stored under this name; see random stored names below.
    """
    name = unicodedata.normalize("NFKC", filename or "")
    name = name.replace("\\", "/").split("/")[-1]           # drop any directory part
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)              # control characters
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")      # conservative charset
    name = re.sub(r"\.{2,}", ".", name)
    if not name:
        return "file"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    # Windows reserved device names (CON, NUL, COM1, ...) are unsafe as download names.
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", stem or name):
        stem = "_" + (stem or name)
    # Truncate the stem, never the extension, so long names stay valid.
    ext = ext[:10]
    stem = stem[: 120 - len(ext) - 1] if ext else stem[:120]
    return f"{stem}.{ext}" if ext and stem else (stem or ext or "file")


def file_extension(filename: str) -> str:
    name = safe_display_name(filename)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_upload(file_storage):
    """Return (display_name, ext, mime, data) or raise UploadError."""
    if file_storage is None or not file_storage.filename:
        raise UploadError("Choose a file to upload.")
    display = safe_display_name(file_storage.filename)
    ext = file_extension(file_storage.filename)
    if ext not in ALLOWED_UPLOADS:
        allowed = ", ".join(sorted(ALLOWED_UPLOADS))
        raise UploadError(f"File type '.{ext or '?'}' is not allowed. Allowed types: {allowed}.")
    # Reject double extensions that hide an executable, e.g. invoice.exe.pdf
    parts = display.lower().split(".")
    blocked = {"exe", "bat", "cmd", "com", "scr", "msi", "ps1", "sh", "js", "vbs", "jar", "dll", "php", "py", "html", "htm", "svg"}
    if any(p in blocked for p in parts[1:-1]):
        raise UploadError("Files with executable or script extensions are not allowed.")

    max_bytes = current_app.config["MAX_UPLOAD_BYTES"]
    data = file_storage.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadError(f"File is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")
    if not data:
        raise UploadError("The file is empty.")
    if data.startswith(EXECUTABLE_SIGNATURES):
        raise UploadError("Executable files are not allowed.")

    mime, signatures = ALLOWED_UPLOADS[ext]
    if signatures is None:
        # Text formats: must decode as UTF-8 text and contain no NUL bytes.
        if b"\x00" in data:
            raise UploadError("Text files must not contain binary data.")
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadError("Text files must be UTF-8 encoded.")
    elif not any(data.startswith(sig) for sig in signatures):
        raise UploadError(f"File content does not match the .{ext} file type.")
    if ext == "webp" and data[8:12] != b"WEBP":
        raise UploadError("File content does not match the .webp file type.")
    if ext in OOXML_PARTS:
        _check_office_file(data, ext)
    return display, ext, mime, data


def store_upload(data: bytes, ext: str) -> str:
    """Write bytes under a random name inside the upload folder; return that name."""
    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    stored = f"{secrets.token_hex(16)}.{ext}"
    path = upload_path(stored)
    with open(path, "xb") as fh:  # 'x' never overwrites an existing file
        fh.write(data)
    return stored


def upload_path(stored_name: str) -> str:
    """Resolve a stored name to an absolute path, refusing anything outside the folder."""
    # Stored names are server-generated hex, so the regex alone rules out traversal;
    # the directory check is defence in depth. abspath (not realpath) because realpath
    # can resolve to a virtualised path on Windows app installs that is not writable.
    if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,5}", stored_name or ""):
        abort(404)
    folder = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
    path = os.path.abspath(os.path.join(folder, stored_name))
    if os.path.dirname(path) != folder:
        abort(404)
    return path


def delete_stored_file(stored_name: str):
    try:
        os.remove(upload_path(stored_name))
    except FileNotFoundError:
        pass
