#!/bin/bash
# Nightly snapshot of the boards and images; keeps the last 14. Run by launchd on the mini.
# Dropped files (videos and the like) are big and never change, so they are copied once into files/ instead of 14 times.
set -euo pipefail
DATA="$HOME/context-canvas-data"
OUT="$HOME/context-canvas-backups"
mkdir -p "$OUT/files"
tar -czf "$OUT/context-canvas-$(date +%Y-%m-%d).tgz" --exclude "$(basename "$DATA")/files" --exclude "$(basename "$DATA")/uploads" -C "$(dirname "$DATA")" "$(basename "$DATA")"
if [ -d "$DATA/files" ]; then rsync -a "$DATA/files/" "$OUT/files/"; fi
ls -1t "$OUT"/context-canvas-*.tgz | tail -n +15 | while read -r f; do rm -f "$f"; done
