#!/bin/bash
# Resumable download of Groot-19B-A3B (APEX I-MiniPlus, 8.4 GB). Re-run to continue.
cd "$(dirname "$0")/models"
U=https://huggingface.co/IsValorum/Groot-19B-A3B-261023-APEX-I-MiniPlus-V2.1-GGUF/resolve/main
f=Groot-19B-A3B-261023.APEX-I-MiniPlus-V2.1.gguf
[ -s "$f" ] && { echo "$(date +%T) have $f"; exit 0; }
for t in 1 2 3 4 5 6 7 8; do
  curl -sL --fail -C - -o "$f.part" "$U/$f" && mv "$f.part" "$f" && { echo "$(date +%T) got $f"; break; }
  echo "$(date +%T) retry $t"; sleep 15
done
echo "$(date +%T) ALL DONE"
