---
name: gnome-shortcut
description: Fix, add, change or find a keyboard shortcut on the GNOME 42 desktop (Pop!_OS)
triggers:
  - my keyboard shortcut stopped working
  - change the shortcut for the terminal
  - add a keybinding to open firefox
  - what is super+v bound to
  - set a hotkey to take a screenshot
  - which key combo opens the launcher
---
Shortcuts live in gsettings. Look before changing anything:
1. Find what a combo does (or whether two things fight over it):
   `gsettings list-recursively | grep -i "<Super>v"` (write the combo as gsettings does:
   `<Super>`, `<Primary>` = Ctrl, `<Alt>`, `<Shift>`, lowercase key).
2. Built-in shortcuts are in these schemas — list one with `gsettings list-recursively SCHEMA`:
   org.gnome.desktop.wm.keybindings (windows, workspaces), org.gnome.shell.keybindings,
   org.gnome.mutter.keybindings, org.gnome.settings-daemon.plugins.media-keys (volume,
   screenshots, terminal), org.gnome.shell.extensions.pop-shell (Pop tiling — it grabs many
   Super combos).
3. Custom shortcuts: `gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings`
   gives paths; read one with
   `gsettings get org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:PATH binding`
   (also `name`, `command`).
Changing: call propose_action with `gsettings set SCHEMA KEY VALUE` (or `gsettings reset`);
values are lists like `"['<Super>t']"`. It runs only after he approves in F4. If the combo is
taken, say by what, and free it in the same proposal (`gsettings set … "[]"`).
A brand-new custom shortcut needs a new path added to the custom-keybindings list; if that
gets fiddly, tell him Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts.
Shortcuts on X11 apply immediately; no logout needed.
