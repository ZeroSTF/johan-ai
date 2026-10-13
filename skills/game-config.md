---
name: game-config
description: Find and read a game's config, settings, keybinds or save files (Steam, Proton, TF2)
triggers:
  - can you check my tf2 config
  - where is my autoexec
  - show me my game's keybinds config file
  - where does this game keep its settings
  - find my save files
  - what's in my crosshair config
---
Steam lives under /home/zeros/.steam/debian-installation (steamapps/common = installed
games; there is no ~/.local/share/Steam here). Use run_command:
1. Locate the game folder: `ls /home/zeros/.steam/debian-installation/steamapps/common`.
2. TF2 (native Linux): configs are in
   `/home/zeros/.steam/debian-installation/steamapps/common/Team Fortress 2/tf/cfg`
   (config.cfg = binds and settings written by the game, autoexec.cfg = his own, runs at
   start) and `tf/custom/*/cfg` for mods/huds. Read with `cat`, quote the path (spaces).
3. Proton (Windows) games keep settings in the prefix:
   `steamapps/compatdata/APPID/pfx/drive_c/users/steamuser/` — look in `AppData/Local`,
   `AppData/Roaming`, `Documents/My Games`. Find the APPID with
   `grep -l "GAME NAME" /home/zeros/.steam/debian-installation/steamapps/appmanifest_*.acf`.
4. Native games may also use `~/.config/GAME` or `~/.local/share/GAME`:
   `find /home/zeros/.config /home/zeros/.local/share -maxdepth 2 -iname "*game*"`.
5. Not sure where? `find /home/zeros/.steam/debian-installation/steamapps -maxdepth 6 -iname "*.cfg" -ipath "*GAME*"`.
Never say a file doesn't exist without checking. Show the relevant lines, not the whole file.
To change a config: these files aren't in a git repo, so you can't edit them yourself —
give him the exact lines to add/change and where; a backup copy can be staged with
propose_action `cp FILE FILE.bak`. Tell him to close the game first (it rewrites config.cfg on exit).
