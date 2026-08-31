#!/usr/bin/env bash
# Called by mediamtx runOnUnDemand with the requested path as $1.
# Triggers the bridge stop via the control API.
set -euo pipefail
curl -sS -X POST --max-time 10 "http://127.0.0.1:4599/api/stop/$1" || true
