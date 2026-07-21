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
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request,
    send_file, session, url_for,
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

# ---------------------------------------------------------------- config

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", BASE_DIR / "photos"))
THUMBS_DIR = Path(os.environ.get("THUMBS_DIR", PHOTOS_DIR / ".thumbs"))
DISPLAY_DIR = Path(os.environ.get("DISPLAY_DIR", PHOTOS_DIR / ".display"))
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "photos.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE_DIR / "logs"))
SECRET_KEY_PATH = Path(os.environ.get("SECRET_KEY_PATH", BASE_DIR / "secret.key"))

API_KEY = os.environ.get("API_KEY", "")
ALLOW_LEGACY_KEY = os.environ.get("ALLOW_LEGACY_KEY", "1") == "1"
INVITE_CODE = os.environ.get("INVITE_CODE", "")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))  # per request
MIN_FREE_MB = int(os.environ.get("MIN_FREE_MB", "500"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

PAGE_SIZE = 200
THUMB_EDGE = 480
DISPLAY_EDGE = 2048

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

# Hashing cost is a login-path concern only. PBKDF2 rather than Werkzeug 3's
# scrypt default: scrypt wants ~32MB per verify and isn't guaranteed present on
# 32-bit Pi OS builds. check_password_hash reads the algorithm from the stored
# string, so raising this later doesn't invalidate existing hashes.
PW_METHOD = "pbkdf2:sha256:260000"

PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
    b'<rect width="48" height="48" fill="#D9CFC0"/>'
    b'<path d="M14 30l7-8 5 6 4-4 6 6H14z" fill="#B4654A" opacity=".6"/>'
    b'<circle cx="18" cy="17" r="3" fill="#B4654A" opacity=".6"/></svg>'
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

SCHEMA_PHOTOS_V2 = """
CREATE TABLE {name} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id),
    sha256        TEXT NOT NULL,
    original_name TEXT,
    stored_path   TEXT NOT NULL,
    format        TEXT,
    size_bytes    INTEGER,
    width         INTEGER,
    height        INTEGER,
    device        TEXT,
    uploaded_at   TEXT NOT NULL,
    UNIQUE(owner_id, sha256)
)
"""


def init_db() -> None:
    """Create a fresh schema at the current version. No-op on existing DBs."""
    with db() as conn:
        conn.execute(SCHEMA_USERS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_token ON users(token_sha256)")
        if not _table_exists(conn, "photos"):
            conn.execute(SCHEMA_PHOTOS_V2.format(name="photos"))
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")


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

    if version >= 2:
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
            conn.execute(SCHEMA_PHOTOS_V2.format(name="photos_new"))
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


@app.context_processor
def inject_user():
    return {"current_user": load_identity()}


# --------------------------------------------------------------- helpers


def free_mb(path: Path) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


def identify_image(data: bytes):
    """Return (format, width, height) if data is a readable image, else None."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # integrity check; invalidates the object
        with Image.open(io.BytesIO(data)) as im:
            return im.format, im.width, im.height
    except Exception:
        return None


def safe_stem(name: str) -> str:
    stem = Path(name or "").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")
    return stem[:40]


def resolve_stored(row) -> Path:
    """Map a DB row to its file, refusing anything outside PHOTOS_DIR."""
    src = (PHOTOS_DIR / row["stored_path"]).resolve()
    if not str(src).startswith(str(PHOTOS_DIR.resolve())):
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


def cached_image(path: Path, mimetype: str = "image/jpeg"):
    resp = send_file(path, mimetype=mimetype, conditional=True)
    resp.cache_control.private = True  # per-user; never let a proxy share these
    resp.cache_control.max_age = 86400
    return resp


def store_photo(file_storage, owner_id: int, device: str) -> dict:
    original = file_storage.filename or ""
    data = file_storage.read()
    if not data:
        return {"original_name": original, "status": "rejected", "reason": "empty file"}

    sha = hashlib.sha256(data).hexdigest()
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM photos WHERE owner_id = ? AND sha256 = ?", (owner_id, sha)
        ).fetchone()
    if row:
        return {"original_name": original, "status": "duplicate", "id": row["id"]}

    ident = identify_image(data)
    if ident is None:
        if len(data) > 12 and data[4:12] in HEIC_MAGIC:
            return {
                "original_name": original,
                "status": "rejected",
                "reason": "HEIC photo received but pillow-heif is not installed on the server",
            }
        return {
            "original_name": original,
            "status": "rejected",
            "reason": "not a supported image (unreadable)",
        }
    fmt, width, height = ident
    if fmt not in ALLOWED_FORMATS:
        return {
            "original_name": original,
            "status": "rejected",
            "reason": f"not a supported image (detected: {fmt})",
        }

    now = datetime.now()
    # Per-user subdirectory, so a filesystem-level slip can't cross accounts.
    subdir = PHOTOS_DIR / f"u{owner_id}" / now.strftime("%Y/%m/%d")
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
    # Write to a temp name and only move into place once the row is committed,
    # so losing an insert race can't delete the winner's file.
    tmp = subdir / f".{name}.{os.getpid()}.tmp"
    tmp.write_bytes(data)

    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO photos (owner_id, sha256, original_name, stored_path,"
                " format, size_bytes, width, height, device, uploaded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    sha,
                    original,
                    str(dest.relative_to(PHOTOS_DIR)),
                    fmt,
                    len(data),
                    width,
                    height,
                    device,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            photo_id = cur.lastrowid
        os.replace(tmp, dest)
    except sqlite3.IntegrityError:  # same photo uploaded concurrently
        tmp.unlink(missing_ok=True)
        return {"original_name": original, "status": "duplicate"}
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    log.info(
        "stored %s (%d bytes, %s) owner=%d device=%r -> %s",
        original or "<unnamed>", len(data), fmt, owner_id, device, dest,
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
    return render_template(
        "account.html",
        user=row,
        photo_count=count,
        mb_used=round(bytes_used / (1024 * 1024), 1),
        free=free_mb(PHOTOS_DIR),
        heic=HEIC_SUPPORT,
        fresh_token=session.pop("fresh_token", None),
        legacy_key_on=ALLOW_LEGACY_KEY and bool(API_KEY),
    )


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
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE owner_id = ?", (me.id,)
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT id, original_name, uploaded_at, device, width, height, format,"
            " size_bytes FROM photos WHERE owner_id = ?"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (me.id, PAGE_SIZE, offset),
        ).fetchall()
    return render_template(
        "gallery.html",
        photos=[dict(r) for r in rows],
        total=total,
        page=page,
        has_next=offset + len(rows) < total,
        free=free_mb(PHOTOS_DIR),
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
            render_cached(resolve_stored(row), tpath, THUMB_EDGE, 80)
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
    src = resolve_stored(row)
    if row["format"] in BROWSER_SAFE:
        mt = mimetypes.guess_type(str(src))[0] or "image/jpeg"
        return cached_image(src, mimetype=mt)
    dpath = DISPLAY_DIR / f"{photo_id}.jpg"
    if not dpath.exists():
        render_cached(src, dpath, DISPLAY_EDGE, 85)
    return cached_image(dpath)


@app.get("/original/<int:photo_id>")
@auth_required
def original(photo_id: int):
    """The untouched bytes, as a download."""
    row = get_photo_row(photo_id, g.identity.id)
    if row is None:
        abort(404)
    src = resolve_stored(row)
    mt = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
    return send_file(
        src,
        mimetype=mt,
        as_attachment=True,
        download_name=row["original_name"] or src.name,
    )


@app.get("/health")
def health():
    # Deliberately thin for anonymous callers; the detail lives on /account.
    if load_identity() is not None:
        return jsonify(
            {"status": "ok", "free_mb": free_mb(PHOTOS_DIR), "heic_support": HEIC_SUPPORT}
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
    if request.path.startswith("/health"):
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


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("create-user", "rotate-token"):
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        init_db()
        migrate()
        {"create-user": cmd_create_user, "rotate-token": cmd_rotate_token}[argv[0]](argv[1:])
        return

    if ALLOW_LEGACY_KEY and not API_KEY:
        log.info("ALLOW_LEGACY_KEY is on but API_KEY is unset; legacy uploads disabled.")
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
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
        "serving on %s:%d | photos -> %s | HEIC: %s | signup: %s",
        HOST, PORT, PHOTOS_DIR, HEIC_SUPPORT, "invite-only" if INVITE_CODE else "off",
    )
    try:
        from waitress import serve  # production-grade WSGI server

        serve(app, host=HOST, port=PORT)
    except ImportError:
        app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
