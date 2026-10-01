#!/usr/bin/env bash
# Gönderilen takip isteklerinden kaçının kabul edildiğini ölçer.
cd "$(dirname "$0")"
exec .venv/bin/python kabul_kontrol.py "$@"
