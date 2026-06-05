#!/usr/bin/env bash
# Outputs a single JSON line with system status for plex-remote.
# Fields: tv_status, tailscale_connected, sunshine_running, plex_htpc_running

tv_status="unknown"
tailscale_connected=false
sunshine_running=false
plex_htpc_running=false

# ── TV power state via CEC ────────────────────────────────────────────────────
if command -v cec-client &>/dev/null; then
    cec_out=$(echo "pow 0" | timeout 8 cec-client -s -d 1 2>/dev/null)
    if echo "$cec_out" | grep -qi "power status: on"; then
        tv_status="on"
    elif echo "$cec_out" | grep -qi "power status: standby\|power status: off"; then
        tv_status="off"
    elif echo "$cec_out" | grep -qi "power status:"; then
        # Capture whatever the status string says
        tv_status=$(echo "$cec_out" | grep -i "power status:" \
                    | awk -F': ' '{print $2}' | tr -d '[:space:]')
    fi
fi

# ── Tailscale ─────────────────────────────────────────────────────────────────
if command -v tailscale &>/dev/null; then
    ts_out=$(tailscale status 2>/dev/null | head -1)
    if [ -n "$ts_out" ] && ! echo "$ts_out" | grep -qi "stopped\|not connected\|failed\|error"; then
        tailscale_connected=true
    fi
fi

# ── Sunshine ──────────────────────────────────────────────────────────────────
if systemctl is-active --quiet sunshine 2>/dev/null; then
    sunshine_running=true
elif pgrep -x sunshine &>/dev/null; then
    sunshine_running=true
fi

# ── Plex HTPC ─────────────────────────────────────────────────────────────────
if ps aux | grep -i "plex-htpc" | grep -qv grep; then
    plex_htpc_running=true
fi

printf '{"tv_status":"%s","tailscale_connected":%s,"sunshine_running":%s,"plex_htpc_running":%s}\n' \
    "$tv_status" "$tailscale_connected" "$sunshine_running" "$plex_htpc_running"
