"""
Photo upload server for Raspberry Pi.

Receives photos over HTTP (from an iPhone via the Shortcuts app, or from
any browser), validates them, stores them in date-based folders, tracks
metadata in SQLite, and serves a small gallery.

Endpoints:
    POST /upload        upload one or more photos (auth required)
    GET  /upload        browser upload form (auth required)
    GET  /              gallery (auth required)
    GET  /thumb/<id>    thumbnail (auth required)
    GET  /photo/<id>    original file (auth required)
    GET  /health        unauthenticated liveness check
"""

import hashlib
import hmac
import io
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_file
from PIL import Image, ImageOps
from werkzeug.exceptions import HTTPException

# HEIC/HEIF support -- the iPhone's default photo format.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

# ---------------------------------------------------------------- config

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", BASE_DIR / "photos"))
THUMBS_DIR = Path(os.environ.get("THUMBS_DIR", PHOTOS_DIR / ".thumbs"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "photos.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE_DIR / "logs"))

API_KEY = os.environ.get("API_KEY", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))  # per request
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "500"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# Pillow format name -> extension used on disk.
ALLOWED_FORMATS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "HEIF": ".heic",
    "GIF": ".gif",
    "WEBP": ".webp",
    "TIFF": ".tif",
    "BMP": ".bmp",
}

HEIC_MAGIC = (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc", b"ftypheim")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# --------------------------------------------------------------- logging


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        LOG_DIR / "server.log", maxBytes=1_000_000, backupCount=3
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger = logging.getLogger("photoserver")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger


log = setup_logging()

# -------------------------------------------------------------- database


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256        TEXT NOT NULL UNIQUE,
                original_name TEXT,
                stored_path   TEXT NOT NULL,
                format        TEXT,
                size_bytes    INTEGER,
                device        TEXT,
                uploaded_at   TEXT NOT NULL
            )
            """
        )


def get_photo_row(photo_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()


# ------------------------------------------------------------------ auth


def request_key() -> str:
    return (
        request.headers.get("X-API-Key")
        or request.args.get("key")
        or request.form.get("key")
        or ""
    )


def require_key() -> None:
    if not API_KEY or not hmac.compare_digest(request_key(), API_KEY):
        abort(401, description="Missing or invalid API key.")


# --------------------------------------------------------------- helpers


def free_mb(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


def identify_format(data: bytes):
    """Return Pillow's format name if data is a readable image, else None."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # integrity check; invalidates the object
        with Image.open(io.BytesIO(data)) as im:
            return im.format
    except Exception:
        return None


def safe_stem(name: str) -> str:
    stem = Path(name or "").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    return stem[:40]


