"""
Photo server for Raspberry Pi.

Multi-user: each account sees only its own photos. The browser authenticates
with a cookie session; the iPhone Shortcut authenticates with a per-user
upload token, so uploads are attributed to the right account.

Endpoints:
    GET  /login /logout /signup     account access (signup needs an invite code)
    GET  /                          gallery (session)
    GET  /account                   personal upload token (session)
    POST /upload                    upload one or more photos (session or token)
    GET  /upload                    browser upload form (session)
    GET  /thumb/<id>                480px JPEG thumbnail (session or token)
    GET  /photo/<id>                display copy, always browser-renderable
    GET  /original/<id>             untouched original bytes, as a download
    GET  /health                    unauthenticated liveness check

Run `python app.py create-user <name>` to make the first account.
"""

from __future__ import annotations  # keeps the type hints working on Python 3.9

import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, stream_with_context, url_for,
)
from PIL import Image, ImageOps
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

# HEIC/HEIF support -- the iPhone's default photo format.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False

# guess_type() doesn't know these on most Linux installs, which makes browsers
# download HEICs as octet-stream instead of trying to display them.
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/mp4", ".mp4")

# Video support -- poster frames need ffmpeg (apt install ffmpeg). Ingest and
# playback work without it; tiles just fall back to the placeholder, mirroring
# how HEIC degrades when pillow-heif is missing.
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
VIDEO_SUPPORT = bool(FFMPEG)

# ---------------------------------------------------------------- config

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", BASE_DIR / "photos"))
THUMBS_DIR = Path(os.environ.get("THUMBS_DIR", PHOTOS_DIR / ".thumbs"))
DISPLAY_DIR = Path(os.environ.get("DISPLAY_DIR", PHOTOS_DIR / ".display"))
AVATARS_DIR = Path(os.environ.get("AVATARS_DIR", PHOTOS_DIR / ".avatars"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "photos.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE_DIR / "logs"))
SECRET_KEY_PATH = Path(os.environ.get("SECRET_KEY_PATH", BASE_DIR / "secret.key"))

API_KEY = os.environ.get("API_KEY", "")
ALLOW_LEGACY_KEY = os.environ.get("ALLOW_LEGACY_KEY", "1") == "1"
INVITE_CODE = os.environ.get("INVITE_CODE", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))  # per request
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "4096"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

PAGE_SIZE = 200
THUMB_EDGE = 480
DISPLAY_EDGE = 2048

# Retrieval limits. A bulk endpoint turns one leaked token into a whole-library
# download, so cap what a single request can pull and log every export.
API_MAX_LIMIT = 1000
API_DEFAULT_LIMIT = 200
MAX_EXPORT_FILES = 500

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

# Formats every browser can render, so /photo can serve the original untouched.
BROWSER_SAFE = {"JPEG", "PNG", "GIF", "WEBP"}

HEIC_MAGIC = (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc", b"ftypheim")

# ISO-BMFF brands that mean "video container", checked only after Pillow and
# the HEIC hint pass on a file. iPhone camera videos are brand "qt  " (.mov).
VIDEO_BRANDS = {
    b"qt  ": "MOV",
    b"isom": "MP4",
    b"iso2": "MP4",
    b"mp41": "MP4",
    b"mp42": "MP4",
    b"mp4v": "MP4",
    b"avc1": "MP4",
    b"M4V ": "MP4",
}
VIDEO_EXT = {"MOV": ".mov", "MP4": ".mp4"}

# Hashing cost is a login-path concern only. PBKDF2 rather than Werkzeug 3's
# scrypt default: scrypt wants ~32MB per verify and isn't guaranteed present on
# 32-bit Pi OS builds. check_password_hash reads the algorithm from the stored
# string, so raising this later doesn't invalidate existing hashes.
PW_METHOD = "pbkdf2:sha256:260000"

PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
    b'<rect width="48" height="48" fill="#D9CFC0"/>'
    b'<path d="M14 30l7-8 5 6 4-4 6 6H14z" fill="#C42B4F" opacity=".6"/>'
    b'<circle cx="18" cy="17" r="3" fill="#C42B4F" opacity=".6"/></svg>'
)

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

# ------------------------------------------------------------ session key


def load_secret_key() -> str:
    """Persist the session key across restarts, or everyone gets logged out."""
    env = os.environ.get("SECRET_KEY", "")
    if env:
        return env
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    # O_EXCL so two workers starting at once can't race to write different keys.
    try:
        fd = os.open(SECRET_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
        log.info("generated a new session key at %s", SECRET_KEY_PATH)
    except FileExistsError:
        key = SECRET_KEY_PATH.read_text().strip()
    return key


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Must default off: the normal access path is plain http:// on the LAN, and
    # a Secure cookie there is silently dropped, so login "succeeds" forever.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# -------------------------------------------------------------- database


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _has_column(conn, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    token_sha256  TEXT UNIQUE,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login_at TEXT
)
"""

SCHEMA_PHOTOS_V5 = """
CREATE TABLE {name} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id),
    sha256        TEXT NOT NULL,
    original_name TEXT,
    stored_path   TEXT NOT NULL,
    format        TEXT,
    media_type    TEXT NOT NULL DEFAULT 'photo',
    size_bytes    INTEGER,
    width         INTEGER,
    height        INTEGER,
    duration_s    REAL,
    taken_at      TEXT,
    latitude      REAL,
    longitude     REAL,
    camera        TEXT,
    live_video_id INTEGER REFERENCES photos(id),
    favorite      INTEGER NOT NULL DEFAULT 0,
    device        TEXT,
    uploaded_at   TEXT NOT NULL,
    UNIQUE(owner_id, sha256)
)
"""

SCHEMA_ALBUMS = """
CREATE TABLE IF NOT EXISTS albums (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, name)
)
"""

SCHEMA_ALBUM_PHOTOS = """
CREATE TABLE IF NOT EXISTS album_photos (
    album_id INTEGER NOT NULL REFERENCES albums(id),
    photo_id INTEGER NOT NULL REFERENCES photos(id),
    added_at TEXT NOT NULL,
    UNIQUE(album_id, photo_id)
)
"""


def init_db() -> None:
    """Create a fresh schema at the current version. No-op on existing DBs."""
    with db() as conn:
        conn.execute(SCHEMA_USERS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_token ON users(token_sha256)")
        if not _table_exists(conn, "photos"):
            conn.execute(SCHEMA_PHOTOS_V5.format(name="photos"))
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (5)")
        conn.execute(SCHEMA_ALBUMS)
        conn.execute(SCHEMA_ALBUM_PHOTOS)


def _current_version(conn) -> int:
    if not _has_column(conn, "photos", "owner_id"):
        return 0
    if not _table_exists(conn, "schema_version"):
        return 1
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row["version"] if row else 1


def _backup_db(version: int) -> None:
    dest = DB_PATH.with_name(DB_PATH.name + f".bak-v{version}")
    if dest.exists() or not DB_PATH.exists():
        return
    # WAL means recent writes live in photos.db-wal, not photos.db. Checkpoint
    # first or the backup silently loses them.
    with db() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(DB_PATH, dest)
    log.info("backed up database to %s", dest)


def migrate() -> None:
    """Bring an existing database up to the current schema. Idempotent."""
    if not DB_PATH.exists():
        return
    with db() as conn:
        version = _current_version(conn)

    if version >= 5:
        return

    _backup_db(version)

    if version == 0:
        log.info("migrating database v0 -> v1")
        with db() as conn:
            conn.execute(SCHEMA_USERS)
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
            # owner_id stays nullable at v1 -- existing rows have no owner until
            # the first account is created and claims them.
            for col, decl in (
                ("owner_id", "INTEGER"),
                ("width", "INTEGER"),
                ("height", "INTEGER"),
            ):
                if not _has_column(conn, "photos", col):
                    conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_owner ON photos(owner_id, id DESC)"
            )
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        version = 1

    if version == 1:
        with db() as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE owner_id IS NULL"
            ).fetchone()["c"]
        if orphans:
            # Fully functional at v1, so just wait -- never brick a boot over this.
            log.warning(
                "%d photos have no owner yet; staying on schema v1. "
                "Create the first account and they will be claimed automatically.",
                orphans,
            )
            return
        log.info("migrating database v1 -> v2")
        # v2 swaps the global UNIQUE(sha256) for UNIQUE(owner_id, sha256).
        # Without that, the second user to upload a shared photo gets it
        # silently swallowed as a duplicate. SQLite can't drop a column-level
        # constraint, so this is a table rebuild.
        conn = db()
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            conn.execute(SCHEMA_PHOTOS_V5.format(name="photos_new"))
            conn.execute(
                "INSERT INTO photos_new (id, owner_id, sha256, original_name,"
                " stored_path, format, size_bytes, width, height, device, uploaded_at)"
                " SELECT id, owner_id, sha256, original_name, stored_path, format,"
                " size_bytes, width, height, device, uploaded_at FROM photos"
            )
            conn.execute("DROP TABLE photos")
            conn.execute("ALTER TABLE photos_new RENAME TO photos")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_photos_owner ON photos(owner_id, id DESC)"
            )
            if list(conn.execute("PRAGMA foreign_key_check")):
                raise RuntimeError("foreign key check failed after rebuild")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
        version = 2

    if version == 2:
        log.info("migrating database v2 -> v3")
        # v3 adds video support: media_type ('photo'/'video') and duration_s.
        # Purely additive, so older code still runs against a migrated DB.
        with db() as conn:
            if not _has_column(conn, "photos", "media_type"):
                conn.execute(
                    "ALTER TABLE photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'photo'"
                )
            if not _has_column(conn, "photos", "duration_s"):
                conn.execute("ALTER TABLE photos ADD COLUMN duration_s REAL")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        version = 3

    if version == 3:
        log.info("migrating database v3 -> v4")
        # v4 adds capture details (taken_at, GPS, camera) and Live Photo
        # pairing. Additive again. Existing rows stay NULL until
        # `python app.py backfill-details` re-reads their files.
        with db() as conn:
            for col, decl in (
                ("taken_at", "TEXT"),
                ("latitude", "REAL"),
                ("longitude", "REAL"),
                ("camera", "TEXT"),
                ("live_video_id", "INTEGER REFERENCES photos(id)"),
            ):
                if not _has_column(conn, "photos", col):
                    conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (4)")
        version = 4

    if version == 4:
        log.info("migrating database v4 -> v5")
        # v5 adds favorites and albums. Additive as always.
        with db() as conn:
            if not _has_column(conn, "photos", "favorite"):
                conn.execute(
                    "ALTER TABLE photos ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(SCHEMA_ALBUMS)
            conn.execute(SCHEMA_ALBUM_PHOTOS)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (5)")


def get_photo_row(photo_id: int, owner_id: int):
    """Fetch a photo scoped to its owner. Cross-user access reads as missing."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM photos WHERE id = ? AND owner_id = ?", (photo_id, owner_id)
        ).fetchone()


