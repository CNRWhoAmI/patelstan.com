#!/usr/bin/env bash
# Günlük tur: süresi dolan istekleri geri çeker, sıradakilere istek atar, istatistiği yazar.
cd "$(dirname "$0")"
exec .venv/bin/python gunluk.py "$@"
