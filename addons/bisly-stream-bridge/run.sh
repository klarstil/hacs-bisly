#!/usr/bin/env bash
set -euo pipefail

CONFIG="/data/options.json"

export RTSP_PORT
RTSP_PORT="$(jq -r '.rtsp_port // 8554' "$CONFIG")"

export BISLY_USERNAME
export BISLY_PASSWORD
BISLY_USERNAME="$(jq -r '.bisly_username // empty' "$CONFIG")"
BISLY_PASSWORD="$(jq -r '.bisly_password // empty' "$CONFIG")"

export VIDEO_WIDTH
export VIDEO_FPS
export FFMPEG_CPU_PRESET
export LOG_LEVEL
VIDEO_WIDTH="$(jq -r '.video_width // 1280' "$CONFIG")"
VIDEO_FPS="$(jq -r '.video_fps // 15' "$CONFIG")"
FFMPEG_CPU_PRESET="$(jq -r '.ffmpeg_cpu_preset // "veryfast"' "$CONFIG")"
LOG_LEVEL="$(jq -r '.log_level // "INFO"' "$CONFIG")"

if [[ -z "$BISLY_USERNAME" || -z "$BISLY_PASSWORD" ]]; then
    echo "Error: bisly_username and bisly_password must be set in the add-on configuration." >&2
    exit 1
fi

if ! mountpoint -q /data; then
    mkdir -p /data
fi

sed -e "s/{{RTSP_PORT}}/${RTSP_PORT}/" /opt/mediamtx.yml >/data/mediamtx.yml

/opt/mediamtx/mediamtx /data/mediamtx.yml &

MEDIAMTX_PID=$!
trap 'kill "$MEDIAMTX_PID" 2>/dev/null || true' EXIT

exec python3 -m bridge.main
