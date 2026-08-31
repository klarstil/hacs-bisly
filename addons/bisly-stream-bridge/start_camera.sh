#!/usr/bin/env bash
# Called by mediamtx runOnDemand with the requested path as $1.
# Triggers the bridge start via the control API.
set -euo pipefail
curl -sS -X POST --max-time 30 "http://127.0.0.1:4599/api/start/$1" || true
