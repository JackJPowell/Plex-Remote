"""
plex-remote — local media server automation API
Designed for Home Assistant REST calls on a private LAN.
"""

import asyncio
import sqlite3
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from plexapi.server import PlexServer

from home_assistant import HomeAssistantNotifier, HomeAssistantStateMonitor

load_dotenv()
logger = logging.getLogger("plex-remote")

# ── Config (set these in .env) ────────────────────────────────────────────────
PLEX_URL = os.getenv("PLEX_URL", "http://10.0.0.10:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "QU9t1Z4UQu8-3jxKzBw6")
PLEX_TIMEOUT = int(os.getenv("PLEX_TIMEOUT", "30"))
PLEX_PLAY_COMMAND_TIMEOUT = int(os.getenv("PLEX_PLAY_COMMAND_TIMEOUT", "5"))
PLEX_CLIENT_STARTUP_TIMEOUT = int(os.getenv("PLEX_CLIENT_STARTUP_TIMEOUT", "45"))
PLEX_CLIENT_NAME = os.getenv("PLEX_CLIENT_NAME", "Emu")
PLEX_CLIENT_URL = os.getenv("PLEX_CLIENT_URL", "").strip()
PLEX_CLIENT_PROXY_THROUGH_SERVER = (
    os.getenv("PLEX_CLIENT_PROXY_THROUGH_SERVER", "true").lower()
    in ("1", "true", "yes", "on")
)
PLEX_HTPC_BINARY = os.getenv("PLEX_HTPC_BINARY", "plex-htpc")

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
FRONTEND_DIST_DIR = os.path.join(os.path.dirname(__file__), "frontend", "dist")
FRONTEND_ASSETS_DIR = os.path.join(FRONTEND_DIST_DIR, "assets")
FRONTEND_INDEX = os.path.join(FRONTEND_DIST_DIR, "index.html")
ARTWORK_CACHE_DIR = Path(os.getenv(
    "ARTWORK_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), ".cache", "artwork"),
))
ARTWORK_MAX_HEIGHT = int(os.getenv("ARTWORK_MAX_HEIGHT", "300"))
ARTWORK_WEBP_QUALITY = int(os.getenv("ARTWORK_WEBP_QUALITY", "75"))
ARTWORK_CACHE_VERSION = 1
STATE_DB_PATH = Path(os.getenv(
    "PLEX_REMOTE_DB",
    os.path.join(os.path.dirname(__file__), ".cache", "plex-remote", "state.sqlite3"),
))
AUTOMATION_POLL_INTERVAL = float(os.getenv("AUTOMATION_POLL_INTERVAL", "4"))
AUTOMATION_IDLE_GRACE_SECONDS = float(os.getenv("AUTOMATION_IDLE_GRACE_SECONDS", "6"))
HOME_ASSISTANT_URL = os.getenv("HOME_ASSISTANT_URL", "").strip()
HOME_ASSISTANT_ACCESS_TOKEN = os.getenv("HOME_ASSISTANT_ACCESS_TOKEN", "").strip()
HOME_ASSISTANT_NOTIFY_ENTITY = os.getenv(
    "HOME_ASSISTANT_NOTIFY_ENTITY",
    os.getenv("HOME_ASSISTANT_NOTIFY_SERVICE", "notify.iphone_jack"),
).strip()

# Shared lock file — ensures only one cec-client process runs at a time.
# status.sh also acquires this lock before calling cec-client.
_CEC_LOCK = "/tmp/cec.lock"

# Last-known TV volume (0–100). Only updated when the user explicitly sets it
# via POST — the TV does not support CEC audio status queries.
_tv_volume: Optional[int] = None
_volume_queried = False

# Last-known HDMI source (port number 1–9), or None if unknown.
# Samsung TVs do not respond to CEC active-source queries; track via API only.
_tv_source: Optional[int] = None

# Last-known TV power state. CEC power queries can block for several seconds,
# so /status returns the cache and refreshes it in the background.
_tv_status: Optional[str] = None
_tv_power_refresh_task: Optional[asyncio.Task] = None
_tv_power_last_refresh = 0.0
_TV_POWER_REFRESH_INTERVAL = 15.0
_automation_task: Optional[asyncio.Task] = None
_automation_lock: Optional[asyncio.Lock] = None
_plex_server_instance: Optional[PlexServer] = None
_plex_server_lock = threading.Lock()
_home_assistant_monitor = HomeAssistantStateMonitor(
    HOME_ASSISTANT_URL,
    HOME_ASSISTANT_ACCESS_TOKEN,
)
_home_assistant_notifier = HomeAssistantNotifier(
    HOME_ASSISTANT_URL,
    HOME_ASSISTANT_ACCESS_TOKEN,
    HOME_ASSISTANT_NOTIFY_ENTITY,
)

# D-Bus / XDG environment required for `systemctl --user` in subprocesses.
# Captured at startup so worker threads and async handlers can pass them.
_SYSTEMD_USER_ENV: dict = {
    **os.environ,
    "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    ),
    "XDG_RUNTIME_DIR": os.environ.get(
        "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
    ),
}

app = FastAPI(
    title="Plex Remote",
    description="Local media-server automation API. All endpoints are JSON.",
    version="0.1.0",
)
app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_ASSETS_DIR, check_dir=False),
    name="spa-assets",
)


# ── Shared playback state ────────────────────────────────────────────────────


class QueueCreate(BaseModel):
    media_id: int
    title: Optional[str] = None
    media_type: Optional[str] = None
    artwork_url: Optional[str] = None


class QueueReorder(BaseModel):
    ids: list[int]


class TimerUpdate(BaseModel):
    hours_delta: Optional[int] = None
    clear: bool = False


class SeekUpdate(BaseModel):
    percent: float = Field(ge=0, le=100)


class MessageWrite(BaseModel):
    text: str
    starts_at: str
    ends_at: str
    enabled: bool = True


class ClientLogWrite(BaseModel):
    message: str
    path: Optional[str] = None
    details: Optional[str] = None


def _now_ts() -> int:
    return int(time.time())


def _utc_iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="minutes")


def _parse_local_datetime(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {value}") from exc
    return int(parsed.timestamp())


def _db() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _db_connection():
    """Provide a transaction and always close its SQLite file descriptors."""
    conn = _db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init_state_db() -> None:
    with _db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS playback_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                title TEXT,
                media_type TEXT,
                artwork_url TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS playback_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                starts_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS error_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                level TEXT NOT NULL,
                log_type TEXT NOT NULL,
                message TEXT NOT NULL,
                path TEXT,
                method TEXT,
                status_code INTEGER,
                details TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_error_logs_created_at
                ON error_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_error_logs_type_created
                ON error_logs(log_type, created_at DESC);
            """
        )


def _log_type_for_path(path: str) -> str:
    first = path.strip("/").split("/", 1)[0]
    return first or "system"


def _write_error_log(
    *,
    message: str,
    log_type: str,
    level: str = "error",
    path: Optional[str] = None,
    method: Optional[str] = None,
    status_code: Optional[int] = None,
    details: Optional[str] = None,
) -> None:
    try:
        with _db_connection() as conn:
            conn.execute(
                """
                INSERT INTO error_logs
                    (created_at, level, log_type, message, path, method, status_code, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_now_ts(), level, log_type, message, path, method, status_code, details),
            )
    except Exception:
        logger.exception("Could not persist error log")


