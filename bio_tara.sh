#!/usr/bin/env bash
# Listedeki profillerin bio'sunu okur, şehre göre ayırır.
cd "$(dirname "$0")"
exec .venv/bin/python bio_tara.py "$@"
