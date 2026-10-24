#!/bin/bash
# Resumable download of Qwen3.6-35B-A3B APEX I-Mini (+ vision projector). Re-run to continue.
cd "$(dirname "$0")/models"
U=https://huggingface.co/mudler/Qwen3.6-35B-A3B-APEX-GGUF/resolve/main
for f in Qwen3.6-35B-A3B-APEX-I-Mini.gguf mmproj.gguf; do
  [ -s "$f" ] && { echo "$(date +%T) have $f"; continue; }
  for t in 1 2 3 4 5 6 7 8; do
    curl -sL --fail -C - -o "$f.part" "$U/$f" && mv "$f.part" "$f" && { echo "$(date +%T) got $f"; break; }
    echo "$(date +%T) retry $t $f"; sleep 15
  done
done
echo "$(date +%T) ALL DONE"