@app.exception_handler(HTTPException)
async def persist_http_exception(request: Request, exc: HTTPException):
    if exc.status_code >= 400:
        _write_error_log(
            message=str(exc.detail),
            log_type=_log_type_for_path(request.url.path),
            level="warning" if exc.status_code < 500 else "error",
            path=request.url.path,
            method=request.method,
            status_code=exc.status_code,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def persist_validation_exception(request: Request, exc: RequestValidationError):
    _write_error_log(
        message="Request validation failed",
        log_type=_log_type_for_path(request.url.path),
        level="warning",
        path=request.url.path,
        method=request.method,
        status_code=422,
        details=json.dumps(exc.errors(), default=str),
    )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def persist_unhandled_exception(request: Request, exc: Exception):
    _write_error_log(
        message=str(exc) or exc.__class__.__name__,
        log_type=_log_type_for_path(request.url.path),
        path=request.url.path,
        method=request.method,
        status_code=500,
        details=exc.__class__.__name__,
    )
    logger.exception("Unhandled request error")
    return Response(content='{"detail":"Internal server error"}', status_code=500, media_type="application/json")


def _queue_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "position": row["position"],
        "media_id": row["media_id"],
        "title": row["title"],
        "type": row["media_type"],
        "artwork_url": row["artwork_url"],
        "created_at": _utc_iso(row["created_at"]),
    }


def _message_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "starts_at": _utc_iso(row["starts_at"]),
        "ends_at": _utc_iso(row["ends_at"]),
        "enabled": bool(row["enabled"]),
        "active": bool(row["enabled"]) and row["starts_at"] <= _now_ts() <= row["ends_at"],
        "created_at": _utc_iso(row["created_at"]),
        "updated_at": _utc_iso(row["updated_at"]),
    }


def _get_setting(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM playback_settings WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    if value is None:
        conn.execute("DELETE FROM playback_settings WHERE key = ?", (key,))
        return
    conn.execute(
        """
        INSERT INTO playback_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _queue_items(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    owns_conn = conn is None
    conn = conn or _db()
    try:
        rows = conn.execute(
            "SELECT * FROM playback_queue ORDER BY position ASC, id ASC"
        ).fetchall()
        return [_queue_row(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def _active_messages(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    owns_conn = conn is None
    conn = conn or _db()
    try:
        now = _now_ts()
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE enabled = 1 AND starts_at <= ? AND ends_at >= ?
            ORDER BY starts_at ASC, id ASC
            """,
            (now, now),
        ).fetchall()
        return [_message_row(row) for row in rows]
    finally:
        if owns_conn:
            conn.close()


def _timer_state(conn: Optional[sqlite3.Connection] = None) -> dict:
    owns_conn = conn is None
    conn = conn or _db()
    try:
        raw = _get_setting(conn, "timer_expires_at")
        expires_at = int(raw) if raw else None
        remaining = max(0, expires_at - _now_ts()) if expires_at else 0
        return {
            "active": remaining > 0,
            "expires_at": _utc_iso(expires_at),
            "remaining_seconds": remaining,
        }
    finally:
        if owns_conn:
            conn.close()


def _playback_state(now_playing: Optional[dict] = None) -> dict:
    with _db_connection() as conn:
        return {
            "queue": _queue_items(conn),
            "timer": _timer_state(conn),
            "active_messages": _active_messages(conn),
            "now_playing": now_playing,
        }


def _next_queue_item() -> Optional[dict]:
    with _db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM playback_queue ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        return _queue_row(row) if row else None


def _delete_queue_item(item_id: int) -> None:
    with _db_connection() as conn:
        conn.execute("DELETE FROM playback_queue WHERE id = ?", (item_id,))
        rows = conn.execute(
            "SELECT id FROM playback_queue ORDER BY position ASC, id ASC"
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE playback_queue SET position = ? WHERE id = ?",
                (index, row["id"]),
            )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cec(command: str, timeout: int = 10) -> None:
    """Send a single cec-client command via stdin, serialised with flock."""
    subprocess.run(
        f'flock -w {timeout} {_CEC_LOCK} bash -c \'echo "{command}" | cec-client -s -d 1\'',
        shell=True,
        capture_output=True,
        timeout=timeout + 5,
    )


def _plex_server() -> PlexServer:
    global _plex_server_instance
    if not PLEX_TOKEN:
        raise HTTPException(status_code=503, detail="PLEX_TOKEN not configured")
    if _plex_server_instance is None:
        with _plex_server_lock:
            if _plex_server_instance is None:
                try:
                    _plex_server_instance = PlexServer(
                        PLEX_URL,
                        PLEX_TOKEN,
                        timeout=PLEX_TIMEOUT,
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Cannot connect to Plex server: {exc}",
                    ) from exc
    return _plex_server_instance


def _close_plex_server() -> None:
    """Close the cached Plex HTTP connection pool during application shutdown."""
    global _plex_server_instance
    with _plex_server_lock:
        plex = _plex_server_instance
        _plex_server_instance = None
        if plex is not None:
            plex._session.close()


def _plex_client(plex: PlexServer):
    if not PLEX_CLIENT_NAME:
        raise HTTPException(status_code=503, detail="PLEX_CLIENT_NAME not configured")
    try:
        client = plex.client(PLEX_CLIENT_NAME)
        if PLEX_CLIENT_URL:
            client._baseurl = PLEX_CLIENT_URL.rstrip("/")
        client._proxyThroughServer = PLEX_CLIENT_PROXY_THROUGH_SERVER
        return client
    except Exception as exc:
        available = [c.title for c in plex.clients()]
        raise HTTPException(
            status_code=503,
            detail=(
                f"Plex client '{PLEX_CLIENT_NAME}' not reachable: {exc}. "
                f"Available clients: {available}"
            ),
        )


def _empty_now_playing() -> dict:
    """Return a stable response shape when the configured client is idle."""
    return {
        "playing": False,
        "state": None,
        "media_type": None,
        "movie_title": None,
        "show_name": None,
        "season_num": None,
        "episode_num": None,
        "episode_name": None,
        "display_title": None,
        "artwork_url": None,
        "rating_key": None,
        "duration": None,
        "progress": None,
        "progress_percent": None,
    }


def _plex_now_playing(plex: PlexServer) -> dict:
    """Describe media playing on PLEX_CLIENT_NAME, if any."""
    for item in plex.sessions():
        players = getattr(item, "players", [])
        player = next(
            (
                candidate
                for candidate in players
                if candidate.title == PLEX_CLIENT_NAME
            ),
            None,
        )
        if player is None:
            continue

        media_type = getattr(item, "type", None)
        is_episode = media_type == "episode"
        movie_title = getattr(item, "title", None) if media_type == "movie" else None
        show_name = getattr(item, "grandparentTitle", None) if is_episode else None
        season_num = getattr(item, "parentIndex", None) if is_episode else None
        episode_num = getattr(item, "index", None) if is_episode else None
        episode_name = getattr(item, "title", None) if is_episode else None

        if is_episode:
            display_title = (
                f"{show_name} S{season_num:02d}E{episode_num:02d} - {episode_name}"
                if show_name is not None
                and season_num is not None
                and episode_num is not None
                else episode_name
            )
            # grandparentThumb is the show's poster, rather than the episode still.
            artwork_path = getattr(item, "grandparentThumb", None)
        else:
            display_title = movie_title or getattr(item, "title", None)
            artwork_path = getattr(item, "thumb", None)

        duration = getattr(item, "duration", None)
        progress = getattr(item, "viewOffset", None)
        progress_percent = None
        if duration and progress is not None:
            progress_percent = max(0, min(100, round((progress / duration) * 100, 1)))

        return {
            "playing": True,
            "state": getattr(player, "state", None),
            "media_type": media_type,
            "movie_title": movie_title,
            "show_name": show_name,
            "season_num": season_num,
            "episode_num": episode_num,
            "episode_name": episode_name,
            "display_title": display_title,
            "artwork_url": _media_artwork_url(item) if artwork_path else None,
            "rating_key": getattr(item, "ratingKey", None),
            "duration": duration,
            "progress": progress,
            "progress_percent": progress_percent,
        }

    return _empty_now_playing()


def _media_artwork_url(item) -> Optional[str]:
    artwork_path = (
        getattr(item, "thumb", None)
        or getattr(item, "grandparentThumb", None)
        or getattr(item, "art", None)
    )
    if not artwork_path:
        return None
    rating_key = getattr(item, "ratingKey", None)
    variant = f"poster-h{ARTWORK_MAX_HEIGHT}-webp-v{ARTWORK_CACHE_VERSION}"
    url = f"/plex/artwork?path={quote(artwork_path, safe='')}&variant={variant}"
    if rating_key is not None:
        url += f"&rating_key={quote(str(rating_key), safe='')}"
    return url


def _coerce_int_list(value) -> list[int]:
    if value in (None, "", False):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(x.strip()) for x in value.split(",") if x.strip()]
    if isinstance(value, list):
        return [int(x) for x in value if x not in (None, "")]
    return []


