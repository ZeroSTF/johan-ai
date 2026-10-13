---
name: free-disk-space
description: Free up disk space safely — find what is taking space and clean it without breaking anything
triggers:
  - my disk is almost full
  - free up some space
  - what is taking up all my storage
  - clean up my drive
  - I'm running out of disk space
  - how much space do I have left
---
1. How bad: `df -h /` (and `df -h` for other drives).
2. What is big, one level at a time:
   `du -h -d1 /home/zeros 2>/dev/null | sort -h | tail -12`, then descend into the biggest
   (`du -h -d1 /home/zeros/.cache | sort -h | tail`). Also: `journalctl --disk-usage`,
   `docker images`, `flatpak list --app`, Steam games in
   `/home/zeros/.steam/debian-installation/steamapps/common`.
3. Usual safe wins: ~/.cache/pip, ~/.cache/mozilla, old files in ~/Downloads, Trash
   (`du -sh /home/zeros/.local/share/Trash`), unused Docker images, the systemd journal.
NEVER touch: ~/johan (his assistant and its data), /usr/share/ollama (models),
~/.cache/huggingface (the embedding model Johan needs), anything in ~/.ssh, games he plays.
What Johan can stage (propose_action, he approves in F4): `gio trash PATH …` for files and
folders in his home — it moves them to the Trash, which still uses space until he empties
it (Files → Trash → Empty), so say that; `apt autoremove`.
What only he can run (give the exact command): `sudo apt clean`,
`sudo journalctl --vacuum-size=300M`, `docker image prune`, `flatpak uninstall --unused`.
Report sizes before proposing anything, biggest first, and how much each step frees.