# ------------------------------------------------------------------ users

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
# Verified against this when the username is unknown, so a bad username and a
# bad password take the same amount of time.
DUMMY_HASH = generate_password_hash("dummy-password", method=PW_METHOD)

_login_fails: dict[tuple, tuple] = {}
MAX_FAILS = 5
LOCKOUT = timedelta(minutes=5)


def token_digest(token: str) -> str:
    # A fast hash is right here: the token is 256 bits of entropy, so it isn't
    # guessable, and a deterministic digest keeps lookup a single indexed query.
    return hashlib.sha256(token.encode()).hexdigest()


def throttled(ident: str) -> bool:
    fails, first = _login_fails.get(ident, (0, None))
    if first and datetime.now(timezone.utc) - first > LOCKOUT:
        _login_fails.pop(ident, None)
        return False
    return fails >= MAX_FAILS


def record_fail(ident: str) -> None:
    fails, first = _login_fails.get(ident, (0, None))
    _login_fails[ident] = (fails + 1, first or datetime.now(timezone.utc))


def create_user(username: str, password: str, token: str | None = None) -> dict:
    """Create an account. The first one becomes admin and claims orphan photos."""
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 2-32 characters: letters, digits, . _ -")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")

    token = token or secrets.token_urlsafe(32)
    conn = db()
    try:
        conn.execute("BEGIN")
        is_first = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, token_sha256, is_admin,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (
                username,
                generate_password_hash(password, method=PW_METHOD),
                token_digest(token),
                1 if is_first else 0,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        uid = cur.lastrowid
        claimed = 0
        if is_first and _has_column(conn, "photos", "owner_id"):
            claimed = conn.execute(
                "UPDATE photos SET owner_id = ? WHERE owner_id IS NULL", (uid,)
            ).rowcount
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("That username is already taken.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if claimed:
        log.info("claimed %d unowned photos for first user %r", claimed, username)
        migrate()  # no orphans left, so the v1 -> v2 rebuild can proceed now
    log.info("created user %r (admin=%s)", username, is_first)
    return {"id": uid, "username": username, "token": token, "is_admin": is_first}


def rotate_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "UPDATE users SET token_sha256 = ? WHERE id = ?",
            (token_digest(token), user_id),
        )
    return token


# ------------------------------------------------------------------ auth


class Identity:
    __slots__ = ("id", "username", "is_admin", "via")

    def __init__(self, row, via: str):
        self.id = row["id"]
        self.username = row["username"]
        self.is_admin = bool(row["is_admin"])
        self.via = via


def presented_token() -> str:
    return (
        request.headers.get("X-API-Key")
        or request.args.get("key")
        or request.form.get("key")
        or ""
    )


def load_identity():
    """Resolve the caller from a session cookie or an upload token."""
    if "identity" in g:
        return g.identity

    ident = None
    uid = session.get("uid")
    if uid:
        with db() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            ident = Identity(row, via="session")
        else:
            session.clear()

    if ident is None:
        tok = presented_token()
        if tok:
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM users WHERE token_sha256 = ?", (token_digest(tok),)
                ).fetchone()
            if row:
                ident = Identity(row, via="token")
            elif ALLOW_LEGACY_KEY and API_KEY and hmac.compare_digest(tok, API_KEY):
                # Keeps the existing iPhone Shortcut working across the upgrade.
                with db() as conn:
                    row = conn.execute(
                        "SELECT * FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
                    ).fetchone()
                if row:
                    ident = Identity(row, via="legacy")
                    log.warning(
                        "legacy shared API_KEY used on %s from %s",
                        request.path, request.remote_addr,
                    )

    g.identity = ident
    return ident


