#!/bin/bash
# Resumable download of HauhauCS Qwen3.6-35B-A3B Uncensored Aggressive IQ3_M. Re-run to continue.
cd "$(dirname "$0")/models"
U=https://huggingface.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive/resolve/main
f=Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_M.gguf
[ -s "$f" ] && { echo "$(date +%T) have $f"; exit 0; }
for t in 1 2 3 4 5 6 7 8; do
  curl -sL --fail -C - -o "$f.part" "$U/$f" && mv "$f.part" "$f" && { echo "$(date +%T) got $f"; break; }
  echo "$(date +%T) retry $t"; sleep 15
done
echo "$(date +%T) ALL DONE"