def store_photo(file_storage, device: str) -> dict:
    original = file_storage.filename or ""
    data = file_storage.read()
    if not data:
        return {"original_name": original, "status": "rejected", "reason": "empty file"}

    sha = hashlib.sha256(data).hexdigest()
    with db() as conn:
        row = conn.execute("SELECT id FROM photos WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        return {"original_name": original, "status": "duplicate", "id": row["id"]}

    fmt = identify_format(data)
    if fmt is None and len(data) > 12 and data[4:12] in HEIC_MAGIC:
        return {
            "original_name": original,
            "status": "rejected",
            "reason": "HEIC photo received but pillow-heif is not installed on the server",
        }
    if fmt not in ALLOWED_FORMATS:
        return {
            "original_name": original,
            "status": "rejected",
            "reason": f"not a supported image (detected: {fmt})",
        }

    now = datetime.now()
    subdir = PHOTOS_DIR / now.strftime("%Y/%m/%d")
    subdir.mkdir(parents=True, exist_ok=True)

    stem = safe_stem(original)
    name = (
        now.strftime("%Y%m%d-%H%M%S")
        + "_"
        + sha[:8]
        + ("_" + stem if stem else "")
        + ALLOWED_FORMATS[fmt]
    )
    dest = subdir / name
    dest.write_bytes(data)

    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO photos (sha256, original_name, stored_path, format,"
                " size_bytes, device, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sha,
                    original,
                    str(dest.relative_to(PHOTOS_DIR)),
                    fmt,
                    len(data),
                    device,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            photo_id = cur.lastrowid
    except sqlite3.IntegrityError:  # same photo uploaded concurrently
        dest.unlink(missing_ok=True)
        return {"original_name": original, "status": "duplicate"}

    log.info(
        "stored %s (%d bytes, %s) from device=%r -> %s",
        original or "<unnamed>", len(data), fmt, device, dest,
    )
    return {
        "original_name": original,
        "status": "stored",
        "id": photo_id,
        "path": str(dest.relative_to(PHOTOS_DIR)),
    }


# ---------------------------------------------------------------- routes


@app.post("/upload")
def upload():
    require_key()
    if free_mb(PHOTOS_DIR) < MIN_FREE_MB:
        # abort(507) isn't supported by werkzeug's exception map, so return directly.
        log.error("upload refused: free space below MIN_FREE_MB=%d", MIN_FREE_MB)
        return (
            jsonify({"error": "Insufficient Storage",
                     "detail": "Not enough free disk space on the server."}),
            507,
        )

    files = []
    for field in request.files:
        files.extend(request.files.getlist(field))
    if not files:
        abort(400, description="No file in request. Send multipart/form-data with a 'file' field.")

    device = request.form.get("device", "")
    results = [store_photo(f, device) for f in files]
    stored = sum(1 for r in results if r["status"] == "stored")
    dupes = sum(1 for r in results if r["status"] == "duplicate")
    rejected = len(results) - stored - dupes
    return (
        jsonify({"stored": stored, "duplicates": dupes, "rejected": rejected, "results": results}),
        200 if (stored or dupes) else 400,
    )


UPLOAD_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;padding:2rem;max-width:30rem;margin:auto}
 input,button{font-size:1rem;margin:.5rem 0;display:block}
 button{padding:.5rem 1.5rem}a{color:#8cf}
</style></head><body>
<h1>Upload photos</h1>
<form method="post" action="/upload" enctype="multipart/form-data">
  <input type="hidden" name="key" value="{{ key }}">
  <input type="hidden" name="device" value="browser">
  <input type="file" name="file" accept="image/*" multiple required>
  <button>Upload</button>
</form>
<p><a href="/?key={{ key }}">back to gallery</a></p>
</body></html>"""


@app.get("/upload")
def upload_form():
    require_key()
    return render_template_string(UPLOAD_PAGE, key=request_key())


GALLERY_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Photos</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:1rem}
 header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:.5rem}
 a{color:#8cf}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-top:1rem}
 .grid img{width:100%;height:150px;object-fit:cover;border-radius:6px;display:block}
 .meta{color:#999;font-size:.85rem}
</style></head><body>
<header>
  <h1 style="margin:0">Pi Photos</h1>
  <span class="meta">{{ total }} photos &middot; {{ free }} MB free &middot;
    <a href="/upload?key={{ key }}">upload</a></span>
</header>
{% if photos %}
<div class="grid">
  {% for p in photos %}
  <a href="/photo/{{ p['id'] }}?key={{ key }}"
     title="{{ p['original_name'] }} · {{ p['uploaded_at'] }} · {{ p['device'] }}">
    <img loading="lazy" src="/thumb/{{ p['id'] }}?key={{ key }}" alt="">
  </a>
  {% endfor %}
</div>
{% else %}
<p class="meta">No photos yet. POST to /upload or use the
  <a href="/upload?key={{ key }}">upload page</a>.</p>
{% endif %}
</body></html>"""


@app.get("/")
def gallery():
    require_key()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
        rows = conn.execute(
            "SELECT id, original_name, uploaded_at, device FROM photos"
            " ORDER BY id DESC LIMIT 500"
        ).fetchall()
    return render_template_string(
        GALLERY_PAGE,
        photos=[dict(r) for r in rows],
        total=total,
        free=free_mb(PHOTOS_DIR),
        key=request_key(),
    )


@app.get("/thumb/<int:photo_id>")
def thumb(photo_id: int):
    require_key()
    row = get_photo_row(photo_id)
    if row is None:
        abort(404)
    tpath = THUMBS_DIR / f"{photo_id}.jpg"
    if not tpath.exists():
        src = PHOTOS_DIR / row["stored_path"]
        if not src.exists():
            abort(404)
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)  # respect iPhone rotation metadata
            im = im.convert("RGB")
            im.thumbnail((480, 480))
            im.save(tpath, "JPEG", quality=80)
    return send_file(tpath, mimetype="image/jpeg")


@app.get("/photo/<int:photo_id>")
def photo(photo_id: int):
    require_key()
    row = get_photo_row(photo_id)
    if row is None:
        abort(404)
    src = PHOTOS_DIR / row["stored_path"]
    if not src.exists():
        abort(404)
    mt = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
    return send_file(src, mimetype=mt)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "free_mb": free_mb(PHOTOS_DIR), "heic_support": HEIC_SUPPORT})


@app.errorhandler(HTTPException)
def as_json(e: HTTPException):
    return jsonify({"error": e.name, "detail": e.description}), e.code


# ------------------------------------------------------------------ main


def main() -> None:
    if not API_KEY:
        raise SystemExit(
            "API_KEY is not set. Generate one with:\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then put it in config.env (API_KEY=...) and restart."
        )
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    log.info(
        "serving on %s:%d | photos -> %s | HEIC support: %s",
        HOST, PORT, PHOTOS_DIR, HEIC_SUPPORT,
    )
    try:
        from waitress import serve  # production-grade WSGI server

        serve(app, host=HOST, port=PORT)
    except ImportError:
        app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