def _load_curated_config(env_name: str) -> list[dict]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return []
    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_name} must be valid JSON: {exc}") from exc
    if not isinstance(configs, list):
        raise RuntimeError(f"{env_name} must be a JSON array")

    normalized = []
    for entry in configs:
        if not isinstance(entry, dict) or "rating_key" not in entry:
            raise RuntimeError(f"{env_name} entries must contain rating_key")
        item = {**entry, "rating_key": str(entry["rating_key"])}
        if "months" in item:
            item["months"] = _coerce_int_list(item.get("months"))
        if "seasons" in item:
            item["seasons"] = _coerce_int_list(item.get("seasons"))
        normalized.append(item)
    return normalized


CURATED_MOVIES = _load_curated_config("CURATED_MOVIES")
CURATED_SHOWS = _load_curated_config("CURATED_SHOWS")


def _movie_config_is_active(config: dict) -> bool:
    months = config.get("months") or []
    return not months or datetime.now().month in months


def _movie_config_is_random_eligible(config: dict) -> bool:
    return config.get("random", True) is not False and _movie_config_is_active(config)


def _show_config_for(rating_key: str) -> Optional[dict]:
    return next(
        (config for config in CURATED_SHOWS if str(config["rating_key"]) == str(rating_key)),
        None,
    )


def _season_allowed(config: Optional[dict], season_index: Optional[int]) -> bool:
    seasons = (config or {}).get("seasons") or []
    return not seasons or season_index in seasons


def _serialize_media_item(item) -> dict:
    return {
        "rating_key": int(item.ratingKey),
        "title": getattr(item, "title", None),
        "type": getattr(item, "type", None),
        "year": getattr(item, "year", None),
        "summary": getattr(item, "summary", None),
        "artwork_url": _media_artwork_url(item),
    }


def _serialize_season(season) -> dict:
    return {
        "rating_key": int(season.ratingKey),
        "title": getattr(season, "title", None),
        "type": getattr(season, "type", "season"),
        "index": getattr(season, "index", None),
        "summary": getattr(season, "summary", None),
        "artwork_url": _media_artwork_url(season),
    }


def _serialize_episode(episode) -> dict:
    return {
        "rating_key": int(episode.ratingKey),
        "title": getattr(episode, "title", None),
        "type": getattr(episode, "type", "episode"),
        "season_num": getattr(episode, "parentIndex", None),
        "episode_num": getattr(episode, "index", None),
        "summary": getattr(episode, "summary", None),
        "artwork_url": _media_artwork_url(episode),
    }


def _curated_media(plex: PlexServer, configs: list[dict]) -> list[dict]:
    items = []
    for config in configs:
        rating_key = config["rating_key"]
        try:
            item = plex.fetchItem(int(rating_key))
        except Exception as exc:
            items.append(
                {
                    "rating_key": rating_key,
                    "title": f"Unavailable item {rating_key}",
                    "type": "unknown",
                    "year": None,
                    "summary": str(exc),
                    "artwork_url": None,
                    "unavailable": True,
                }
            )
            continue
        data = _serialize_media_item(item)
        if config.get("months"):
            data["months"] = config["months"]
        if config.get("random") is False:
            data["random"] = False
        data["available_now"] = _movie_config_is_active(config)
        data["random_eligible"] = _movie_config_is_random_eligible(config)
        if config.get("seasons"):
            data["seasons"] = config["seasons"]
        items.append(data)
    return items


def _curated_show_seasons(show) -> list:
    config = _show_config_for(str(show.ratingKey))
    return [
        season
        for season in show.seasons()
        if _season_allowed(config, getattr(season, "index", None))
    ]


def _episode_pool_for_show(show) -> list:
    episodes = []
    for season in _curated_show_seasons(show):
        episodes.extend(season.episodes())
    return episodes


def _artwork_cache_paths(path: str, rating_key: Optional[str]) -> tuple[Path, Path]:
    content_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(rating_key or "artwork")).strip("._")
    if not content_id:
        content_id = "artwork"
    path_hash = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    base = ARTWORK_CACHE_DIR / f"{content_id}-{path_hash}"
    return base.with_suffix(".img"), base.with_suffix(".json")


