#!/usr/bin/env bash
# Listedeki herkese takip isteği atar.
cd "$(dirname "$0")"
exec .venv/bin/python istek_at.py "$@"