def login_required(fn):
    """For pages a person visits: bounce to the login form, never JSON."""

    @wraps(fn)
    def wrapper(*a, **kw):
        if load_identity() is None:
            return redirect(url_for("login", next=request.full_path))
        return fn(*a, **kw)

    return wrapper


def auth_required(fn):
    """For assets and the upload API: session cookie or token, 401 on failure.

    Images use this rather than login_required because an <img> that follows a
    302 to the login page renders as a mystery broken image.
    """

    @wraps(fn)
    def wrapper(*a, **kw):
        if load_identity() is None:
            abort(401, description="Sign in, or send a valid upload token.")
        return fn(*a, **kw)

    return wrapper


# Computed once at startup: browsers cache app.css/app.js hard, so a deploy
# used to leave phones running week-old script until they felt like refreshing.
# The mtime in ?v= makes every restart-after-pull an instant cache miss.
STATIC_VER = max(
    int((BASE_DIR / "static" / name).stat().st_mtime)
    for name in ("app.css", "app.js")
)


@app.context_processor
def inject_user():
    ident = load_identity()
    # mtime doubles as a cache-buster so a fresh avatar shows immediately.
    ver = None
    if ident is not None:
        p = AVATARS_DIR / f"{ident.id}.jpg"
        if p.exists():
            ver = int(p.stat().st_mtime)
    return {"current_user": ident, "avatar_ver": ver, "static_ver": STATIC_VER}


# --------------------------------------------------------------- helpers


def free_mb(path: Path) -> int:
    """Free space in MB, or -1 if the path can't be reached.

    Returning -1 rather than raising keeps an unmounted external drive from
    500ing the whole gallery -- you can still sign in and see that something is
    wrong. It also reads as "below MIN_FREE_MB" to upload(), which correctly
    refuses to write. Deliberately does NOT mkdir the path: silently recreating
    a mount point would start filling the SD card behind the missing drive.
    """
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        log.warning("cannot read free space at %s -- is the drive mounted?", path)
        return -1


def identify_image(path: Path):
    """Return (format, width, height) if path is a readable image, else None.

    Reads from disk rather than bytes so a 2GB upload never has to sit in RAM
    just to find out it isn't an image.
    """
    try:
        with Image.open(path) as im:
            im.verify()  # integrity check; invalidates the object
        with Image.open(path) as im:
            return im.format, im.width, im.height
    except Exception:
        return None


def identify_video(head: bytes) -> str | None:
    """Return "MOV"/"MP4" if the first bytes look like a video container.

    ISO-BMFF: bytes 4-8 are "ftyp", 8-12 the brand. HEIC shares the layout but
    its brands aren't in VIDEO_BRANDS, so photos can't be misfiled as videos.
    """
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return VIDEO_BRANDS.get(head[8:12])
    return None


# "+37.3349-122.0090+018.337/" -- the ISO 6709 string Apple stores in videos.
ISO6709_RE = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def probe_video(path: Path) -> dict:
    """Width/height/duration plus capture details via one ffprobe call.

    Returns a dict with keys width, height, duration_s, taken_at, latitude,
    longitude, camera -- any of which may be None. Metadata here is nice to
    have, never load-bearing.
    """
    out = {"width": None, "height": None, "duration_s": None,
           "taken_at": None, "latitude": None, "longitude": None, "camera": None}
    if not FFPROBE:
        return out
    try:
        raw = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration:format_tags",
             "-of", "json", str(path)],
            capture_output=True, timeout=30, check=True,
        ).stdout
        info = json.loads(raw)
        stream = (info.get("streams") or [{}])[0]
        fmt = info.get("format") or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

        out["width"] = stream.get("width")
        out["height"] = stream.get("height")
        if fmt.get("duration"):
            out["duration_s"] = float(fmt["duration"])
        # Apple's creationdate carries the local timezone; creation_time is UTC.
        out["taken_at"] = (tags.get("com.apple.quicktime.creationdate")
                           or tags.get("creation_time") or None)
        loc = (tags.get("com.apple.quicktime.location.iso6709")
               or tags.get("location") or "")
        m = ISO6709_RE.match(loc)
        if m:
            out["latitude"], out["longitude"] = float(m.group(1)), float(m.group(2))
        out["camera"] = tags.get("com.apple.quicktime.model") or None
    except Exception:
        log.warning("ffprobe failed for %s", path)
    return out


def _gps_to_float(vals, ref) -> float | None:
    """EXIF GPS (deg, min, sec) rationals + N/S/E/W ref -> signed decimal."""
    try:
        deg, minutes, seconds = (float(v) for v in vals)
        out = deg + minutes / 60 + seconds / 3600
        if str(ref).upper() in ("S", "W"):
            out = -out
        return round(out, 6)
    except Exception:
        return None


def extract_image_details(path: Path) -> dict:
    """taken_at / latitude / longitude / camera from EXIF, all optional.

    Screenshots and messaging-app downloads legitimately have no EXIF at all,
    so every field degrades to None rather than failing the upload.
    """
    out = {"taken_at": None, "latitude": None, "longitude": None, "camera": None}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
    except Exception:
        return out
    if not exif:
        return out
    try:
        make = str(exif.get(271) or "").strip()   # Make
        model = str(exif.get(272) or "").strip()  # Model
        if model:
            # "Apple iPhone 14" reads better than "iPhone 14 iPhone 14".
            out["camera"] = model if model.startswith(make) or not make else f"{make} {model}"

        sub = exif.get_ifd(0x8769)  # Exif sub-IFD
        dt = sub.get(36867) or sub.get(36868) or exif.get(306)  # DateTimeOriginal
        if dt:
            # EXIF "2026:07:23 12:30:00" -> ISO "2026-07-23T12:30:00"
            dt = str(dt).strip()
            if len(dt) >= 19 and dt[4] == ":" and dt[7] == ":":
                dt = dt[:4] + "-" + dt[5:7] + "-" + dt[8:10] + "T" + dt[11:19]
            out["taken_at"] = dt

        gps = exif.get_ifd(0x8825)  # GPS IFD
        if gps:
            out["latitude"] = _gps_to_float(gps.get(2), gps.get(1))
            out["longitude"] = _gps_to_float(gps.get(4), gps.get(3))
    except Exception:
        log.warning("EXIF parse failed for %s", path)
    return out


def safe_stem(name: str) -> str:
    stem = Path(name or "").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    return stem[:40]


def resolve_stored(row) -> Path:
    """Map a DB row to its file, refusing anything outside PHOTOS_DIR."""
    src = (PHOTOS_DIR / row["stored_path"]).resolve()
    # is_relative_to, not a string prefix: "/mnt/photos" prefix-matches
    # "/mnt/photos-old", which matters once sibling drives get related names.
    if not src.is_relative_to(PHOTOS_DIR.resolve()):
        abort(404)
    if not src.exists():
        abort(404)
    return src


