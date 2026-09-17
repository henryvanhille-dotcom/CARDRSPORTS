"""Private CARDR accounts, sessions, and consent-based profile controls.

The app can still be used as a single-device guest workspace during local
development.  On a hosted domain, guest mode is disabled by default and every
Vault, Watchlist, import, and portfolio request is scoped to a signed-in
collector.  Passwords are never stored directly; session cookies contain an
opaque random token whose hash is persisted server-side.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response

from database import get_connection, initialize_database


SESSION_COOKIE = "cardr_session"
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 310_000
MAX_EMAIL_LENGTH = 254
MAX_DISPLAY_NAME_LENGTH = 80
MAX_BIO_LENGTH = 600
MAX_POST_LENGTH = 600
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
HANDLE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$")
UPLOAD_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")


@dataclass(frozen=True)
class WorkspaceContext:
    """The authenticated owner for a request, or the local guest workspace."""

    owner_id: Optional[str]
    user: Optional[Dict[str, Any]]
    guest_mode: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _local_host(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "testserver"}


def guest_mode_enabled(request: Request) -> bool:
    """Allow a temporary single-device workspace only on a local server.

    A deploy can explicitly opt in with ``CARDR_GUEST_MODE=true``, but the
    safe default for a non-local host is false.  Render's production blueprint
    sets the variable to false as a defense in depth measure.
    """

    configured = os.getenv("CARDR_GUEST_MODE")
    if configured is not None:
        return _as_bool(configured, False)
    return _local_host(request)


def _normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Enter a valid email address.")
    return email


def _clean_display_name(value: Any) -> str:
    name = " ".join(str(value or "").strip().split())
    if not name:
        raise ValueError("Display name is required.")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError("Display name must be {} characters or fewer.".format(MAX_DISPLAY_NAME_LENGTH))
    return name


def _clean_bio(value: Any) -> str:
    bio = str(value or "").replace("\x00", "").strip()
    if len(bio) > MAX_BIO_LENGTH:
        raise ValueError("Bio must be {} characters or fewer.".format(MAX_BIO_LENGTH))
    return bio


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:32].strip("-") or "collector"


def _clean_handle(value: Any) -> str:
    handle = _slug(str(value or ""))
    if len(handle) < 3 or not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError("Profile handle must use 3–32 letters, numbers, or hyphens.")
    return handle


def _password_hash(password: str, salt: Optional[str] = None) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(password) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PASSWORD_ITERATIONS
    ).hex()
    return "pbkdf2_sha256${}${}${}".format(PASSWORD_ITERATIONS, salt, digest)


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        ).hex()
        return hmac.compare_digest(derived, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row: Any) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "profile_handle": row["profile_handle"],
        "bio": row["bio"] or "",
        "public_profile": bool(row["public_profile"]),
        "email_recap_enabled": bool(row["email_recap_enabled"]),
        "plan_id": row["plan_id"] or "free",
        "created_at": row["created_at"],
    }


def initialize_accounts() -> None:
    """Create account tables and persistence needed for a cloud-ready Vault."""

    initialize_database()
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                profile_handle TEXT NOT NULL UNIQUE COLLATE NOCASE,
                bio TEXT NOT NULL DEFAULT '',
                public_profile INTEGER NOT NULL DEFAULT 0,
                email_recap_enabled INTEGER NOT NULL DEFAULT 0,
                plan_id TEXT NOT NULL DEFAULT 'free',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                total_value_cad REAL NOT NULL,
                total_invested_cad REAL NOT NULL,
                profit_loss_cad REAL NOT NULL,
                card_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(owner_id, snapshot_date),
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_import_previews (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                records_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_notifications (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                dedupe_key TEXT,
                read_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_alert_rules (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                rule_json TEXT NOT NULL,
                last_valuation_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS account_alert_delivery_state (
                owner_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, dedupe_key),
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS community_posts (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploaded_files (
                filename TEXT PRIMARY KEY,
                owner_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry ON user_sessions(user_id, expires_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_owner_created ON account_notifications(owner_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_import_previews_owner_expiry ON collection_import_previews(owner_id, expires_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alert_rules_owner_updated ON account_alert_rules(owner_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_alert_delivery_owner_active ON account_alert_delivery_state(owner_id, active)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_community_posts_created ON community_posts(created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploaded_files_owner ON uploaded_files(owner_id)"
        )
        connection.commit()


def _unique_handle(connection: sqlite3.Connection, desired: str) -> str:
    base = _clean_handle(desired)
    candidate = base
    suffix = 0
    while connection.execute(
        "SELECT 1 FROM users WHERE profile_handle = ? COLLATE NOCASE", (candidate,)
    ).fetchone():
        suffix += 1
        ending = "-{}".format(secrets.token_hex(3)) if suffix > 2 else "-{}".format(suffix + 1)
        candidate = (base[: 32 - len(ending)] + ending).strip("-")
    return candidate


def register_account(email: Any, password: Any, display_name: Any) -> Tuple[Dict[str, Any], str]:
    initialize_accounts()
    normalized_email = _normalize_email(email)
    name = _clean_display_name(display_name)
    password_hash = _password_hash(password)
    now = _now()
    user_id = uuid.uuid4().hex
    try:
        with get_connection() as connection:
            handle = _unique_handle(connection, name)
            connection.execute(
                """
                INSERT INTO users (
                    id, email, display_name, password_hash, profile_handle,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, normalized_email, name, password_hash, handle, now, now),
            )
            connection.commit()
    except sqlite3.IntegrityError as error:
        raise ValueError("An account already exists for that email address.") from error
    user = get_user(user_id)
    if user is None:  # Defensive only; the insert above is transactional.
        raise RuntimeError("Could not create account.")
    return user, create_session(user_id)


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    initialize_accounts()
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public_user(row) if row is not None else None


