---
name: pc-slow
description: Find out why the PC is slow, laggy, stuttering, overheating, or the fans are loud
triggers:
  - why is my pc so slow
  - my computer is lagging
  - everything is stuttering
  - the fans are really loud
  - what is eating my cpu
  - my laptop is overheating
  - games are running badly today
---
Measure, don't guess. One system_status call gives RAM, VRAM, disk and loaded models; then
run_command for the rest:
1. CPU hogs: `ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -12`; load: `uptime`
   (16 threads, so load above ~12 is saturated).
2. Memory: `free -h` — little "available" plus several GB of swap used means it is
   swapping, which feels like everything freezing. RAM hogs: `ps -eo pid,pmem,rss,comm --sort=-pmem | head -10`.
3. Heat: `sensors` (Tctl is the CPU; above ~90 °C it throttles) and
   `nvidia-smi --query-gpu=utilization.gpu,memory.used,temperature.gpu --format=csv`.
4. Disk: `df -h /` (nearly full = slow), and `vmstat 1 5` (high `wa` = waiting on disk).
5. Errors: `journalctl -p err -b --no-pager -n 30`.
Johan's own stack can be the cause: ollama / llama-server (the model), python3 embed_server,
ComfyUI, and docker containers (immich_* can be heavy). Say so plainly if it's Johan.
For games, `johan-pause` (GameMode does it automatically) stops all of Johan's background work.
Fix: name the culprit with numbers. To close something, propose_action `pkill NAME` (he
approves in F4); never kill gnome-shell, Xorg or his terminal.