def _read_cached_artwork(path: str, rating_key: Optional[str]) -> Optional[FileResponse]:
    image_path, meta_path = _artwork_cache_paths(path, rating_key)
    if not image_path.exists():
        return None
    meta = {}
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        pass
    cache_is_current = (
        meta.get("optimization_version") == ARTWORK_CACHE_VERSION
        and meta.get("max_height") == ARTWORK_MAX_HEIGHT
        and meta.get("quality") == ARTWORK_WEBP_QUALITY
    )
    if not cache_is_current:
        try:
            _write_cached_artwork(
                path,
                rating_key,
                image_path.read_bytes(),
                meta.get("content_type", "image/jpeg"),
            )
            meta = json.loads(meta_path.read_text())
        except Exception as exc:
            logger.warning("Could not optimize cached artwork %s: %s", image_path, exc)
    media_type = meta.get("content_type") or "image/jpeg"
    return FileResponse(
        image_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _write_cached_artwork(
    path: str,
    rating_key: Optional[str],
    content: bytes,
    content_type: str,
) -> bytes:
    image_path, meta_path = _artwork_cache_paths(path, rating_key)
    ARTWORK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        if image.height > ARTWORK_MAX_HEIGHT:
            width = max(1, round(image.width * ARTWORK_MAX_HEIGHT / image.height))
            image = image.resize((width, ARTWORK_MAX_HEIGHT), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        output = BytesIO()
        image.save(
            output,
            format="WEBP",
            quality=ARTWORK_WEBP_QUALITY,
            method=4,
        )
        optimized = output.getvalue()
        metadata = {
            "path": path,
            "content_type": "image/webp",
            "source_content_type": content_type,
            "optimization_version": ARTWORK_CACHE_VERSION,
            "max_height": ARTWORK_MAX_HEIGHT,
            "width": image.width,
            "height": image.height,
            "quality": ARTWORK_WEBP_QUALITY,
        }

    temp_id = f"{os.getpid()}-{time.time_ns()}"
    image_tmp = image_path.with_name(f"{image_path.name}.{temp_id}.tmp")
    meta_tmp = meta_path.with_name(f"{meta_path.name}.{temp_id}.tmp")
    image_tmp.write_bytes(optimized)
    meta_tmp.write_text(json.dumps(metadata))
    image_tmp.replace(image_path)
    meta_tmp.replace(meta_path)
    return optimized


def _stop_plex_playback(client) -> None:
    """Stop playback on the configured Plex client."""
    try:
        client.stop()
    except TypeError:
        client.stop(mtype="video")


def _is_read_timeout(exc: Exception) -> bool:
    """Return True for the Plex command timeouts HTPC can emit after accepting play."""
    message = str(exc).lower()
    return "read timed out" in message or "readtimeout" in message


def _item_is_now_playing(plex: PlexServer, rating_key: str) -> bool:
    """Check whether the configured Plex client is playing the requested item."""
    now_playing = _plex_now_playing(plex)
    return (
        now_playing["playing"]
        and str(now_playing["rating_key"]) == str(rating_key)
    )


async def _wait_for_plex_client(plex: PlexServer, timeout: int) -> None:
    """Wait until Plex HTPC advertises itself as a controllable client."""
    for _ in range(timeout):
        try:
            _plex_client(plex)
            return
        except HTTPException:
            await asyncio.sleep(1)
    raise HTTPException(
        status_code=504,
        detail=(
            "Plex HTPC started but did not register as a client "
            f"within {timeout} seconds"
        ),
    )


async def _send_play_command(client, item) -> bool:
    """Send play and tolerate HTPC read timeouts when playback actually starts."""
    timed_out = False
    loop = asyncio.get_running_loop()
    command = loop.run_in_executor(None, client.playMedia, item)
    try:
        await asyncio.wait_for(command, timeout=PLEX_PLAY_COMMAND_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as exc:
        if not _is_read_timeout(exc):
            raise
        timed_out = True

    if not timed_out:
        return False

    for _ in range(8):
        try:
            if _item_is_now_playing(_plex_server(), item.ratingKey):
                return True
        except HTTPException:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Timed out waiting for Plex client '{PLEX_CLIENT_NAME}' to acknowledge play"
    )


def _cec_get_volume() -> Optional[int]:
    """Return the current TV volume (0–100), or None if it cannot be determined.

    The CEC query is attempted exactly once. If it succeeds the value is stored
    and returned on every subsequent call. If it fails, None is stored and
    returned immediately on every subsequent call without retrying.
    """
    global _tv_volume, _volume_queried
    if _volume_queried:
        return _tv_volume
    _volume_queried = True
    try:
        result = subprocess.run(
            f"flock -w 15 {_CEC_LOCK} cec-client -s -d 8",
            input="tx FF:71\n",
            shell=True,
            capture_output=True,
            text=True,
            timeout=6,
        )
        # The responding device sends Report Audio Status (opcode 7A).
        # CEC frame: XX:7A:VV  — bit 7 of VV = mute, bits 6:0 = volume (0–100).
        for line in result.stdout.splitlines():
            m = re.search(r'7[Aa]:([0-9a-fA-F]{2})', line)
            if m:
                _tv_volume = int(m.group(1), 16) & 0x7F
                return _tv_volume
    except Exception:
        pass
    return None


def _cec_adjust_volume(steps: int, timeout: int = 120) -> None:
    """Send *steps* volup (positive) or voldown (negative) commands in a
    single cec-client session so the init delay is only paid once."""
    if steps == 0:
        return
    cmd = "volup" if steps > 0 else "voldown"
    commands = "\n".join([cmd] * abs(steps)) + "\n"
    subprocess.run(
        f"flock -w {timeout + 30} {_CEC_LOCK} cec-client -s -d 1",
        input=commands,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout + 35,
    )


async def _query_tv_power() -> Optional[str]:
    """Query the TV power state via CEC and return 'on', 'off', or None."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                f"flock -w 5 {_CEC_LOCK} cec-client -s -d 1",
                input="pow 0\n",
                shell=True,
                capture_output=True,
                text=True,
                timeout=12,
            ),
        )
        out = result.stdout.lower()
        if "power status: on" in out:
            return "on"
        if "power status: standby" in out or "power status: off" in out:
            return "off"
    except Exception:
        pass
    return None


async def _refresh_tv_power_status() -> None:
    """Update cached TV power without making /status wait on CEC."""
    global _tv_status, _tv_power_refresh_task, _tv_power_last_refresh
    try:
        status = await _query_tv_power()
        if status is not None:
            _tv_status = status
    finally:
        _tv_power_last_refresh = time.monotonic()
        _tv_power_refresh_task = None


def _schedule_tv_power_refresh() -> None:
    global _tv_power_refresh_task
    refresh_due = time.monotonic() - _tv_power_last_refresh >= _TV_POWER_REFRESH_INTERVAL
    if refresh_due and _tv_power_refresh_task is None:
        _tv_power_refresh_task = asyncio.create_task(_refresh_tv_power_status())


async def _ensure_tv_on() -> bool:
    """Send the idempotent TV-on command before playback.

    Some TVs return a stale CEC power status, so the query is useful for the
    response metadata but must not decide whether the working on command is
    sent.
    """
    global _tv_status
    current = await _query_tv_power()
    _cec("on 0")
    _tv_status = "on"
    return current != "on"


def _ensure_plex_htpc_running() -> bool:
    """Start the plex-htpc user service if it is not already active.

    Uses `systemctl is-active` as the authoritative check rather than pgrep,
    so orphaned plex-bin processes left behind by a previous crash or manual
    stop don't prevent a fresh start.  Any such orphans are killed first.
    """
    active = subprocess.run(
        "systemctl --user is-active plex-htpc",
        shell=True,
        capture_output=True,
        env=_SYSTEMD_USER_ENV,
    )
    if active.returncode == 0:
        return False  # service is genuinely running
    # Kill any orphaned plex-bin processes that survived a previous stop
    subprocess.run("pkill -9 -f 'plex-bin'", shell=True, capture_output=True)
    started = subprocess.run(
        "systemctl --user start --no-block plex-htpc",
        shell=True,
        capture_output=True,
        env=_SYSTEMD_USER_ENV,
    )
    if started.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start Plex HTPC: {started.stderr.decode().strip()}",
        )
    return True


async def _resolve_play_item(plex: PlexServer, now_playing: dict, media_id: Optional[int]):
    """Resolve the requested media item, matching /plex/play selection rules."""
    if media_id is not None:
        try:
            item = plex.fetchItem(media_id)
        except Exception as exc:
            raise HTTPException(
                status_code=404, detail=f"Media {media_id} not found: {exc}"
            )

        if item.type == "show":
            episodes = _episode_pool_for_show(item)
            if not episodes:
                raise HTTPException(
                    status_code=404, detail="No episodes found for that curated show"
                )
            item = random.choice(episodes)
        elif item.type == "season":
            episodes = item.episodes()
            if not episodes:
                raise HTTPException(
                    status_code=404, detail="No episodes found for that season"
                )
            item = random.choice(episodes)
        return item

    active_movies = [
        config
        for config in CURATED_MOVIES
        if _movie_config_is_random_eligible(config)
    ]
    if not active_movies:
        raise HTTPException(
            status_code=400,
            detail="No curated movies available - set CURATED_MOVIES in .env",
        )

    current_rating_key = str(now_playing["rating_key"])
    choices = [
        config
        for config in active_movies
        if str(config["rating_key"]) != current_rating_key
    ]
    rating_key = int(random.choice(choices or active_movies)["rating_key"])
    try:
        return plex.fetchItem(rating_key)
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail=f"Curated item {rating_key} not found: {exc}"
        )


async def _resolve_random_movie_or_show(plex: PlexServer, now_playing: dict):
    candidates: list[tuple[str, int]] = []
    current_rating_key = str(now_playing["rating_key"])
    for config in CURATED_MOVIES:
        if _movie_config_is_random_eligible(config):
            candidates.append(("movie", int(config["rating_key"])))
    for config in CURATED_SHOWS:
        candidates.append(("show", int(config["rating_key"])))

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No curated movies or shows available for random playback",
        )

    filtered = [
        candidate
        for candidate in candidates
        if str(candidate[1]) != current_rating_key
    ]
    media_type, rating_key = random.choice(filtered or candidates)
    try:
        item = plex.fetchItem(rating_key)
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail=f"Curated item {rating_key} not found: {exc}"
        )

    if media_type == "show":
        episodes = _episode_pool_for_show(item)
        if not episodes:
            raise HTTPException(
                status_code=404, detail="No episodes found for that curated show"
            )
        return random.choice(episodes)
    return item


async def _play_plex_media(
    media_id: Optional[int],
    startup_timeout: int = PLEX_CLIENT_STARTUP_TIMEOUT,
    random_movie_or_show: bool = False,
    ensure_tv: bool = True,
) -> dict:
    """Ensure the TV and HTPC are ready, then select and play media."""
    tv_powered_on = await _ensure_tv_on() if ensure_tv else False
    started = _ensure_plex_htpc_running()
    plex = _plex_server()

    if started:
        await _wait_for_plex_client(plex, startup_timeout)

    client = _plex_client(plex)
    now_playing = _plex_now_playing(plex)
    if random_movie_or_show:
        item = await _resolve_random_movie_or_show(plex, now_playing)
    else:
        item = await _resolve_play_item(plex, now_playing, media_id)

    if now_playing["playing"]:
        try:
            _stop_plex_playback(client)
            await asyncio.sleep(0.8)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to stop current Plex playback before starting new media: {exc}",
            )

    command_timed_out = False
    try:
        command_timed_out = await _send_play_command(client, item)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send play command to Plex client '{PLEX_CLIENT_NAME}': {exc}",
        )

    return {
        "status": "ok",
        "action": "play",
        "playing": item.title,
        "type": item.type,
        "rating_key": item.ratingKey,
        "tv_powered_on": tv_powered_on,
        "plex_started": started,
        "command_timed_out": command_timed_out,
    }


def _run_play_media_job(media_id: Optional[int], random_movie_or_show: bool = False) -> None:
    try:
        asyncio.run(_play_plex_media(media_id, random_movie_or_show=random_movie_or_show))
    except Exception:
        logger.exception("Background Plex play command failed")


async def _advance_automation(now_playing: dict, last_seen_playing: bool) -> bool:
    if now_playing["playing"]:
        return True

    queue_item = _next_queue_item()
    timer_active = _timer_state()["active"]
    if not queue_item and not timer_active:
        return False

    if not last_seen_playing and not timer_active and not queue_item:
        return False

    async with _automation_lock:
        refreshed_now_playing = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _plex_now_playing(_plex_server()),
        )
        if refreshed_now_playing["playing"]:
            return True

        queue_item = _next_queue_item()
        if queue_item:
            await _play_plex_media(queue_item["media_id"])
            _delete_queue_item(queue_item["id"])
            return True

        if _timer_state()["active"]:
            await _play_plex_media(None, random_movie_or_show=True)
            return True

    return False


async def _automation_loop() -> None:
    last_seen_playing = False
    idle_since: Optional[float] = None
    while True:
        try:
            now_playing = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _plex_now_playing(_plex_server()),
            )
            if now_playing["playing"]:
                last_seen_playing = True
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since >= AUTOMATION_IDLE_GRACE_SECONDS:
                    last_seen_playing = await _advance_automation(
                        now_playing,
                        last_seen_playing,
                    )
                    idle_since = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Playback automation loop failed")
        await asyncio.sleep(AUTOMATION_POLL_INTERVAL)


# ── Status ────────────────────────────────────────────────────────────────────


@app.get(
    "/status",
    summary="System status",
    response_description="TV power, Tailscale, Sunshine, and Plex HTPC status",
)
async def get_status() -> dict:
    """
    Runs `scripts/status.sh` and returns its JSON output.
    Fields: `tv_status`, `tailscale_connected`, `sunshine_running`, `plex_htpc_running`.
    """
    script = os.path.join(SCRIPTS_DIR, "status.sh")
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["bash", script],
                capture_output=True,
                text=True,
                timeout=30,
            ),
        )
        # Take the last non-empty line — guards against stray output from
        # cec-client or other commands leaking to stdout before the JSON line.
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        if not lines:
            raise HTTPException(
                status_code=500,
                detail=f"Status script produced no output. stderr: {result.stderr.strip()}",
            )
        data = json.loads(lines[-1])
        _schedule_tv_power_refresh()
        data["tv_status"] = _tv_status or data.get("tv_status")
        data["tv_source"] = _tv_source
        data["tv_volume"] = _tv_volume
        return data
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Status script timed out")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Status script produced invalid JSON: {exc}. stdout: {result.stdout!r}. stderr: {result.stderr.strip()!r}",
        )


@app.get("/home-assistant/occupancy", summary="Chair and bed occupancy state")
async def home_assistant_occupancy() -> dict:
    """Return the latest states cached by the Home Assistant WebSocket client."""
    return _home_assistant_monitor.snapshot()


@app.post("/home-assistant/help", summary="Send a Home Assistant help notification")
async def home_assistant_help() -> dict:
    """Notify through Home Assistant when dashboard assistance is requested."""
    try:
        await _home_assistant_notifier.notify_help_requested()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConnectionError as exc:
        logger.warning("Could not send Home Assistant help notification: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "action": "help_notification_sent"}


@app.get("/api/logs", summary="Query persisted error logs")
async def get_error_logs(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    log_type: Optional[str] = Query(None, alias="type"),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    start_ts = _parse_local_datetime(start) if start else _now_ts() - (7 * 86400)
    end_ts = _parse_local_datetime(end) if end else _now_ts()
    if end_ts < start_ts:
        raise HTTPException(status_code=400, detail="End time must be after start time")

    date_where = "created_at >= ? AND created_at <= ?"
    date_params: list[object] = [start_ts, end_ts]
    where = date_where
    params = list(date_params)
    if log_type:
        where += " AND log_type = ?"
        params.append(log_type)

    with _db_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM error_logs WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM error_logs WHERE {where}",
            params,
        ).fetchone()["count"]
        count_rows = conn.execute(
            f"""
            SELECT log_type, COUNT(*) AS count
            FROM error_logs WHERE {date_where}
            GROUP BY log_type ORDER BY count DESC, log_type ASC
            """,
            date_params,
        ).fetchall()

    return {
        "logs": [
            {
                "id": row["id"],
                "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                "level": row["level"],
                "type": row["log_type"],
                "message": row["message"],
                "path": row["path"],
                "method": row["method"],
                "status_code": row["status_code"],
                "details": row["details"],
            }
            for row in rows
        ],
        "total": total,
        "counts": {row["log_type"]: row["count"] for row in count_rows},
    }


@app.post("/api/logs/client", status_code=204, summary="Persist a client-side error")
async def create_client_error_log(entry: ClientLogWrite):
    _write_error_log(
        message=entry.message,
        log_type="frontend",
        path=entry.path,
        method="CLIENT",
        details=entry.details,
    )
    return Response(status_code=204)


def _spa_response():
    if os.path.exists(FRONTEND_INDEX):
        # The HTML file points at hashed frontend assets. Never cache the HTML
        # itself so embedded browsers (notably Fully Kiosk) discover a newly
        # built asset bundle after a reload while retaining normal asset caching.
        return FileResponse(
            FRONTEND_INDEX,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Plex Remote</title>
            <style>
              body{margin:0;min-height:100vh;display:grid;place-items:center;background:#121212;color:#eee;font-family:system-ui,sans-serif}
              main{max-width:560px;padding:2rem;line-height:1.5}
              code{color:#e5a00d}
            </style>
          </head>
          <body>
            <main>
              <h1>Plex Remote</h1>
              <p>The React app has not been built yet. Run <code>npm install</code> and <code>npm run build</code> in <code>frontend/</code>, then restart FastAPI.</p>
            </main>
          </body>
        </html>
        """,
        status_code=503,
    )


@app.get("/", include_in_schema=False)
@app.get("/echo", include_in_schema=False)
@app.get("/settings", include_in_schema=False)
@app.get("/logs", include_in_schema=False)
async def spa_page():
    """Serve the built React SPA."""
    return _spa_response()


@app.on_event("startup")
async def app_startup() -> None:
    global _automation_task, _automation_lock
    _init_state_db()
    _automation_lock = asyncio.Lock()
    _automation_task = asyncio.create_task(_automation_loop())
    _home_assistant_monitor.start()


@app.on_event("shutdown")
async def app_shutdown() -> None:
    await _home_assistant_monitor.stop()
    if _automation_task is not None:
        _automation_task.cancel()
        try:
            await _automation_task
        except asyncio.CancelledError:
            pass
    _close_plex_server()


# ── Shared Playback State ────────────────────────────────────────────────────


@app.get("/playback/state", summary="Queue, timer, messages, and now-playing state")
async def playback_state():
    now_playing = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _plex_now_playing(_plex_server()),
    )
    return _playback_state(now_playing)