def authenticate(email: Any, password: Any) -> Tuple[Dict[str, Any], str]:
    initialize_accounts()
    normalized_email = _normalize_email(email)
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (normalized_email,)).fetchone()
    if row is None or not _verify_password(str(password or ""), row["password_hash"]):
        # Use one generic response so the endpoint does not disclose which
        # email addresses have accounts.
        raise ValueError("Email or password is incorrect.")
    return _public_user(row), create_session(row["id"])


def create_session(user_id: str) -> str:
    initialize_accounts()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_DAYS)
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM user_sessions WHERE expires_at <= ?", (now.isoformat(),)
        )
        connection.execute(
            """
            INSERT INTO user_sessions (token_hash, user_id, expires_at, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_token_hash(token), user_id, expires.isoformat(), now.isoformat(), now.isoformat()),
        )
        connection.commit()
    return token


def revoke_session(token: Optional[str]) -> None:
    if not token:
        return
    initialize_accounts()
    with get_connection() as connection:
        connection.execute("DELETE FROM user_sessions WHERE token_hash = ?", (_token_hash(token),))
        connection.commit()


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    initialize_accounts()
    now = _now()
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT users.* FROM user_sessions
            JOIN users ON users.id = user_sessions.user_id
            WHERE user_sessions.token_hash = ? AND user_sessions.expires_at > ?
            """,
            (_token_hash(token), now),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, _token_hash(token)),
            )
            connection.commit()
    return _public_user(row) if row is not None else None


def workspace_context(request: Request) -> WorkspaceContext:
    user = current_user(request)
    if user is not None:
        return WorkspaceContext(owner_id=user["id"], user=user, guest_mode=False)
    if guest_mode_enabled(request):
        return WorkspaceContext(owner_id=None, user=None, guest_mode=True)
    raise HTTPException(
        status_code=401,
        detail="Create an account or sign in to access your private Cardr workspace.",
    )