def render_cached(src: Path, dest: Path, max_edge: int, quality: int) -> Path:
    """Decode, EXIF-rotate, downscale and write a JPEG to dest atomically.

    The atomic replace is load-bearing: browsers request the same thumbnail
    concurrently, and a reader that catches a half-written file gets a
    truncated JPEG, which renders as a broken image.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge))
            im.save(tmp, "JPEG", quality=quality, optimize=True)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


# A gallery page of freshly-uploaded videos must not fork one ffmpeg per tile:
# waitress serves thumbnails from multiple threads, and a Pi 4 has four cores.
FFMPEG_SEM = threading.Semaphore(2)


def render_video_poster(src: Path, dest: Path) -> Path:
    """Extract a poster frame with ffmpeg, atomically like render_cached."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg is not installed; no poster frames")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ffmpeg picks the output format from the extension, so the temp name
    # must end in .jpg -- unlike render_cached's bare .tmp.
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp.jpg")
    try:
        with FFMPEG_SEM:
            base = [FFMPEG, "-y", "-v", "error"]
            args = ["-frames:v", "1", "-vf", f"scale='min({THUMB_EDGE},iw)':-2",
                    "-q:v", "4", str(tmp)]
            subprocess.run(base + ["-ss", "0.5", "-i", str(src)] + args,
                           capture_output=True, timeout=60, check=True)
            if not tmp.exists():
                # Clips shorter than the seek point produce no frame; retry
                # from the very start rather than serving a placeholder.
                subprocess.run(base + ["-i", str(src)] + args,
                               capture_output=True, timeout=60, check=True)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return dest


def cached_image(path: Path, mimetype: str = "image/jpeg"):
    resp = send_file(path, mimetype=mimetype, conditional=True)
    resp.cache_control.private = True  # per-user; never let a proxy share these
    resp.cache_control.max_age = 86400
    return resp


# A Live Photo's clip runs about 3s; anything longer is just a video.
LIVE_VIDEO_MAX_S = 6.5


def pair_live_photo(conn, owner_id: int, new_id: int, media_type: str,
                    original_name: str, duration_s) -> None:
    """Link a still and its Live Photo clip when both halves have arrived.

    iOS exports the pair as IMG_1234.HEIC + IMG_1234.MOV, so match on the
    filename stem within the owner's library, in either upload order. Runs in
    the caller's transaction alongside the INSERT.
    """
    stem = Path(original_name or "").stem
    if not stem:
        return
    like = (stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            .lower() + ".%")
    if media_type == "photo":
        row = conn.execute(
            "SELECT id FROM photos WHERE owner_id = ? AND media_type = 'video'"
            " AND duration_s IS NOT NULL AND duration_s <= ?"
            " AND LOWER(original_name) LIKE ? ESCAPE '\\'"
            " AND id NOT IN (SELECT live_video_id FROM photos"
            "                WHERE live_video_id IS NOT NULL)"
            " ORDER BY id DESC LIMIT 1",
            (owner_id, LIVE_VIDEO_MAX_S, like),
        ).fetchone()
        if row:
            conn.execute("UPDATE photos SET live_video_id = ? WHERE id = ?",
                         (row["id"], new_id))
            log.info("paired live photo %d with clip %d", new_id, row["id"])
    elif duration_s is not None and duration_s <= LIVE_VIDEO_MAX_S:
        row = conn.execute(
            "SELECT id FROM photos WHERE owner_id = ? AND media_type = 'photo'"
            " AND live_video_id IS NULL AND LOWER(original_name) LIKE ? ESCAPE '\\'"
            " ORDER BY id DESC LIMIT 1",
            (owner_id, like),
        ).fetchone()
        if row:
            conn.execute("UPDATE photos SET live_video_id = ? WHERE id = ?",
                         (new_id, row["id"]))
            log.info("paired live photo %d with clip %d", row["id"], new_id)


def store_photo(file_storage, owner_id: int, device: str) -> dict:
    original = file_storage.filename or ""

    now = datetime.now()
    # Per-user subdirectory, so a filesystem-level slip can't cross accounts.
    subdir = PHOTOS_DIR / f"u{owner_id}" / now.strftime("%Y/%m/%d")
    subdir.mkdir(parents=True, exist_ok=True)

    # Stream to a temp file in 1MB chunks, hashing as we go: a 2GB video must
    # never sit in RAM on a 4GB Pi. Written into the destination directory so
    # the final os.replace stays a same-filesystem atomic rename. The sha (and
    # so the dedup check) is only known once the file is fully on disk; a
    # duplicate costs one wasted write, which is fine.
    tmp = subdir / f".upload.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    sha256 = hashlib.sha256()
    head = b""
    size = 0
    try:
        with open(tmp, "wb") as out:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                sha256.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if not size:
            return {"original_name": original, "status": "rejected", "reason": "empty file"}

        sha = sha256.hexdigest()
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM photos WHERE owner_id = ? AND sha256 = ?", (owner_id, sha)
            ).fetchone()
        if row:
            return {"original_name": original, "status": "duplicate", "id": row["id"]}

        width = height = duration_s = None
        details = {"taken_at": None, "latitude": None, "longitude": None, "camera": None}
        ident = identify_image(tmp)
        if ident is not None:
            fmt, width, height = ident
            if fmt not in ALLOWED_FORMATS:
                return {
                    "original_name": original,
                    "status": "rejected",
                    "reason": f"not a supported image (detected: {fmt})",
                }
            media_type = "photo"
            ext = ALLOWED_FORMATS[fmt]
            details = extract_image_details(tmp)
        else:
            if len(head) >= 12 and head[4:12] in HEIC_MAGIC:
                return {
                    "original_name": original,
                    "status": "rejected",
                    "reason": "HEIC photo received but pillow-heif is not installed on the server",
                }
            fmt = identify_video(head)
            if fmt is None:
                return {
                    "original_name": original,
                    "status": "rejected",
                    "reason": "not a supported image or video (unreadable)",
                }
            media_type = "video"
            ext = VIDEO_EXT[fmt]
            probed = probe_video(tmp)
            width, height = probed["width"], probed["height"]
            duration_s = probed["duration_s"]
            details = {k: probed[k] for k in ("taken_at", "latitude", "longitude", "camera")}

        stem = safe_stem(original)
        name = (
            now.strftime("%Y%m%d-%H%M%S")
            + "_"
            + sha[:8]
            + ("_" + stem if stem else "")
            + ext
        )
        dest = subdir / name

        # Insert first and only then move into place, so losing an insert race
        # can't delete the winner's file.
        try:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO photos (owner_id, sha256, original_name, stored_path,"
                    " format, media_type, size_bytes, width, height, duration_s,"
                    " taken_at, latitude, longitude, camera, device, uploaded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        owner_id,
                        sha,
                        original,
                        str(dest.relative_to(PHOTOS_DIR)),
                        fmt,
                        media_type,
                        size,
                        width,
                        height,
                        duration_s,
                        details["taken_at"],
                        details["latitude"],
                        details["longitude"],
                        details["camera"],
                        device,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                photo_id = cur.lastrowid
                pair_live_photo(conn, owner_id, photo_id, media_type, original, duration_s)
            os.replace(tmp, dest)
        except sqlite3.IntegrityError:  # same file uploaded concurrently
            return {"original_name": original, "status": "duplicate"}
    finally:
        tmp.unlink(missing_ok=True)

    log.info(
        "stored %s (%d bytes, %s %s) owner=%d device=%r -> %s",
        original or "<unnamed>", size, media_type, fmt, owner_id, device, dest,
    )
    return {
        "original_name": original,
        "status": "stored",
        "id": photo_id,
        "path": str(dest.relative_to(PHOTOS_DIR)),
    }


# --------------------------------------------------------- account routes


@app.route("/login", methods=["GET", "POST"])
def login():
    if load_identity() is not None:
        return redirect(url_for("gallery"))
    if request.method == "GET":
        return render_template("login.html", signup_open=bool(INVITE_CODE))

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ident = (username.lower(), request.remote_addr)
    if throttled(ident):
        flash("Too many attempts. Wait a few minutes and try again.", "error")
        return render_template("login.html", signup_open=bool(INVITE_CODE)), 429

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    # Always hash something, so timing doesn't reveal whether the account exists.
    ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
    if not row or not ok:
        record_fail(ident)
        log.warning("failed login for %r from %s", username, request.remote_addr)
        flash("Wrong username or password.", "error")
        return render_template("login.html", signup_open=bool(INVITE_CODE)), 401

    _login_fails.pop(ident, None)
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    with db() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]),
        )
    nxt = request.args.get("next") or ""
    # Only ever redirect within this site.
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("gallery")
    return redirect(nxt)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    # Fail closed: a missing config line must never open public registration.
    if not INVITE_CODE:
        abort(503, description="Signup is disabled. Ask the admin for an account.")
    if request.method == "GET":
        return render_template("signup.html")

    ident = ("signup", request.remote_addr)
    if throttled(ident):
        flash("Too many attempts. Wait a few minutes and try again.", "error")
        return render_template("signup.html"), 429

    if not hmac.compare_digest(request.form.get("invite") or "", INVITE_CODE):
        record_fail(ident)
        flash("That invite code isn't right.", "error")
        return render_template("signup.html"), 403

    password = request.form.get("password") or ""
    if password != (request.form.get("confirm") or ""):
        flash("The two passwords don't match.", "error")
        return render_template("signup.html"), 400

    try:
        user = create_user(request.form.get("username") or "", password)
    except ValueError as e:
        flash(str(e), "error")
        return render_template("signup.html"), 400

    _login_fails.pop(ident, None)
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    # Shown once, right after creation.
    session["fresh_token"] = user["token"]
    return redirect(url_for("account"))


