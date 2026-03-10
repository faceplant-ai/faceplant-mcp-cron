#!/bin/bash
set -e

DATA_DIR="/data"
mkdir -p "$DATA_DIR/cron" "$DATA_DIR/logs"
chown -R cron:cron "$DATA_DIR"

# Restore persisted crontab if it exists
if [ -f "$DATA_DIR/crontab" ]; then
    crontab "$DATA_DIR/crontab"
    echo "entrypoint: restored crontab from /data/crontab"
fi

# Start cron daemon
cron
echo "entrypoint: cron daemon started"

echo "entrypoint: faceplant-mcp-cron ready ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
exec uv run uvicorn src.main:app --host 0.0.0.0 --port 8000