@app.post("/playback/timer", summary="Adjust or clear playback automation timer")
async def playback_timer(update: TimerUpdate):
    with _db_connection() as conn:
        if update.clear:
            _set_setting(conn, "timer_expires_at", None)
        else:
            delta = update.hours_delta or 0
            if delta != 0:
                current = _timer_state(conn)
                base = int(_get_setting(conn, "timer_expires_at") or 0)
                if not current["active"]:
                    base = _now_ts()
                expires_at = max(_now_ts(), base + (delta * 3600))
                if expires_at <= _now_ts():
                    _set_setting(conn, "timer_expires_at", None)
                else:
                    _set_setting(conn, "timer_expires_at", str(expires_at))
        return _timer_state(conn)


@app.get("/playback/queue", summary="List queued playback items")
async def playback_queue():
    return _queue_items()


@app.post("/playback/queue", summary="Add media to the shared playback queue")
async def playback_queue_add(item: QueueCreate):
    title = item.title
    media_type = item.media_type
    artwork_url = item.artwork_url
    if not title or not media_type or not artwork_url:
        def load_metadata():
            media = _plex_server().fetchItem(item.media_id)
            return _serialize_media_item(media)

        try:
            metadata = await asyncio.get_running_loop().run_in_executor(None, load_metadata)
            title = title or metadata.get("title")
            media_type = media_type or metadata.get("type")
            artwork_url = artwork_url or metadata.get("artwork_url")
        except Exception:
            title = title or f"Media {item.media_id}"

    with _db_connection() as conn:
        row = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM playback_queue").fetchone()
        conn.execute(
            """
            INSERT INTO playback_queue
                (position, media_id, title, media_type, artwork_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["next_position"],
                item.media_id,
                title,
                media_type,
                artwork_url,
                _now_ts(),
            ),
        )
        return {"status": "ok", "queue": _queue_items(conn)}


@app.delete("/playback/queue", summary="Clear queued playback items")
async def playback_queue_clear():
    with _db_connection() as conn:
        conn.execute("DELETE FROM playback_queue")
    return {"status": "ok", "queue": []}


@app.delete("/playback/queue/{item_id}", summary="Remove one queued playback item")
async def playback_queue_delete(item_id: int):
    _delete_queue_item(item_id)
    return {"status": "ok", "queue": _queue_items()}


@app.post("/playback/queue/{item_id}/play-now", summary="Play one queued item now")
async def playback_queue_play_now(item_id: int):
    with _db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM playback_queue WHERE id = ?",
            (item_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Queued item not found")
    _delete_queue_item(item_id)
    asyncio.get_running_loop().run_in_executor(
        None,
        _run_play_media_job,
        row["media_id"],
        False,
    )
    return {"status": "accepted", "action": "play", "queue": _queue_items()}


@app.post("/playback/queue/reorder", summary="Reorder queued playback items")
async def playback_queue_reorder(ordering: QueueReorder):
    with _db_connection() as conn:
        rows = conn.execute("SELECT id FROM playback_queue").fetchall()
        existing = {row["id"] for row in rows}
        requested = [item_id for item_id in ordering.ids if item_id in existing]
        missing = [item_id for item_id in existing if item_id not in requested]
        for index, item_id in enumerate([*requested, *missing], start=1):
            conn.execute(
                "UPDATE playback_queue SET position = ? WHERE id = ?",
                (index, item_id),
            )
        return {"status": "ok", "queue": _queue_items(conn)}


@app.get("/messages", summary="List scheduled nurse messages")
async def messages_list(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return _spa_response()
    with _db_connection() as conn:
        rows = conn.execute("SELECT * FROM messages ORDER BY starts_at DESC, id DESC").fetchall()
        return [_message_row(row) for row in rows]


@app.post("/messages", summary="Create a scheduled nurse message")
async def messages_create(message: MessageWrite):
    starts_at = _parse_local_datetime(message.starts_at)
    ends_at = _parse_local_datetime(message.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="End time must be after start time")
    now = _now_ts()
    with _db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (text, starts_at, ends_at, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message.text.strip(), starts_at, ends_at, int(message.enabled), now, now),
        )
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _message_row(row)


@app.put("/messages/{message_id}", summary="Update a scheduled nurse message")
async def messages_update(message_id: int, message: MessageWrite):
    starts_at = _parse_local_datetime(message.starts_at)
    ends_at = _parse_local_datetime(message.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="End time must be after start time")
    with _db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE messages
            SET text = ?, starts_at = ?, ends_at = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                message.text.strip(),
                starts_at,
                ends_at,
                int(message.enabled),
                _now_ts(),
                message_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Message not found")
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message_row(row)


@app.delete("/messages/{message_id}", summary="Delete a scheduled nurse message")
async def messages_delete(message_id: int):
    with _db_connection() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    return {"status": "ok"}


# ── TV Control ────────────────────────────────────────────────────────────────


@app.post("/tv/power/{state}", summary="Turn TV on or off")
async def tv_power(state: str):
    """
    `state=on` sends CEC *on 0*; `state=off` sends *standby 0*.
    """
    if state not in ("on", "off"):
        raise HTTPException(status_code=400, detail="state must be 'on' or 'off'")
    global _tv_status
    _cec("on 0" if state == "on" else "standby 0")
    _tv_status = state
    return {"status": "ok", "state": state}


@app.post("/tv/mute", summary="Toggle TV mute via CEC")
async def tv_mute():
    _cec("mute")
    return {"status": "ok", "action": "mute_toggle"}


@app.post("/tv/source/{port}", summary="Switch TV HDMI input via CEC")
async def tv_source(port: int):
    """
    Switches the TV to HDMI `port` (1–9) via CEC Set Stream Path.
    Plex no longer holds the CEC adapter (flatpak device override), so the
    switch can happen at any time without stopping Plex first.
    """
    if not 1 <= port <= 9:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 9")

    global _tv_source
    # Send Set Stream Path + raw opcode in a single cec-client session
    phys_addr = f"{port * 16:02X}:00"  # e.g. port 3 → 30:00
    subprocess.run(
        f"flock -w 15 {_CEC_LOCK} cec-client -s -d 1",
        input=f"sp {port}.0.0.0\ntx 0F:86:{phys_addr}\n",
        shell=True, capture_output=True, text=True, timeout=20,
    )
    _tv_source = port
    return {
        "status": "ok",
        "action": "source_set",
        "port": port,
    }


@app.get("/tv/volume", summary="Get current TV volume")
async def get_tv_volume():
    """Returns the last-known TV volume level (0–100), or `null` if unknown."""
    return {"level": _tv_volume}


@app.post("/tv/volume/up", summary="Increase TV volume by 5 steps")
async def tv_volume_up():
    global _tv_volume
    _cec_adjust_volume(5)
    if _tv_volume is not None:
        _tv_volume = min(100, _tv_volume + 5)
    return {"status": "ok", "action": "volume_up", "level": _tv_volume}


@app.post("/tv/volume/down", summary="Decrease TV volume by 5 steps")
async def tv_volume_down():
    global _tv_volume
    _cec_adjust_volume(-5)
    if _tv_volume is not None:
        _tv_volume = max(0, _tv_volume - 5)
    return {"status": "ok", "action": "volume_down", "level": _tv_volume}


@app.post("/tv/volume/{level}", summary="Set TV volume via CEC")
async def set_tv_volume(level: int):
    """
    Adjusts TV volume to `level` (0–100) via CEC volup/voldown commands.
    Reads the current level first, calculates the delta, then sends all
    step commands in a single cec-client session to avoid repeated init delays.
    """
    global _tv_volume
    if not 0 <= level <= 100:
        raise HTTPException(status_code=400, detail="Volume level must be between 0 and 100")
    current = _tv_volume
    if current is None:
        raise HTTPException(
            status_code=409,
            detail="Current volume is unknown — use /tv/volume/up or /tv/volume/down to adjust first.",
        )
    steps = level - current
    _cec_adjust_volume(steps)
    _tv_volume = level
    return {
        "status": "ok",
        "action": "volume_set",
        "previous": current,
        "level": level,
        "steps": steps,
    }


# ── Tailscale Control ────────────────────────────────────────────────────────


@app.post("/tailscale/start", summary="Start Tailscale service")
async def tailscale_start():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["systemctl", "start", "tailscaled"],
            capture_output=True,
            text=True,
            timeout=10,
        ),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Failed to start Tailscale"
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "action": "start"}


@app.post("/tailscale/stop", summary="Stop Tailscale service")
async def tailscale_stop():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["systemctl", "stop", "tailscaled"],
            capture_output=True,
            text=True,
            timeout=10,
        ),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Failed to stop Tailscale"
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "action": "stop"}


# ── Sunshine Control ─────────────────────────────────────────────────────────


@app.post("/sunshine/start", summary="Start Sunshine streaming service")
async def sunshine_start():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            "systemctl --user start sunshine",
            shell=True,
            capture_output=True,
            env=_SYSTEMD_USER_ENV,
        ),
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail="Failed to start Sunshine")
    return {"status": "ok", "action": "start"}


@app.post("/sunshine/stop", summary="Stop Sunshine streaming service")
async def sunshine_stop():
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            "systemctl --user stop sunshine",
            shell=True,
            capture_output=True,
            env=_SYSTEMD_USER_ENV,
        ),
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail="Failed to stop Sunshine")
    return {"status": "ok", "action": "stop"}


# ── Plex Playback ─────────────────────────────────────────────────────────────


@app.post("/plex/play", summary="Start Plex playback")
async def plex_play(
    media_id: Optional[int] = Query(
        None,
        description=(
            "Plex rating key. "
            "Movie → plays it. "
            "Show or Season → random episode. "
            "Episode → plays it. "
            "Omit → random active movie from CURATED_MOVIES."
        ),
    ),
):
    """
    Queue playback work and return immediately so the dashboard stays responsive.
    """
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_play_media_job, media_id)
    return {"status": "accepted", "action": "play"}


@app.post("/plex/smart-play", summary="Turn on TV, start Plex, play or toggle movie")
async def plex_smart_play(
    media_id: Optional[int] = Query(
        None,
        description=(
            "Plex rating key. Omit to use a random active movie from CURATED_MOVIES. "
            "If a movie is already playing or paused, this toggles pause/resume."
        ),
    ),
):
    """
    Ensures the TV is on and Plex HTPC is reachable. If a movie is already
    active, toggles pause/resume; otherwise starts a selected movie.
    """
    tv_powered_on = await _ensure_tv_on()
    started = _ensure_plex_htpc_running()
    plex = _plex_server()

    if started:
        await _wait_for_plex_client(plex, PLEX_CLIENT_STARTUP_TIMEOUT)

    client = _plex_client(plex)
    now_playing = _plex_now_playing(plex)
    state = str(now_playing["state"] or "").lower()

    if now_playing["media_type"] == "movie" and state in ("playing", "paused"):
        action = "resume" if state == "paused" else "pause"
        try:
            if state == "paused":
                client.play()
            else:
                client.pause()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Failed to send {action} command to Plex client "
                    f"'{PLEX_CLIENT_NAME}': {exc}"
                ),
            )
        return {
            "status": "ok",
            "action": action,
            "tv_powered_on": tv_powered_on,
            "plex_started": started,
            "rating_key": now_playing["rating_key"],
            "playing": now_playing["display_title"],
        }

    result = await _play_plex_media(
        media_id,
        startup_timeout=PLEX_CLIENT_STARTUP_TIMEOUT,
        ensure_tv=False,
    )
    result["tv_powered_on"] = tv_powered_on
    result["plex_started"] = started or result["plex_started"]
    return result


@app.get("/plex/clients", summary="List available Plex clients")
async def plex_clients():
    """Returns all Plex clients currently visible to the server."""
    plex = _plex_server()
    clients = plex.clients()
    return [
        {"name": c.title, "product": c.product, "device": c.device} for c in clients
    ]


@app.get("/plex/now-playing", summary="Get media playing on Plex HTPC")
async def plex_now_playing():
    """Return movie/episode metadata and poster artwork as separate fields."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: _plex_now_playing(_plex_server()),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Cannot read Plex sessions: {exc}"
        )


