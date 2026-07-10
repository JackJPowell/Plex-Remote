#!/usr/bin/env bash
# Outputs a single JSON line with system status for plex-remote.
# Fields: tv_status, tailscale_connected, sunshine_running, plex_htpc_running

tv_status="unknown"
tailscale_connected=false
sunshine_running=false
plex_htpc_running=false

# tv_status is managed by the Python API via CEC background refresh.

# ── Tailscale ─────────────────────────────────────────────────────────────────
if timeout 1s systemctl is-active --quiet tailscaled 2>/dev/null; then
    tailscale_connected=true
elif timeout 1s tailscale status --peers=false &>/dev/null; then
    tailscale_connected=true
fi

# ── Sunshine ──────────────────────────────────────────────────────────────────
if timeout 1s systemctl --user is-active --quiet sunshine 2>/dev/null; then
    sunshine_running=true
elif timeout 1s pgrep -x sunshine &>/dev/null; then
    sunshine_running=true
fi

# ── Plex HTPC ─────────────────────────────────────────────────────────────────
if timeout 1s pgrep -f "plex-bin|plex-htpc" &>/dev/null; then
    plex_htpc_running=true
fi

printf '{"tv_status":"%s","tailscale_connected":%s,"sunshine_running":%s,"plex_htpc_running":%s}\n' \
    "$tv_status" "$tailscale_connected" "$sunshine_running" "$plex_htpc_running"
