"""
plex-remote — local media server automation API
Designed for Home Assistant REST calls on a private LAN.
"""

import asyncio
import json
import os
import random
import subprocess
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from plexapi.server import PlexServer

load_dotenv()

# ── Config (set these in .env) ────────────────────────────────────────────────
PLEX_URL = os.getenv("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PLEX_CLIENT_NAME = os.getenv("PLEX_CLIENT_NAME", "")
PLEX_HTPC_BINARY = os.getenv("PLEX_HTPC_BINARY", "plex-htpc")
# Comma-separated Plex rating keys (integers) for the curated movie list
CURATED_MOVIE_IDS = [
    x.strip() for x in os.getenv("CURATED_MOVIE_IDS", "").split(",") if x.strip()
]

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")

app = FastAPI(
    title="Plex Remote",
    description="Local media-server automation API. All endpoints are JSON.",
    version="0.1.0",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cec(command: str, timeout: int = 10) -> None:
    """Send a single cec-client command via stdin."""
    subprocess.run(
        f'echo "{command}" | cec-client -s -d 1',
        shell=True,
        capture_output=True,
        timeout=timeout,
    )


def _plex_server() -> PlexServer:
    if not PLEX_TOKEN:
        raise HTTPException(status_code=503, detail="PLEX_TOKEN not configured")
    try:
        return PlexServer(PLEX_URL, PLEX_TOKEN)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Cannot connect to Plex server: {exc}")


def _plex_client(plex: PlexServer):
    if not PLEX_CLIENT_NAME:
        raise HTTPException(status_code=503, detail="PLEX_CLIENT_NAME not configured")
    try:
        return plex.client(PLEX_CLIENT_NAME)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Plex client '{PLEX_CLIENT_NAME}' not reachable: {exc}",
        )


def _ensure_plex_htpc_running() -> None:
    """Launch Plex HTPC if it is not already running."""
    check = subprocess.run(
        "ps aux | grep -i 'plex-htpc' | grep -qv grep;",
        shell=True,
        capture_output=True,
    )
    if check.returncode == 0:
        return  # already running

    # Try systemd service first, then fall back to binary
    svc = subprocess.run(
        "systemctl start plex-htpc",
        shell=True,
        capture_output=True,
    )
    if svc.returncode != 0:
        subprocess.Popen(
            [PLEX_HTPC_BINARY],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


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
        result = subprocess.run(
            ["bash", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        # Take the last non-empty line — guards against stray output from
        # cec-client or other commands leaking to stdout before the JSON line.
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        if not lines:
            raise HTTPException(
                status_code=500,
                detail=f"Status script produced no output. stderr: {result.stderr.strip()}",
            )
        return json.loads(lines[-1])
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Status script timed out")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Status script produced invalid JSON: {exc}. stdout: {result.stdout!r}. stderr: {result.stderr.strip()!r}",
        )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview_page() -> str:
    """Simple auto-refreshing status dashboard."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Plex Remote</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:system-ui,sans-serif;background:#111;color:#eee;
         display:flex;flex-direction:column;align-items:center;padding:2rem}
    h1{color:#e5a00d;margin-bottom:1.5rem;font-size:1.8rem}
    .grid{display:grid;gap:.75rem;width:100%;max-width:480px}
    .card{display:flex;justify-content:space-between;align-items:center;
          background:#1e1e1e;border-radius:8px;padding:.75rem 1.25rem}
    .label{font-size:.95rem;color:#aaa}
    .on {color:#4caf50;font-weight:600}
    .off{color:#f44336;font-weight:600}
    .unknown{color:#888;font-weight:600}
    .ts{font-size:.75rem;color:#555;margin-top:1.5rem}
  </style>
</head>
<body>
  <h1>Plex Remote</h1>
  <div class="grid" id="grid"><div class="card"><span class="label">Loading…</span></div></div>
  <p class="ts" id="ts"></p>
  <script>
    const ROWS = [
      ['tv_status',        'TV Power',     v => (v === 'on' ? 'on' : v === 'off' ? 'off' : 'unknown'), v => v],
      ['tailscale_connected','Tailscale',  v => (v ? 'on' : 'off'), v => (v ? 'Connected' : 'Disconnected')],
      ['sunshine_running', 'Sunshine',     v => (v ? 'on' : 'off'), v => (v ? 'Running' : 'Stopped')],
      ['plex_htpc_running','Plex HTPC',    v => (v ? 'on' : 'off'), v => (v ? 'Running' : 'Stopped')],
    ];
    async function load() {
      try {
        const d = await (await fetch('/status')).json();
        document.getElementById('grid').innerHTML = ROWS.map(([key, label, cls, fmt]) =>
          `<div class="card"><span class="label">${label}</span>
           <span class="${cls(d[key])}">${fmt(d[key])}</span></div>`
        ).join('');
        document.getElementById('ts').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
      } catch(e) {
        document.getElementById('grid').innerHTML = '<div class="card"><span class="label">Error fetching status</span></div>';
      }
    }
    load();
  </script>
</body>
</html>"""


# ── TV Control ────────────────────────────────────────────────────────────────

@app.post("/tv/power", summary="Turn TV on or off")
async def tv_power(state: str = Query(..., pattern="^(on|off)$")):
    """
    `state=on` sends CEC *on 0*; `state=off` sends *standby 0*.
    """
    _cec("on 0" if state == "on" else "standby 0")
    return {"status": "ok", "state": state}


@app.post("/tv/mute", summary="Toggle TV mute via CEC")
async def tv_mute():
    _cec("mute")
    return {"status": "ok", "action": "mute_toggle"}


# ── Plex Playback ─────────────────────────────────────────────────────────────

@app.post("/plex/play", summary="Start Plex playback")
async def plex_play(
    media_id: Optional[int] = Query(
        None,
        description=(
            "Plex rating key. "
            "Movie → plays it. "
            "Show or Season → random episode. "
            "Omit → random movie from CURATED_MOVIE_IDS."
        ),
    )
):
    """
    Ensures Plex HTPC is running, then starts playback via the Plex API.
    """
    _ensure_plex_htpc_running()
    # Allow HTPC time to initialise and register as a client if it just launched
    await asyncio.sleep(3)

    plex = _plex_server()

    if media_id is not None:
        try:
            item = plex.fetchItem(media_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Media {media_id} not found: {exc}")

        if item.type in ("show", "season"):
            episodes = item.episodes()
            if not episodes:
                raise HTTPException(status_code=404, detail="No episodes found for that item")
            item = random.choice(episodes)
    else:
        if not CURATED_MOVIE_IDS:
            raise HTTPException(
                status_code=400,
                detail="No curated list configured — set CURATED_MOVIE_IDS in .env",
            )
        rating_key = int(random.choice(CURATED_MOVIE_IDS))
        try:
            item = plex.fetchItem(rating_key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Curated item {rating_key} not found: {exc}")

    client = _plex_client(plex)
    client.playMedia(item)
    return {"status": "ok", "playing": item.title, "type": item.type, "rating_key": item.ratingKey}


@app.get("/plex/clients", summary="List available Plex clients")
async def plex_clients():
    """Returns all Plex clients currently visible to the server."""
    plex = _plex_server()
    clients = plex.clients()
    return [{"name": c.title, "product": c.product, "device": c.device} for c in clients]


@app.post("/plex/pause", summary="Pause Plex playback")
async def plex_pause():
    plex = _plex_server()
    _plex_client(plex).pause()
    return {"status": "ok", "action": "pause"}


@app.post("/plex/resume", summary="Resume Plex playback")
async def plex_resume():
    plex = _plex_server()
    _plex_client(plex).play()
    return {"status": "ok", "action": "resume"}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()