@app.get("/plex/media/movies", summary="List curated Plex movies")
async def plex_media_movies():
    """Return the curated movie list configured by CURATED_MOVIES."""
    if not CURATED_MOVIES:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _curated_media(_plex_server(), CURATED_MOVIES),
    )


@app.get("/plex/media/shows", summary="List curated Plex TV shows")
async def plex_media_shows():
    """Return the curated TV show list configured by CURATED_SHOWS."""
    if not CURATED_SHOWS:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _curated_media(_plex_server(), CURATED_SHOWS),
    )


@app.get("/plex/media/shows/{show_rating_key}/seasons", summary="List curated show seasons")
async def plex_media_show_seasons(show_rating_key: int):
    """Return seasons for a curated show, applying its configured season filter."""
    loop = asyncio.get_running_loop()

    def load():
        show = _plex_server().fetchItem(show_rating_key)
        return [_serialize_season(season) for season in _curated_show_seasons(show)]

    return await loop.run_in_executor(None, load)


@app.get(
    "/plex/media/shows/{show_rating_key}/seasons/{season_rating_key}/episodes",
    summary="List season episodes",
)
async def plex_media_season_episodes(show_rating_key: int, season_rating_key: int):
    """Return episodes for one curated show season."""
    loop = asyncio.get_running_loop()

    def load():
        season = _plex_server().fetchItem(season_rating_key)
        return [_serialize_episode(episode) for episode in season.episodes()]

    return await loop.run_in_executor(None, load)