@app.get("/account")
@login_required
def account():
    me = g.identity
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (me.id,)).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE owner_id = ?", (me.id,)
        ).fetchone()["c"]
        bytes_used = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS b FROM photos WHERE owner_id = ?",
            (me.id,),
        ).fetchone()["b"]
        # The storage meter: who is using the drive, and what your own share
        # is made of. Everyone can see the per-person split -- family server.
        by_user = conn.execute(
            "SELECT u.id AS uid, u.username AS name,"
            " COALESCE(SUM(p.size_bytes), 0) AS bytes"
            " FROM users u LEFT JOIN photos p ON p.owner_id = u.id"
            " GROUP BY u.id HAVING bytes > 0 ORDER BY bytes DESC"
        ).fetchall()
        by_type = conn.execute(
            "SELECT media_type, COALESCE(SUM(size_bytes), 0) AS bytes"
            " FROM photos WHERE owner_id = ? GROUP BY media_type",
            (me.id,),
        ).fetchall()

    try:
        du = shutil.disk_usage(PHOTOS_DIR)
        disk_total, disk_free = du.total, du.free
    except OSError:
        disk_total = disk_free = 0

    def gb(n):  # one decimal reads like a phone's storage screen
        return round(n / (1024 ** 3), 1)

    library_total = sum(r["bytes"] for r in by_user)
    other = max(0, (disk_total - disk_free) - library_total)
    def avatar_ver_of(uid):
        p = AVATARS_DIR / f"{uid}.jpg"
        return int(p.stat().st_mtime) if p.exists() else None

    disk_segs = [
        {"label": r["name"], "bytes": r["bytes"], "gb": gb(r["bytes"]),
         "pct": (r["bytes"] / disk_total * 100) if disk_total else 0,
         "uid": r["uid"], "avatar_ver": avatar_ver_of(r["uid"])}
        for r in by_user
    ]
    disk_segs.append({"label": "System & other", "bytes": other, "gb": gb(other),
                      "pct": (other / disk_total * 100) if disk_total else 0,
                      "muted": True})
    type_label = {"photo": "Photos", "video": "Videos"}
    my_segs = [
        {"label": type_label.get(r["media_type"], r["media_type"]),
         "bytes": r["bytes"], "gb": gb(r["bytes"]),
         "pct": (r["bytes"] / bytes_used * 100) if bytes_used else 0}
        for r in sorted(by_type, key=lambda r: -r["bytes"])
    ]

    return render_template(
        "account.html",
        user=row,
        photo_count=count,
        mb_used=round(bytes_used / (1024 * 1024), 1),
        free=free_mb(PHOTOS_DIR),
        heic=HEIC_SUPPORT,
        video=VIDEO_SUPPORT,
        fresh_token=session.pop("fresh_token", None),
        legacy_key_on=ALLOW_LEGACY_KEY and bool(API_KEY),
        disk_segs=disk_segs,
        disk_total_gb=gb(disk_total),
        disk_free_gb=gb(disk_free),
        my_segs=my_segs,
        my_total_gb=gb(bytes_used),
    )


@app.get("/avatar/<int:user_id>")
@login_required
def avatar(user_id: int):
    """Profile pictures. Any signed-in account may see any avatar -- they
    appear in shared chrome, and this is a family server."""
    path = AVATARS_DIR / f"{user_id}.jpg"
    if not path.exists():
        abort(404)
    return cached_image(path)


@app.post("/account/avatar")
@login_required
def account_avatar():
    f = request.files.get("avatar")
    if f is None or not f.filename:
        flash("Choose an image first.", "error")
        return redirect(url_for("account"))
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    dest = AVATARS_DIR / f"{g.identity.id}.jpg"
    raw = AVATARS_DIR / f".{g.identity.id}.raw.{os.getpid()}.tmp"
    out = AVATARS_DIR / f".{g.identity.id}.out.{os.getpid()}.tmp.jpg"
    try:
        f.save(raw)
        if identify_image(raw) is None:
            flash("That doesn't look like an image the server can read.", "error")
            return redirect(url_for("account"))
        with Image.open(raw) as im:
            im = ImageOps.exif_transpose(im)
            # Centre-crop to a square so circles never squash faces.
            im = ImageOps.fit(im.convert("RGB"), (256, 256))
            im.save(out, "JPEG", quality=85, optimize=True)
        os.replace(out, dest)
    except Exception:
        log.exception("avatar upload failed for user %d", g.identity.id)
        flash("Couldn't process that image; try a different one.", "error")
        return redirect(url_for("account"))
    finally:
        raw.unlink(missing_ok=True)
        Path(out).unlink(missing_ok=True)
    flash("Profile picture updated.", "ok")
    return redirect(url_for("account"))


@app.post("/account/rotate-token")
@login_required
def account_rotate():
    session["fresh_token"] = rotate_token(g.identity.id)
    flash("New upload token issued. The old one stopped working immediately.", "ok")
    return redirect(url_for("account"))


# ----------------------------------------------------------- photo routes


@app.post("/upload")
@auth_required
def upload():
    if free_mb(PHOTOS_DIR) < MIN_FREE_MB:
        # abort(507) isn't in werkzeug's exception map, so return directly.
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
    results = [store_photo(f, g.identity.id, device) for f in files]
    stored = sum(1 for r in results if r["status"] == "stored")
    dupes = sum(1 for r in results if r["status"] == "duplicate")
    rejected = len(results) - stored - dupes

    if request.form.get("redirect") == "1":
        flash(f"{stored} uploaded, {dupes} already here, {rejected} rejected.", "ok")
        return redirect(url_for("gallery"))
    return (
        jsonify({"stored": stored, "duplicates": dupes, "rejected": rejected,
                 "owner": g.identity.username, "results": results}),
        200 if (stored or dupes) else 400,
    )


