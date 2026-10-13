---
name: no-sound
description: Diagnose no sound, wrong audio output, or a microphone not working on Linux with PipeWire
triggers:
  - I have no sound
  - my headphones aren't working
  - audio stopped working
  - sound comes out of the wrong device
  - my mic isn't picking anything up
  - discord can't hear me
  - no audio in games
---
This PC runs PipeWire + WirePlumber (pipewire-pulse for Pulse apps). Sound cards: NVIDIA
HDMI audio, the onboard analog output, and often a USB headset. Check in this order with
run_command, and stop when you find it:
1. `wpctl status` — Sinks (outputs) and Sources (mics); `*` marks the default. Most "no
   sound" is simply the wrong default (e.g. HDMI on the monitor instead of the headset).
2. `wpctl get-volume @DEFAULT_AUDIO_SINK@` — shows [MUTED] or volume 0.00.
   Mic: `wpctl get-volume @DEFAULT_AUDIO_SOURCE@`.
3. Is the device there at all? `cat /proc/asound/cards`, and `lsusb` for a USB headset.
4. Are the services alive? `systemctl --user status pipewire pipewire-pulse wireplumber --no-pager`,
   errors: `journalctl --user -u wireplumber -u pipewire -n 40 --no-pager`.
5. One app silent only: `pactl list sink-inputs` shows each app's stream, its sink and mute.
Fixes (Johan cannot run these — audio services are protected; give him the exact command):
- wrong default → `wpctl set-default ID` (ID from wpctl status), or Settings → Sound.
- muted → `wpctl set-mute @DEFAULT_AUDIO_SINK@ 0`; volume → `wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.6`.
- stuck services → `systemctl --user restart pipewire pipewire-pulse wireplumber`.
Say what you found first (which device is default, muted or not), then the one fix.