@app.get(
    "/plex/artwork",
    response_class=Response,
    include_in_schema=False,
)
async def plex_artwork(path: str = Query(...), rating_key: Optional[str] = Query(None)):
    """Proxy Plex artwork so it works when the dashboard is viewed remotely."""
    if not path.startswith("/") or "://" in path:
        raise HTTPException(status_code=400, detail="Invalid Plex artwork path")
    cached = _read_cached_artwork(path, rating_key)
    if cached is not None:
        return cached
    plex = _plex_server()
    try:
        result = plex._session.get(
            plex.url(path, includeToken=True),
            timeout=10,
        )
        result.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot load artwork: {exc}")
    content_type = result.headers.get("content-type", "image/jpeg")
    optimized = _write_cached_artwork(path, rating_key, result.content, content_type)
    return Response(
        content=optimized,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/plex/start", summary="Start Plex HTPC")
async def plex_start():
    started = _ensure_plex_htpc_running()
    return {"status": "ok", "action": "start", "started": started}


@app.post("/plex/terminate", summary="Stop Plex HTPC")
async def plex_terminate():
    subprocess.run(
        "pkill -9 -f 'plex-bin'",
        shell=True,
        capture_output=True,
    )
    subprocess.run(
        "systemctl --user stop plex-htpc",
        shell=True,
        capture_output=True,
        env=_SYSTEMD_USER_ENV,
    )
    return {"status": "ok", "action": "stop"}


@app.post("/plex/stop", summary="Stop Plex playback")
async def plex_stop():
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _stop_plex_playback(_plex_client(_plex_server())),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send stop command to Plex client '{PLEX_CLIENT_NAME}': {exc}",
        )
    return {"status": "ok", "action": "stop"}