@app.get("/upload")
@login_required
def upload_form():
    return render_template("upload.html")


@app.get("/")
@login_required
def gallery():
    me = g.identity
    page = max(1, request.args.get("page", type=int) or 1)
    offset = (page - 1) * PAGE_SIZE

    # Which shelf of the library: everything, favorites, or one album.
    view = "favorites" if request.args.get("view") == "favorites" else "all"
    album = None
    album_id = request.args.get("album", type=int)

    # A Live Photo's clip is part of its still, not a separate item -- keep it
    # out of the grid and the count.
    where = (
        "p.owner_id = ? AND p.id NOT IN (SELECT live_video_id FROM photos"
        " WHERE owner_id = ? AND live_video_id IS NOT NULL)"
    )
    args = [me.id, me.id]
    joins = ""
    if album_id is not None:
        with db() as conn:
            album = conn.execute(
                "SELECT * FROM albums WHERE id = ? AND owner_id = ?",
                (album_id, me.id),
            ).fetchone()
        if album is None:
            abort(404)
        view = "album"
        joins = " JOIN album_photos ap ON ap.photo_id = p.id AND ap.album_id = ?"
        args.insert(0, album_id)
    elif view == "favorites":
        where += " AND p.favorite = 1"

    with db() as conn:
        albums = conn.execute(
            "SELECT a.*, COUNT(ap.photo_id) AS n FROM albums a"
            " LEFT JOIN album_photos ap ON ap.album_id = a.id"
            " WHERE a.owner_id = ? GROUP BY a.id ORDER BY a.name COLLATE NOCASE",
            (me.id,),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM photos p{joins} WHERE {where}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT p.id, p.original_name, p.uploaded_at, p.device, p.width,"
            f" p.height, p.format, p.media_type, p.duration_s, p.taken_at,"
            f" p.latitude, p.longitude, p.camera, p.live_video_id, p.favorite,"
            f" p.size_bytes FROM photos p{joins} WHERE {where}"
            f" ORDER BY p.id DESC LIMIT ? OFFSET ?",
            args + [PAGE_SIZE, offset],
        ).fetchall()
    return render_template(
        "gallery.html",
        photos=[dict(r) for r in rows],
        total=total,
        page=page,
        has_next=offset + len(rows) < total,
        free=free_mb(PHOTOS_DIR),
        view=view,
        album=album,
        albums=[dict(a) for a in albums],
    )


@app.get("/thumb/<int:photo_id>")
@auth_required
def thumb(photo_id: int):
    row = get_photo_row(photo_id, g.identity.id)
    if row is None:
        abort(404)
    tpath = THUMBS_DIR / f"{photo_id}.jpg"
    if not tpath.exists():
        try:
            src = resolve_stored(row)
            if row["media_type"] == "video":
                render_video_poster(src, tpath)
            else:
                render_cached(src, tpath, THUMB_EDGE, 80)
        except HTTPException:
            raise
        except Exception:
            # Serve a placeholder rather than a 500: the grid stays intact, and
            # the header plus the traceback below make it debuggable.
            log.exception(
                "thumbnail generation failed for photo %d (%s)", photo_id, row["format"]
            )
            resp = send_file(io.BytesIO(PLACEHOLDER_SVG), mimetype="image/svg+xml")
            resp.headers["X-Thumb-Error"] = "generation-failed"
            resp.cache_control.no_store = True  # retry on the next load
            return resp
    return cached_image(tpath)


@app.get("/photo/<int:photo_id>")
@auth_required
def photo(photo_id: int):
    """A copy the browser can actually display. HEIC/TIFF/BMP get transcoded."""
    row = get_photo_row(photo_id, g.identity.id)
    if row is None:
        abort(404)
    if row["media_type"] == "video":
        # Stale links and lightbox preloads degrade to the stream instead of
        # crashing Pillow on a video file.
        return redirect(url_for("media", photo_id=photo_id))
    src = resolve_stored(row)
    if row["format"] in BROWSER_SAFE:
        mt = mimetypes.guess_type(str(src))[0] or "image/jpeg"
        return cached_image(src, mimetype=mt)
    dpath = DISPLAY_DIR / f"{photo_id}.jpg"
    if not dpath.exists():
        render_cached(src, dpath, DISPLAY_EDGE, 85)
    return cached_image(dpath)


@app.get("/media/<int:photo_id>")
@auth_required
def media(photo_id: int):
    """Stream a video inline. conditional=True turns on HTTP Range (206),
    which iOS Safari requires before it will play or scrub anything."""
    row = get_photo_row(photo_id, g.identity.id)
    if row is None:
        abort(404)
    src = resolve_stored(row)
    mt = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
    resp = send_file(src, mimetype=mt, conditional=True)
    resp.cache_control.private = True
    resp.cache_control.max_age = 86400
    return resp


@app.get("/original/<int:photo_id>")
@auth_required
def original(photo_id: int):
    """The untouched bytes.

    Attachment by default (desktop "save as"). With ?inline=1 it is served
    inline instead, which is what makes iOS long-press -> "Add to Photos" save
    the true original rather than the downscaled display copy. iOS reads HEIC
    natively, so the file round-trips back to the camera roll intact.
    """
    row = get_photo_row(photo_id, g.identity.id)
    if row is None:
        abort(404)
    src = resolve_stored(row)
    mt = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
    inline = request.args.get("inline") == "1"
    resp = send_file(
        src,
        mimetype=mt,
        as_attachment=not inline,
        download_name=row["original_name"] or src.name,
        conditional=True,
    )
    resp.cache_control.private = True
    resp.cache_control.max_age = 86400
    return resp


# ------------------------------------------------------ favorites & albums


def _ids_from_form() -> list[int]:
    try:
        ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip()]
    except ValueError:
        abort(400, description="ids must be a comma-separated list of numbers.")
    if not ids or len(ids) > MAX_EXPORT_FILES:
        abort(400, description="Send between 1 and 500 ids.")
    return ids


@app.post("/photo/<int:photo_id>/favorite")
@auth_required
def favorite_toggle(photo_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT favorite FROM photos WHERE id = ? AND owner_id = ?",
            (photo_id, g.identity.id),
        ).fetchone()
        if row is None:
            abort(404)
        new = 0 if row["favorite"] else 1
        conn.execute("UPDATE photos SET favorite = ? WHERE id = ?", (new, photo_id))
    return jsonify({"id": photo_id, "favorite": bool(new)})


@app.post("/photos/favorite")
@auth_required
def favorite_bulk():
    ids = _ids_from_form()
    on = 1 if request.form.get("on") == "1" else 0
    with db() as conn:
        cur = conn.execute(
            f"UPDATE photos SET favorite = ? WHERE owner_id = ?"
            f" AND id IN ({','.join('?' * len(ids))})",
            [on, g.identity.id] + ids,
        )
    return jsonify({"updated": cur.rowcount, "favorite": bool(on)})


