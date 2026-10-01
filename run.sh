#!/usr/bin/env bash
# Playwright'ı proje venv'i ile çalıştırır.
cd "$(dirname "$0")"
exec .venv/bin/python ig_export.py "$@"