@app.post("/plex/pause", summary="Pause Plex playback")
async def plex_pause():
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _plex_client(_plex_server()).pause(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send pause command to Plex client '{PLEX_CLIENT_NAME}': {exc}",
        )
    return {"status": "ok", "action": "pause"}


@app.post("/plex/resume", summary="Resume Plex playback")
async def plex_resume():
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _plex_client(_plex_server()).play(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to send resume command to Plex client '{PLEX_CLIENT_NAME}': {exc}",
        )
    return {"status": "ok", "action": "resume"}


@app.post("/plex/seek", summary="Seek Plex playback to a percentage of its duration")
async def plex_seek(update: SeekUpdate):
    """Convert a playback percentage to milliseconds and seek the active video."""
    loop = asyncio.get_running_loop()

    def seek():
        plex = _plex_server()
        now_playing = _plex_now_playing(plex)
        duration = now_playing["duration"]
        if not now_playing["playing"] or not duration:
            raise HTTPException(status_code=409, detail="No seekable media is playing")

        offset = round(duration * update.percent / 100)
        offset = max(0, min(duration, offset))
        _plex_client(plex).seekTo(offset, mtype="video")
        return {
            "status": "ok",
            "action": "seek",
            "percent": update.percent,
            "offset": offset,
            "duration": duration,
        }

    try:
        return await loop.run_in_executor(None, seek)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to seek Plex client '{PLEX_CLIENT_NAME}': {exc}",
        ) from exc

@app.get("/debug/config")
async def debug_config():
    return {
        "PLEX_URL": PLEX_URL,
        "PLEX_TIMEOUT": PLEX_TIMEOUT,
        "PLEX_CLIENT_NAME": PLEX_CLIENT_NAME,
        "PLEX_CLIENT_URL": PLEX_CLIENT_URL or None,
        "PLEX_CLIENT_PROXY_THROUGH_SERVER": PLEX_CLIENT_PROXY_THROUGH_SERVER,
        "CURATED_MOVIES": CURATED_MOVIES,
        "CURATED_SHOWS": CURATED_SHOWS,
    }

# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        # Polling dashboards can leave requests waiting on Plex network
        # timeouts. Do not let those requests hold up a service restart.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