@app.post("/albums/add")
@auth_required
def album_add():
    """Add photos to an album, creating it first if a new name was given."""
    ids = _ids_from_form()
    name = (request.form.get("new_name") or "").strip()
    album_id = request.form.get("album_id", type=int)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as conn:
        if album_id is None:
            if not name or len(name) > 40:
                abort(400, description="Album names run 1-40 characters.")
            row = conn.execute(
                "SELECT id FROM albums WHERE owner_id = ? AND name = ?",
                (g.identity.id, name),
            ).fetchone()
            if row:
                album_id = row["id"]
            else:
                album_id = conn.execute(
                    "INSERT INTO albums (owner_id, name, created_at) VALUES (?, ?, ?)",
                    (g.identity.id, name, now),
                ).lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM albums WHERE id = ? AND owner_id = ?",
                (album_id, g.identity.id),
            ).fetchone()
            if row is None:
                abort(404)
        # Only the caller's own photos can enter their album.
        owned = [r["id"] for r in conn.execute(
            f"SELECT id FROM photos WHERE owner_id = ?"
            f" AND id IN ({','.join('?' * len(ids))})",
            [g.identity.id] + ids,
        )]
        added = 0
        for pid in owned:
            cur = conn.execute(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_id, added_at)"
                " VALUES (?, ?, ?)",
                (album_id, pid, now),
            )
            added += cur.rowcount
    return jsonify({"album_id": album_id, "added": added})


@app.post("/albums/<int:album_id>/remove")
@auth_required
def album_remove(album_id: int):
    ids = _ids_from_form()
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM albums WHERE id = ? AND owner_id = ?",
            (album_id, g.identity.id),
        ).fetchone()
        if row is None:
            abort(404)
        cur = conn.execute(
            f"DELETE FROM album_photos WHERE album_id = ?"
            f" AND photo_id IN ({','.join('?' * len(ids))})",
            [album_id] + ids,
        )
    return jsonify({"removed": cur.rowcount})


@app.post("/albums/<int:album_id>/delete")
@login_required
def album_delete(album_id: int):
    """Delete the album itself. The photos in it are untouched."""
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM albums WHERE id = ? AND owner_id = ?",
            (album_id, g.identity.id),
        ).fetchone()
        if row is None:
            abort(404)
        conn.execute("DELETE FROM album_photos WHERE album_id = ?", (album_id,))
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
    flash(f"Album “{row['name']}” deleted. Its photos are still in your library.", "ok")
    return redirect(url_for("gallery"))


