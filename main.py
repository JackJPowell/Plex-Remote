"""
plex-remote — local media server automation API
Designed for Home Assistant REST calls on a private LAN.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from plexapi.server import PlexServer

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
    if not PLEX_TOKEN:
        raise HTTPException(status_code=503, detail="PLEX_TOKEN not configured")
    try:
        return PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Cannot connect to Plex server: {exc}"
        )


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
    url = f"/plex/artwork?path={quote(artwork_path, safe='')}"
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
    media_type = "image/jpeg"
    try:
        meta = json.loads(meta_path.read_text())
        media_type = meta.get("content_type") or media_type
    except Exception:
        pass
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
) -> None:
    image_path, meta_path = _artwork_cache_paths(path, rating_key)
    ARTWORK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(content)
    meta_path.write_text(json.dumps({"path": path, "content_type": content_type}))


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
    """Turn the TV on if CEC reports it is off or unknown."""
    current = await _query_tv_power()
    if current == "on":
        return False

    _cec("on 0")
    for _ in range(6):
        await asyncio.sleep(1)
        if await _query_tv_power() == "on":
            return True
    return True


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


async def _play_plex_media(
    media_id: Optional[int],
    startup_timeout: int = PLEX_CLIENT_STARTUP_TIMEOUT,
) -> dict:
    """Ensure HTPC is reachable, pick media, stop current playback, and play it."""
    started = _ensure_plex_htpc_running()
    plex = _plex_server()

    if started:
        await _wait_for_plex_client(plex, startup_timeout)

    client = _plex_client(plex)
    now_playing = _plex_now_playing(plex)
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
        "plex_started": started,
        "command_timed_out": command_timed_out,
    }


def _run_play_media_job(media_id: Optional[int]) -> None:
    try:
        asyncio.run(_play_plex_media(media_id))
    except Exception:
        logger.exception("Background Plex play command failed")


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


def _spa_response():
    if os.path.exists(FRONTEND_INDEX):
        return FileResponse(FRONTEND_INDEX)
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
async def spa_page():
    """Serve the built React SPA."""
    return _spa_response()


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

    result = await _play_plex_media(media_id, startup_timeout=PLEX_CLIENT_STARTUP_TIMEOUT)
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
    _write_cached_artwork(path, rating_key, result.content, content_type)
    return Response(
        content=result.content,
        media_type=content_type,
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

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