def require_user(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def update_profile(user_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    initialize_accounts()
    allowed = {"display_name", "profile_handle", "bio", "public_profile", "email_recap_enabled"}
    requested = {key: value for key, value in values.items() if key in allowed}
    if not requested:
        user = get_user(user_id)
        if user is None:
            raise ValueError("Account not found.")
        return user

    clean: Dict[str, Any] = {}
    if "display_name" in requested:
        clean["display_name"] = _clean_display_name(requested["display_name"])
    if "profile_handle" in requested:
        clean["profile_handle"] = _clean_handle(requested["profile_handle"])
    if "bio" in requested:
        clean["bio"] = _clean_bio(requested["bio"])
    for boolean in ("public_profile", "email_recap_enabled"):
        if boolean in requested:
            value = requested[boolean]
            if isinstance(value, bool):
                clean[boolean] = int(value)
            elif isinstance(value, (int, float)) and value in {0, 1}:
                clean[boolean] = int(value)
            elif isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                clean[boolean] = int(value.strip().lower() in {"true", "1", "yes", "on"})
            else:
                raise ValueError("{} must be true or false.".format(boolean.replace("_", " ")))
    clean["updated_at"] = _now()
    assignments = ", ".join("{} = ?".format(field) for field in clean)
    try:
        with get_connection() as connection:
            cursor = connection.execute(
                "UPDATE users SET {} WHERE id = ?".format(assignments), [*clean.values(), user_id]
            )
            connection.commit()
    except sqlite3.IntegrityError as error:
        raise ValueError("That profile handle is already taken.") from error
    if cursor.rowcount == 0:
        raise ValueError("Account not found.")
    user = get_user(user_id)
    if user is None:
        raise ValueError("Account not found.")
    return user


def get_public_profile(handle: str) -> Optional[Dict[str, Any]]:
    candidate = _clean_handle(handle)
    initialize_accounts()
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, display_name, profile_handle, bio, created_at
            FROM users
            WHERE profile_handle = ? COLLATE NOCASE AND public_profile = 1
            """,
            (candidate,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "profile_handle": row["profile_handle"],
        "bio": row["bio"] or "",
        "created_at": row["created_at"],
    }


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    configured = os.getenv("CARDR_COOKIE_SECURE")
    secure = _as_bool(configured, not _local_host(request)) if configured is not None else not _local_host(request)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    configured = os.getenv("CARDR_COOKIE_SECURE")
    secure = _as_bool(configured, not _local_host(request)) if configured is not None else not _local_host(request)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="lax")


def save_import_preview(owner_id: str, records: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Persist server-produced preview records until an explicit confirmation."""

    initialize_accounts()
    if len(records) > 5_000:
        raise ValueError("Import preview exceeds the 5,000-row limit.")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=2)
    import_id = uuid.uuid4().hex
    with get_connection() as connection:
        connection.execute("DELETE FROM collection_import_previews WHERE expires_at <= ?", (now.isoformat(),))
        connection.execute(
            """
            INSERT INTO collection_import_previews (id, owner_id, records_json, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (import_id, owner_id, json.dumps(records, separators=(",", ":")), now.isoformat(), expires.isoformat()),
        )
        connection.commit()
    return {"id": import_id, "expires_at": expires.isoformat()}


def consume_import_preview(import_id: str, owner_id: str) -> list[Dict[str, Any]]:
    initialize_accounts()
    now = _now()
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT records_json FROM collection_import_previews
            WHERE id = ? AND owner_id = ? AND consumed_at IS NULL AND expires_at > ?
            """,
            (import_id, owner_id, now),
        ).fetchone()
        if row is None:
            raise ValueError("This import preview is unavailable or has expired. Preview the CSV again.")
        connection.execute(
            "UPDATE collection_import_previews SET consumed_at = ? WHERE id = ? AND owner_id = ?",
            (now, import_id, owner_id),
        )
        connection.commit()
    try:
        records = json.loads(row["records_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("This import preview could not be read. Preview the CSV again.") from error
    if not isinstance(records, list):
        raise ValueError("This import preview is invalid. Preview the CSV again.")
    return records


def add_notification(
    owner_id: str,
    *,
    kind: str,
    title: str,
    body: str,
    payload: Optional[Dict[str, Any]] = None,
    dedupe_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Store an in-app alert. Email delivery deliberately lives elsewhere."""

    initialize_accounts()
    if not owner_id or len(kind) > 60 or len(title) > 160 or len(body) > 1000:
        raise ValueError("Notification fields are invalid.")
    notification_id = uuid.uuid4().hex
    created = _now()
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO account_notifications (
                    id, owner_id, kind, title, body, payload_json, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    owner_id,
                    kind,
                    title,
                    body,
                    json.dumps(payload or {}, separators=(",", ":")),
                    dedupe_key,
                    created,
                ),
            )
            connection.commit()
    except sqlite3.IntegrityError:
        return None
    return {
        "id": notification_id,
        "kind": kind,
        "title": title,
        "body": body,
        "payload": payload or {},
        "created_at": created,
        "read_at": None,
    }


def list_notifications(owner_id: str, limit: int = 30) -> list[Dict[str, Any]]:
    initialize_accounts()
    limit = max(1, min(int(limit), 100))
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM account_notifications WHERE owner_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()
    notifications = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        notifications.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "payload": payload,
                "created_at": row["created_at"],
                "read_at": row["read_at"],
            }
        )
    return notifications


def _clean_upload_filename(value: Any) -> str:
    filename = str(value or "").strip()
    if not UPLOAD_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("Uploaded file name is invalid.")
    return filename


def record_uploaded_file(filename: Any, owner_id: Optional[str]) -> None:
    """Associate a safe stored image name with its private workspace."""

    filename = _clean_upload_filename(filename)
    initialize_accounts()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO uploaded_files (filename, owner_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET owner_id = excluded.owner_id
            """,
            (filename, owner_id, _now()),
        )
        connection.commit()


def get_uploaded_file(filename: Any) -> Optional[Dict[str, Any]]:
    filename = _clean_upload_filename(filename)
    initialize_accounts()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT filename, owner_id, created_at FROM uploaded_files WHERE filename = ?", (filename,)
        ).fetchone()
    return dict(row) if row is not None else None


def claim_uploaded_files(owner_id: str, filenames: list[str]) -> None:
    """Attach only explicitly claimed local guest photos to a new account."""

    initialize_accounts()
    safe_filenames = []
    for filename in filenames:
        try:
            safe_filenames.append(_clean_upload_filename(filename))
        except ValueError:
            continue
    if not safe_filenames:
        return
    with get_connection() as connection:
        for filename in safe_filenames:
            connection.execute(
                """
                INSERT INTO uploaded_files (filename, owner_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET owner_id = excluded.owner_id
                WHERE uploaded_files.owner_id IS NULL
                """,
                (filename, owner_id, _now()),
            )
        connection.commit()


def remove_uploaded_files(owner_id: str) -> list[str]:
    """Remove ownership records and return safe filenames for account deletion."""

    initialize_accounts()
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT filename FROM uploaded_files WHERE owner_id = ?", (owner_id,)
        ).fetchall()
        connection.execute("DELETE FROM uploaded_files WHERE owner_id = ?", (owner_id,))
        connection.commit()
    return [row["filename"] for row in rows if UPLOAD_FILENAME_PATTERN.fullmatch(row["filename"])]