def photo_json(row) -> dict:
    """Shape one photo for the API. URLs are absolute so Shortcuts can use them."""
    return {
        "id": row["id"],
        "name": row["original_name"],
        "uploaded_at": row["uploaded_at"],
        "device": row["device"],
        "format": row["format"],
        "media_type": row["media_type"],
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "duration_s": row["duration_s"],
        "taken_at": row["taken_at"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "camera": row["camera"],
        "live_video_id": row["live_video_id"],
        "favorite": bool(row["favorite"]),
        "thumb_url": url_for("thumb", photo_id=row["id"], _external=True),
        "display_url": url_for(
            "media" if row["media_type"] == "video" else "photo",
            photo_id=row["id"], _external=True,
        ),
        "original_url": url_for("original", photo_id=row["id"], _external=True),
    }


@app.get("/api/photos")
@auth_required
def api_photos():
    """List this account's photos as JSON, for the iOS "Get from Pi" Shortcut.

    Default order is newest-first. Pass ?after_id=N to pull only what arrived
    since last time, oldest-first -- that makes repeat runs incremental instead
    of re-downloading the whole library.
    """
    limit = request.args.get("limit", type=int) or API_DEFAULT_LIMIT
    limit = max(1, min(limit, API_MAX_LIMIT))
    after_id = request.args.get("after_id", type=int)

    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE owner_id = ?", (g.identity.id,)
        ).fetchone()["c"]
        if after_id is not None:
            rows = conn.execute(
                "SELECT * FROM photos WHERE owner_id = ? AND id > ?"
                " ORDER BY id ASC LIMIT ?",
                (g.identity.id, after_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM photos WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
                (g.identity.id, limit),
            ).fetchall()

    photos = [photo_json(r) for r in rows]
    return jsonify({
        "owner": g.identity.username,
        "total": total,
        "count": len(photos),
        # Feed this back as ?after_id= on the next run to fetch only what's new.
        "max_id": max((p["id"] for p in photos), default=after_id or 0),
        "photos": photos,
    })


def _requested_export_rows(owner_id: int):
    """Resolve ?ids=/?all=1 to owner-scoped rows, or abort with a reason."""
    with db() as conn:
        if request.args.get("all") == "1":
            rows = conn.execute(
                "SELECT * FROM photos WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
                (owner_id, MAX_EXPORT_FILES + 1),
            ).fetchall()
        else:
            raw = (request.args.get("ids") or "").split(",")
            ids = [int(x) for x in raw if x.strip().isdigit()]
            if not ids:
                abort(400, description="Pass ?ids=1,2,3 or ?all=1.")
            if len(ids) > MAX_EXPORT_FILES:
                abort(400, description=f"At most {MAX_EXPORT_FILES} photos per export.")
            marks = ",".join("?" * len(ids))
            # owner_id in the WHERE clause is what stops one account exporting
            # another's photos by guessing ids.
            rows = conn.execute(
                f"SELECT * FROM photos WHERE owner_id = ? AND id IN ({marks})"
                " ORDER BY id DESC",
                (owner_id, *ids),
            ).fetchall()
    if not rows:
        abort(404, description="No matching photos.")
    if len(rows) > MAX_EXPORT_FILES:
        abort(400, description=f"At most {MAX_EXPORT_FILES} photos per export.")
    return rows


@app.get("/export.zip")
@auth_required
def export_zip():
    """Bundle originals into a zip. Owner-scoped and capped; every call logged."""
    rows = _requested_export_rows(g.identity.id)

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    used = set()
    written = 0
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for row in rows:
                src = PHOTOS_DIR / row["stored_path"]
                if not src.exists():
                    log.warning("export skipped missing file for photo %d", row["id"])
                    continue
                name = row["original_name"] or src.name
                # Originals are already compressed, so ZIP_STORED keeps the Pi's
                # CPU out of it. Disambiguate repeats by id rather than clobber.
                if name in used:
                    name = f"{Path(name).stem}_{row['id']}{Path(name).suffix}"
                used.add(name)
                zf.write(src, arcname=name)
                written += 1
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    if not written:
        Path(tmp.name).unlink(missing_ok=True)
        abort(404, description="None of those photos are still on disk.")

    log.info("export: %d photos for %s from %s",
             written, g.identity.username, request.remote_addr)
    size = os.path.getsize(tmp.name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Stream it out and delete in the generator's finally, rather than handing
    # the path to send_file and unlinking after the request. That ordering is
    # what lets the same code work on Windows, where you can't remove a file
    # another handle still has open -- and it keeps a 500-photo zip off the
    # Pi's heap, which matters a lot more than the portability does.
    def pump():
        try:
            with open(tmp.name, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    resp = Response(stream_with_context(pump()), mimetype="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="pi-storage-{stamp}.zip"'
    resp.headers["Content-Length"] = str(size)  # so the browser shows progress
    resp.cache_control.no_store = True
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    """Makes 'Add to Home Screen' produce a real app icon rather than a bookmark.

    Deliberately unauthenticated: browsers routinely fetch the manifest without
    credentials, and a 401 here makes installation silently fall back to a
    generic web clip. It leaks nothing but the app's name and colours.
    """
    resp = jsonify({
        "name": "Pi-Storage",
        "short_name": "Pi-Storage",
        "description": "Your photos, on your Pi.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#F4EFE6",
        "theme_color": "#F4EFE6",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-512-maskable.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })
    resp.mimetype = "application/manifest+json"
    return resp


@app.get("/health")
def health():
    # Deliberately thin for anonymous callers; the detail lives on /account.
    if load_identity() is not None:
        return jsonify(
            {"status": "ok", "free_mb": free_mb(PHOTOS_DIR),
             "heic_support": HEIC_SUPPORT, "video_support": VIDEO_SUPPORT}
        )
    return jsonify({"status": "ok"})


@app.errorhandler(HTTPException)
def handle_http_error(e: HTTPException):
    if wants_json():
        return jsonify({"error": e.name, "detail": e.description}), e.code
    return render_template("error.html", code=e.code, name=e.name,
                           detail=e.description), e.code


@app.errorhandler(Exception)
def handle_unexpected(e: Exception):
    log.exception("unhandled error on %s", request.path)
    if wants_json():
        return jsonify({"error": "Internal Server Error",
                        "detail": "Something broke. Check the server log."}), 500
    return render_template("error.html", code=500, name="Internal Server Error",
                           detail="Something broke. Check the server log."), 500


def wants_json() -> bool:
    if request.path.startswith(("/upload", "/health")) and request.method == "POST":
        return True
    # /api and /export are called by Shortcuts, which can't read an HTML page.
    if request.path.startswith(("/health", "/api/", "/export")):
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] > accept["text/html"]


# ------------------------------------------------------------------ main


def cmd_create_user(argv) -> None:
    import getpass

    if not argv:
        raise SystemExit("usage: python app.py create-user <username>")
    username = argv[0]
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm: "):
        raise SystemExit("Passwords don't match.")
    init_db()
    migrate()
    try:
        user = create_user(username, password)
    except ValueError as e:
        raise SystemExit(str(e))
    print(f"\nCreated {user['username']}" + (" (admin)" if user["is_admin"] else ""))
    print(f"Upload token: {user['token']}")
    print("Put that in the iPhone Shortcut as the X-API-Key header. Shown once.")


def cmd_rotate_token(argv) -> None:
    if not argv:
        raise SystemExit("usage: python app.py rotate-token <username>")
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (argv[0],)
        ).fetchone()
    if not row:
        raise SystemExit(f"No such user: {argv[0]}")
    print(f"Upload token: {rotate_token(row['id'])}")


def cmd_backfill(argv) -> None:
    """Re-read stored files to fill the capture details added in schema v4.

    Uploads made before v4 have NULL taken_at/GPS/camera; this walks the
    library once and fills what the files can still tell us. Safe to re-run:
    rows that already have details are skipped.
    """
    with db() as conn:
        rows = conn.execute("SELECT * FROM photos ORDER BY id").fetchall()
    updated = missing = 0
    for row in rows:
        src = PHOTOS_DIR / row["stored_path"]
        if not src.exists():
            missing += 1
            continue
        if not (row["taken_at"] is None and row["camera"] is None
                and row["latitude"] is None):
            continue
        duration = row["duration_s"]
        if row["media_type"] == "video":
            p = probe_video(src)
            vals = (p["taken_at"], p["latitude"], p["longitude"], p["camera"])
            duration = duration if duration is not None else p["duration_s"]
        else:
            d = extract_image_details(src)
            vals = (d["taken_at"], d["latitude"], d["longitude"], d["camera"])
        if any(v is not None for v in vals) or duration != row["duration_s"]:
            with db() as conn:
                conn.execute(
                    "UPDATE photos SET taken_at = ?, latitude = ?, longitude = ?,"
                    " camera = ?, duration_s = ? WHERE id = ?",
                    (*vals, duration, row["id"]),
                )
            updated += 1
    # Pair any Live Photos whose halves were both uploaded before v4 existed.
    with db() as conn:
        stills = conn.execute(
            "SELECT id, owner_id, original_name FROM photos"
            " WHERE media_type = 'photo' AND live_video_id IS NULL"
            " AND original_name IS NOT NULL"
        ).fetchall()
        for s in stills:
            pair_live_photo(conn, s["owner_id"], s["id"], "photo",
                            s["original_name"], None)
        pairs = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE live_video_id IS NOT NULL"
        ).fetchone()["c"]
    print(f"details filled: {updated} | files missing: {missing} | live photo pairs: {pairs}")


def check_storage_ready() -> None:
    """Prepare PHOTOS_DIR, or refuse to start if it looks like an absent drive.

    The dangerous case, once photos live on a USB stick or external disk: the
    drive fails to mount, the empty mount point is still there, and an
    unconditional mkdir would happily start writing photos onto the SD card
    underneath it. The gallery would look fine, free space would read as the
    card's, and you'd find out when the card filled -- with the library split
    across two places and nothing logged.

    So: only auto-create the default location, and treat "the database has
    photos but the directory is empty" as proof the drive isn't there.
    """
    configured = "PHOTOS_DIR" in os.environ

    if not PHOTOS_DIR.exists():
        if configured:
            raise SystemExit(
                f"PHOTOS_DIR is set to {PHOTOS_DIR} but that path does not exist.\n"
                "If it's a USB stick or external drive, mount it first "
                "(`lsblk` to find it, `mount -a` if it's in /etc/fstab).\n"
                "Refusing to create it, because that would write photos to the "
                "SD card instead of the drive."
            )
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        return

    if not DB_PATH.exists():
        return
    with db() as conn:
        if not _table_exists(conn, "photos"):
            return
        known = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
    if known and not any(PHOTOS_DIR.iterdir()):
        raise SystemExit(
            f"The database lists {known} photos but {PHOTOS_DIR} is empty.\n"
            "That almost always means the drive holding them is not mounted.\n"
            "Mount it and start again. Refusing to serve, because writing new "
            "photos here would split the library across two locations.\n"
            "If you really did clear the photos on purpose, delete or move "
            f"{DB_PATH} as well."
        )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("create-user", "rotate-token", "backfill-details"):
        check_storage_ready()
        init_db()
        migrate()
        {"create-user": cmd_create_user, "rotate-token": cmd_rotate_token,
         "backfill-details": cmd_backfill}[argv[0]](argv[1:])
        return

    if ALLOW_LEGACY_KEY and not API_KEY:
        log.info("ALLOW_LEGACY_KEY is on but API_KEY is unset; legacy uploads disabled.")
    check_storage_ready()
    init_db()
    migrate()

    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if not users:
        log.warning(
            "No accounts yet. Create one with:  python app.py create-user <name>"
            + ("  (or use the signup page)" if INVITE_CODE else "")
        )

    log.info(
        "serving on %s:%d | photos -> %s | HEIC: %s | video: %s | signup: %s",
        HOST, PORT, PHOTOS_DIR, HEIC_SUPPORT, VIDEO_SUPPORT,
        "invite-only" if INVITE_CODE else "off",
    )
    try:
        from waitress import serve  # production-grade WSGI server

        # waitress has its own body cap (default 1GB) that would 413 large
        # videos before Flask's MAX_CONTENT_LENGTH ever saw them.
        serve(app, host=HOST, port=PORT,
              max_request_body_size=MAX_UPLOAD_MB * 1024 * 1024)
    except ImportError:
        app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
