#!/usr/bin/env bash
# Bekleyen takip isteklerini geri çeker.
cd "$(dirname "$0")"
exec .venv/bin/python withdraw_requests.py "$@"
