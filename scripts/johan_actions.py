"""Changes to his machine — Johan proposes, Riadh approves, then it runs.

run_command is read-only by design. This is the other half: installing
software, changing a setting, starting a service, moving a file. The model
calls `propose_action` and NOTHING runs; the change waits in F4 (Changes)
with the exact commands, what they will do and how to undo them, and runs
only when he presses `y`. Same shape as propose_edit.

Approval is the main gate, but not the only one, because pressing `y` on
the tenth proposal of the day is not careful review:

  1. Allowlist of ACTIONS per program, never a denylist: `apt install` yes,
     `apt purge` no; `systemctl stop` yes, `systemctl mask` no. Anything not
     listed is refused at proposal time, with what to use instead.
  2. No shell. argv lists, `shell=False`; only `&&` chaining (stop at the
     first failure). Pipes, redirects, `;`, `$()` and globs are refused —
     in a writing command they are how one approved line becomes another.
  3. Checked against the real machine WHEN PROPOSED: the gsettings key
     exists, the apt package exists and isn't already installed, the unit
     isn't already running, the destination file doesn't already exist. A
     4B model invents package names and keys; this turns them into an error
     it can fix, not a broken approval card. apt is simulated (`apt-get -s`)
     so the card shows everything that would be installed or removed, and a
     removal that would drag a protected package with it is refused.
  4. Protected things are refused even with approval: the desktop, the
     NVIDIA driver, the kernel, systemd/dbus/gdm, sudo/apt/dpkg, his keys
     and secrets (SENSITIVE), .git internals, Johan's brain/ and compose/.
     Files are never deleted — `gio trash` moves them to the Trash.
  5. Root: run through `pkexec`, i.e. the desktop's own password dialog.
     Johan never sees, stores or types his password, and a root step needs
     that second, separate confirmation.
  6. Every applied step records its undo (the gsettings value it replaced,
     the inverse systemctl/apt/docker call, mv back, chmod back). "Undo" is
     staged as a new proposal — it gets approved like anything else.

Re-validated at run time: the machine can change between proposal and `y`.

Checked after doing: every step that exits 0 is then re-read from the
machine in CODE (`_check`, no model call) — the setting holds the new value,
the unit is active, the package is installed, the file exists. rc 0 is not
proof; a step that "succeeded" but didn't take effect fails the change.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "johan"
PENDING = ROOT / "brain/pending-actions.json"
LOG = ROOT / "brain/actions-log.json"
LOG_KEEP = 60
HOME = Path.home().resolve()
MAX_STEPS = 6


def _jt():
    """johan_tools, without a circular import (it registers our TOOL)."""
    jt = sys.modules.get("johan_tools")
    if jt is None:
        import johan_tools as jt  # noqa: F811
    return jt


class Refused(ValueError):
    pass


# ------------------------------------------------------------------ helpers

def _q(argv):
    return " ".join(shlex.quote(a) for a in argv)


def _probe(argv, timeout=30):
    """Run a read-only probe; returns (rc, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           env=_jt()._child_env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


PROTECTED_DIRS = [ROOT / "brain", ROOT / "compose"]

# What earlier steps of the change being parsed will create/remove, so step 2
# is judged against the disk AFTER step 1: `mkdir X && mv f X/f`, and every
# multi-step undo, are otherwise impossible to stage.
_overlay = {"created": set(), "removed": set()}


def _exists(p):
    p = str(p)
    if p in _overlay["created"]:
        return True
    if p in _overlay["removed"] or any(p.startswith(r + "/") for r in _overlay["removed"]):
        return False
    return os.path.lexists(p)


def _creates(*ps):
    for p in ps:
        _overlay["created"].add(str(p))
        _overlay["removed"].discard(str(p))


def _removes(*ps):
    for p in ps:
        _overlay["removed"].add(str(p))
        _overlay["created"].discard(str(p))


def _path(arg, must_exist=None, what="path"):
    """Resolve a file argument; it must stay inside $HOME and away from
    secrets. `must_exist` True/False checks presence. The LAST component is
    not followed (so a symlink itself can be trashed or replaced), the
    parent is."""
    if not arg or arg.startswith("-"):
        raise Refused(f"expected a {what}, got {arg!r}")
    if any(ch in arg for ch in "*?["):
        raise Refused(f"no wildcards in {arg!r} — list each file explicitly, "
                      f"so he sees exactly what changes")
    raw = Path(os.path.expanduser(arg))
    if not raw.is_absolute():
        raise Refused(f"use an absolute path or ~/…, not {arg!r} (there is no "
                      f"working directory here)")
    p = raw.parent.resolve() / raw.name
    if p == HOME or HOME not in p.parents:
        raise Refused(f"{arg} is outside your home folder — file changes are "
                      f"limited to /home/zeros")
    if _jt().SENSITIVE.search(str(p)):
        raise Refused(f"{arg} looks like keys or secrets — never touched")
    if ".git" in p.parts:
        raise Refused(".git internals are never touched — use git itself")
    for d in PROTECTED_DIRS:
        if p == d or d in p.parents:
            raise Refused(f"{arg} is Johan's own private data/config — not "
                          f"changed by actions")
    exists = _exists(p)
    if must_exist is True and not exists:
        raise Refused(f"{arg} does not exist")
    if must_exist is False and exists:
        raise Refused(f"{arg} already exists — refusing to overwrite it; pick "
                      f"a new name or trash the old one first")
    return p


def _flags(args, allowed):
    """Split leading flags off; refuse any not in `allowed`."""
    flags, rest = [], []
    for a in args:
        if a.startswith("-") and not rest:
            if a not in allowed:
                raise Refused(f"flag {a!r} is not allowed here "
                              f"(allowed: {', '.join(sorted(allowed)) or 'none'})")
            flags.append(a)
        else:
            rest.append(a)
    return flags, rest


# ------------------------------------------------------------------ apt

PKG = re.compile(r"^[a-z0-9][a-z0-9+.:-]{0,80}$")
PROTECTED_PKGS = re.compile(
    r"^(pop-desktop|pop-.*|gnome-shell.*|gdm3|xorg|xserver-xorg.*|nvidia-.*|"
    r"libnvidia-.*|linux-.*|systemd.*|libsystemd.*|dbus.*|sudo|apt|dpkg|"
    r"libc6|libc-bin|base-files|bash|coreutils|polkitd?|policykit-1|"
    r"network-manager|docker-ce.*|containerd.*|ollama|openssh-.*|grub.*|"
    r"initramfs-tools.*|login|passwd|ubuntu-.*)$")


def _apt_sim(sub, pkgs):
    rc, out = _probe(["apt-get", "-s", sub] + pkgs, timeout=60)
    inst = re.findall(r"^Inst (\S+)", out, re.M)
    remv = re.findall(r"^Remv (\S+)", out, re.M)
    return rc, out, inst, remv


def _installed(pkg):
    rc, out = _probe(["dpkg-query", "-W", "-f=${Status}", pkg])
    return rc == 0 and "install ok installed" in out


def _apt(args):
    if not args:
        raise Refused("apt needs a subcommand: install, remove, update, upgrade, autoremove")
    allowed = {"-y", "--yes", "--no-install-recommends"}
    bad = [a for a in args if a.startswith("-") and a not in allowed]
    if bad:
        raise Refused(f"apt flag {bad[0]!r} is not allowed")
    words = [a for a in args if not a.startswith("-")]
    if not words:
        raise Refused("apt needs a subcommand: install, remove, update, upgrade, autoremove")
    sub, pkgs = words[0], words[1:]
    norec = "--no-install-recommends" in args
    step = {"root": True, "timeout": 900}
    if sub in ("update",):
        if pkgs:
            raise Refused("apt update takes no package names")
        return {**step, "argv": ["apt-get", "-y", "update"],
                "what": "refresh the package lists", "undo": None}
    if sub in ("upgrade", "autoremove"):
        rc, out, inst, remv = _apt_sim(sub, [])
        bad = [p for p in remv if PROTECTED_PKGS.match(p)]
        if bad:
            raise Refused(f"apt {sub} would remove protected packages: {', '.join(bad)}")
        return {**step, "argv": ["apt-get", "-y", sub],
                "what": f"{sub}: {len(inst)} package(s) installed/upgraded, "
                        f"{len(remv)} removed" + (f" ({', '.join(remv[:8])})" if remv else ""),
                "undo": None}
    if sub not in ("install", "remove"):
        raise Refused(f"apt {sub!r} is not allowed — only install, remove, "
                      f"update, upgrade, autoremove (never purge)")
    if not pkgs:
        raise Refused(f"apt {sub} needs package names")
    for p in pkgs:
        if not PKG.match(p):
            raise Refused(f"{p!r} is not a valid package name")
    if sub == "install":
        already = [p for p in pkgs if _installed(p)]
        if already:
            raise Refused(f"already installed: {', '.join(already)}")
        rc, out, inst, remv = _apt_sim("install", (["--no-install-recommends"] if norec else []) + pkgs)
        if rc != 0:
            tail = " ".join(out.strip().splitlines()[-2:])[:300]
            raise Refused(f"apt can't install {' '.join(pkgs)}: {tail} — check "
                          f"the exact name with `apt-cache search <word>`")
        if remv:
            raise Refused(f"installing {' '.join(pkgs)} would REMOVE: {', '.join(remv)}")
        extra = [p for p in inst if p not in pkgs]
        return {**step,
                "argv": ["apt-get", "-y"] + (["--no-install-recommends"] if norec else [])
                        + ["install"] + pkgs,
                "what": f"install {', '.join(pkgs)}"
                        + (f" + {len(extra)} dependenc{'y' if len(extra) == 1 else 'ies'}"
                           f" ({', '.join(extra[:6])}{'…' if len(extra) > 6 else ''})" if extra else ""),
                "undo": ["apt", "remove"] + pkgs}
    missing = [p for p in pkgs if not _installed(p)]
    if missing:
        raise Refused(f"not installed, nothing to remove: {', '.join(missing)}")
    prot = [p for p in pkgs if PROTECTED_PKGS.match(p)]
    if prot:
        raise Refused(f"protected package(s): {', '.join(prot)} — removing them "
                      f"can break the desktop, driver or boot")
    rc, out, inst, remv = _apt_sim("remove", pkgs)
    bad = [p for p in remv if PROTECTED_PKGS.match(p)]
    if bad:
        raise Refused(f"removing {' '.join(pkgs)} would also remove protected "
                      f"packages: {', '.join(bad)}")
    return {**step, "argv": ["apt-get", "-y", "remove"] + pkgs,
            "what": f"remove {', '.join(remv or pkgs)}",
            "undo": ["apt", "install"] + pkgs}


# ------------------------------------------------------------------ flatpak

APP_ID = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$")


def _flatpak(args):
    if not args or args[0] not in ("install", "uninstall", "update"):
        raise Refused("flatpak: only install, uninstall, update")
    sub = args[0]
    _, rest = _flags(args[1:], {"-y", "--user", "--system", "--noninteractive"})
    user = "--user" in args
    base = ["flatpak", sub, "-y", "--noninteractive"] + (["--user"] if user else [])
    if sub == "update":
        return {"argv": base, "root": False, "timeout": 900,
                "what": "update flatpak apps", "undo": None}
    remote = None
    if sub == "install" and len(rest) == 2:
        remote, rest = rest[0], rest[1:]
    if len(rest) != 1 or not APP_ID.match(rest[0]):
        raise Refused("flatpak needs one app id like org.videolan.VLC "
                      "(find it with `flatpak search <name>`)")
    app = rest[0]
    rc, _ = _probe(["flatpak", "info", app])
    if sub == "install" and rc == 0:
        raise Refused(f"{app} is already installed")
    if sub == "uninstall" and rc != 0:
        raise Refused(f"{app} is not installed")
    return {"argv": base + ([remote or "flathub"] if sub == "install" else []) + [app],
            "root": False, "timeout": 900,
            "what": f"{sub} {app}",
            "undo": (["flatpak", "uninstall"] + (["--user"] if user else []) + [app]
                     if sub == "install" else
                     ["flatpak", "install"] + (["--user"] if user else []) + ["flathub", app])}


# ------------------------------------------------------------------ gsettings

_share = [Path("/usr/share"), Path.home() / ".local/share"]
THEME_DIRS = {
    "gtk-theme": [b / "themes" for b in _share] + [Path.home() / ".themes"],
    "icon-theme": [b / "icons" for b in _share] + [Path.home() / ".icons"],
    "cursor-theme": [b / "icons" for b in _share] + [Path.home() / ".icons"],
}


def _gsettings(args):
    if not args or args[0] not in ("set", "reset"):
        raise Refused("gsettings: only set and reset (get is read-only — use run_command)")
    sub = args[0]
    want = 4 if sub == "set" else 3
    if len(args) != want:
        raise Refused(f"gsettings {sub} takes " + ("SCHEMA KEY VALUE" if sub == "set"
                                                    else "SCHEMA KEY")
                      + " — quote VALUE if it has spaces")
    schema, key = args[1], args[2]
    rc, old = _probe(["gsettings", "get", schema, key])
    if rc != 0:
        raise Refused(f"no such setting {schema} {key}: {old.strip()[:200]}")
    old = old.strip()
    if sub == "set" and args[3] == old:
        raise Refused(f"{key} is already {old}")
    if sub == "set":
        # gsettings validates the value only when it runs; `range` lets the
        # card catch an out-of-range enum now.
        rc, rng = _probe(["gsettings", "range", schema, key])
        if rc == 0 and rng.startswith("enum"):
            allowed = [v.strip().strip("'") for v in rng.splitlines()[1:]]
            if args[3].strip("'\"") not in allowed:
                raise Refused(f"{key} must be one of: {', '.join(allowed)}")
    if sub == "set" and key in THEME_DIRS:
        # gsettings accepts ANY string here; a theme that isn't installed
        # silently falls back to unstyled windows. The model proposed
        # 'Yaru-dark' (Ubuntu's) on this Pop!_OS box.
        name = args[3].strip("'\"")
        found = sorted({d.name for base in THEME_DIRS[key] if base.is_dir()
                        for d in base.iterdir() if d.is_dir()})
        if name not in found:
            raise Refused(f"theme {name!r} is not installed. Installed: "
                          f"{', '.join(found[:20])}"
                          + (". For dark mode set org.gnome.desktop.interface "
                             "color-scheme 'prefer-dark'" if key == "gtk-theme" else ""))
    return {"argv": ["gsettings"] + args, "root": False, "timeout": 30,
            "what": f"{key}: {old} → {args[3] if sub == 'set' else '(default)'}",
            "undo": ["gsettings", "set", schema, key, old]}


# ------------------------------------------------------------------ systemctl

PROTECTED_UNITS = re.compile(
    r"^(gdm|display-manager|dbus|systemd-.*|NetworkManager|polkit|user@.*|"
    r"ssh|sshd|getty@.*|login|nvidia-.*|accounts-daemon|udisks2|upower|"
    r"wpa_supplicant|pipewire.*|wireplumber|gnome-.*)(\.service)?$")
INVERSE = {"start": "stop", "stop": "start", "enable": "disable",
           "disable": "enable", "pause": "unpause", "unpause": "pause",
           "install": "remove", "remove": "install"}


def _systemctl(args):
    _, rest = _flags(args, {"--user", "--now"})
    user = "--user" in args
    now = "--now" in args
    if len(rest) != 2 or rest[0] not in ("start", "stop", "restart", "enable", "disable"):
        raise Refused("systemctl: [--user] start|stop|restart|enable|disable UNIT "
                      "(one unit; never mask, kill, poweroff, reboot)")
    sub, unit = rest
    if PROTECTED_UNITS.match(unit):
        raise Refused(f"{unit} is protected — stopping it can kill the desktop, "
                      f"network or login")
    base = ["systemctl"] + (["--user"] if user else [])
    rc, load = _probe(base + ["show", "-p", "LoadState", "--value", unit])
    if load.strip() != "loaded":
        raise Refused(f"no {'user ' if user else ''}unit {unit!r} "
                      f"(try {'without' if user else 'with'} --user, or "
                      f"`systemctl list-unit-files`)")
    if sub in ("start", "stop"):
        active = _probe(base + ["is-active", unit])[1].strip() == "active"
        if (sub == "start") == active:
            raise Refused(f"{unit} is already {'running' if active else 'stopped'}")
    if sub in ("enable", "disable"):
        enabled = _probe(base + ["is-enabled", unit])[1].strip() == "enabled"
        if (sub == "enable") == enabled:
            raise Refused(f"{unit} is already {sub}d")
    flags = (["--user"] if user else []) + (["--now"] if now and sub in ("enable", "disable") else [])
    return {"argv": ["systemctl"] + flags + [sub, unit], "root": not user,
            "timeout": 120,
            "what": f"{sub} {'user ' if user else ''}service {unit}"
                    + (" (and start/stop it now)" if "--now" in flags else ""),
            "undo": (None if sub == "restart" else
                     ["systemctl"] + flags + [INVERSE[sub], unit])}


# ------------------------------------------------------------------ docker / extensions / mime

def _docker(args):
    if len(args) != 2 or args[0] not in ("start", "stop", "restart", "pause", "unpause", "pull"):
        raise Refused("docker: start|stop|restart|pause|unpause CONTAINER, or pull IMAGE "
                      "(never rm, run, exec or prune)")
    sub, name = args
    if sub != "pull":
        rc, out = _probe(["docker", "inspect", "-f", "{{.State.Status}}", name])
        if rc != 0:
            raise Refused(f"no container {name!r} (see `docker ps -a`)")
    return {"argv": ["docker", sub, name], "root": False,
            "timeout": 600 if sub == "pull" else 120,
            "what": f"docker {sub} {name}",
            "undo": ["docker", INVERSE[sub], name] if sub in INVERSE else None}


def _gnome_extensions(args):
    if len(args) != 2 or args[0] not in ("enable", "disable"):
        raise Refused("gnome-extensions: enable|disable UUID")
    rc, out = _probe(["gnome-extensions", "info", args[1]])
    if rc != 0:
        raise Refused(f"no extension {args[1]!r} (see `gnome-extensions list`)")
    return {"argv": ["gnome-extensions"] + args, "root": False, "timeout": 30,
            "what": f"{args[0]} GNOME extension {args[1]}",
            "undo": ["gnome-extensions", INVERSE[args[0]], args[1]]}


def _xdg_mime(args):
    if len(args) < 3 or args[0] != "default" or not args[1].endswith(".desktop"):
        raise Refused("xdg-mime: default APP.desktop MIME/TYPE …")
    olds = {m: _probe(["xdg-mime", "query", "default", m])[1].strip() for m in args[2:]}
    undo = None
    if len(set(olds.values())) == 1 and next(iter(olds.values())):
        undo = ["xdg-mime", "default", next(iter(olds.values()))] + args[2:]
    return {"argv": ["xdg-mime"] + args, "root": False, "timeout": 30,
            "what": f"open {', '.join(args[2:])} with {args[1]}"
                    + "".join(f" (was {v or 'unset'})" for v in set(olds.values())),
            "undo": undo}


# ------------------------------------------------------------------ processes

PROTECTED_PROCS = re.compile(
    r"^(systemd|init|gnome-shell|Xorg|Xwayland|gdm.*|dbus.*|pulseaudio|"
    r"pipewire.*|wireplumber|dockerd|containerd|ollama|polkitd|sshd|"
    r"NetworkManager|wezterm.*|zsh|bash|python3?|claude|kworker.*)$")


def _pkill(args):
    flags, rest = _flags(args, {"-9", "-KILL", "-TERM", "-x"})
    flags = [f for f in flags if f != "-x"]
    if len(rest) != 1:
        raise Refused("pkill NAME — one exact process name (see `pgrep -a NAME`)")
    name = rest[0]
    if PROTECTED_PROCS.match(name):
        raise Refused(f"{name} is protected — killing it can end the session, "
                      f"the terminal or Johan itself")
    rc, out = _probe(["pgrep", "-x", name])
    if rc != 0:
        raise Refused(f"no process named exactly {name!r} (check `pgrep -a <part>`)")
    n = len(out.split())
    return {"argv": ["pkill"] + flags + ["-x", name], "root": False, "timeout": 15,
            "what": f"close {n} process{'es' * (n > 1)} named {name}"
                    + (" (forced)" if flags else ""),
            "undo": None}


def _kill(args):
    flags, rest = _flags(args, {"-9", "-KILL", "-TERM"})
    if len(rest) != 1 or not rest[0].isdigit() or int(rest[0]) < 2:
        raise Refused("kill PID — one process id")
    pid = rest[0]
    comm = Path(f"/proc/{pid}/comm")
    if not comm.exists():
        raise Refused(f"no process {pid}")
    name = comm.read_text().strip()
    if PROTECTED_PROCS.match(name) or int(pid) == os.getpid():
        raise Refused(f"process {pid} is {name} — protected")
    return {"argv": ["kill"] + flags + [pid], "root": False, "timeout": 15,
            "what": f"close process {pid} ({name})", "undo": None}


# ------------------------------------------------------------------ files

def _mkdir(args):
    flags, rest = _flags(args, {"-p"})
    if len(rest) != 1:
        raise Refused("mkdir [-p] ONE_PATH")
    p = _path(rest[0], must_exist=False)
    _creates(p)
    return {"argv": ["mkdir"] + flags + [str(p)], "root": False, "timeout": 15,
            "what": f"create folder {p}", "undo": ["rmdir", str(p)]}


def _touch(args):
    if len(args) != 1:
        raise Refused("touch ONE_NEW_FILE")
    p = _path(args[0], must_exist=False)
    _creates(p)
    return {"argv": ["touch", str(p)], "root": False, "timeout": 15,
            "what": f"create empty file {p}", "undo": ["gio", "trash", str(p)]}


def _cp(args):
    flags, rest = _flags(args, {"-r", "-a", "-n"})
    flags = [f for f in flags if f != "-n"]
    if len(rest) != 2:
        raise Refused("cp [-r] SOURCE NEW_DESTINATION (one of each)")
    src = _path(rest[0], must_exist=True, what="source")
    dst = _path(rest[1], must_exist=False, what="destination")
    _creates(dst)
    return {"argv": ["cp", "-n"] + flags + [str(src), str(dst)], "root": False,
            "timeout": 300, "what": f"copy {src} → {dst}",
            "undo": ["gio", "trash", str(dst)]}


def _mv(args):
    args = [a for a in args if a != "-n"]
    if len(args) != 2:
        raise Refused("mv SOURCE NEW_DESTINATION")
    src = _path(args[0], must_exist=True, what="source")
    dst = _path(args[1], must_exist=False, what="destination")
    _removes(src)
    _creates(dst)
    return {"argv": ["mv", "-n", str(src), str(dst)], "root": False, "timeout": 120,
            "what": f"move {src} → {dst}", "undo": ["mv", str(dst), str(src)]}


def _ln(args):
    if len(args) != 3 or args[0] != "-s":
        raise Refused("ln -s TARGET NEW_LINK")
    target = _path(args[1], must_exist=True, what="target")
    link = _path(args[2], must_exist=False, what="link")
    _creates(link)
    return {"argv": ["ln", "-s", str(target), str(link)], "root": False, "timeout": 15,
            "what": f"link {link} → {target}", "undo": ["gio", "trash", str(link)]}


MODE = re.compile(r"^(0?[0-7]{3}|[ugoa]*[+-=][rwxX]+)$")


def _chmod(args):
    if len(args) != 2 or not MODE.match(args[0]):
        raise Refused("chmod MODE PATH — e.g. 644, 755 or u+x (no setuid, no -R)")
    p = _path(args[1], must_exist=True)
    if not os.path.lexists(p):          # made by an earlier step: no old mode yet
        return {"argv": ["chmod", args[0], str(p)], "root": False, "timeout": 15,
                "what": f"permissions of {p} → {args[0]}", "undo": None}
    old = oct(stat.S_IMODE(p.stat().st_mode))[2:]
    return {"argv": ["chmod", args[0], str(p)], "root": False, "timeout": 15,
            "what": f"permissions of {p}: {old} → {args[0]}",
            "undo": ["chmod", old, str(p)]}


def _rmdir(args):
    if len(args) != 1:
        raise Refused("rmdir ONE_EMPTY_FOLDER")
    p = _path(args[0], must_exist=True)
    _removes(p)
    return {"argv": ["rmdir", str(p)], "root": False, "timeout": 15,
            "what": f"remove empty folder {p}", "undo": ["mkdir", str(p)]}


def _gio(args):
    if len(args) < 2 or args[0] != "trash":
        raise Refused("gio trash PATH … — files go to the Trash, never deleted")
    paths = [_path(a, must_exist=True) for a in args[1:]]
    if len(paths) > 20:
        raise Refused("at most 20 files per trash")
    _removes(*paths)
    return {"argv": ["gio", "trash"] + [str(p) for p in paths], "root": False,
            "timeout": 60,
            "what": f"move to Trash: {', '.join(str(p) for p in paths[:5])}"
                    + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""),
            "undo": None, "undo_note": "restore it from the Trash"}


RULES = {
    "apt": _apt, "apt-get": _apt, "flatpak": _flatpak, "gsettings": _gsettings,
    "systemctl": _systemctl, "docker": _docker,
    "gnome-extensions": _gnome_extensions, "xdg-mime": _xdg_mime,
    "pkill": _pkill, "kill": _kill,
    "mkdir": _mkdir, "touch": _touch, "cp": _cp, "mv": _mv, "ln": _ln,
    "chmod": _chmod, "rmdir": _rmdir, "gio": _gio,
}

INSTEAD = {
    "rm": "files are never deleted: use `gio trash PATH` (it goes to the Trash)",
    "sudo": "don't write sudo — root steps are detected and he gets the system "
            "password dialog",
    "pkexec": "don't write pkexec — root steps are detected automatically",
    "dd": "never", "mkfs": "never", "shred": "never",
    "sh": "no shell — write the commands themselves, joined with &&",
    "bash": "no shell — write the commands themselves, joined with &&",
    "curl": "no downloads-and-run; install from apt or flatpak",
    "wget": "no downloads-and-run; install from apt or flatpak",
    "pip": "not supported yet — say so instead",
    "snap": "snap isn't installed here; use apt or flatpak",
    "tee": "writing file contents isn't supported — use propose_edit for files in a git repo",
    "echo": "writing file contents isn't supported — use propose_edit for files in a git repo",
    "sed": "use propose_edit for file contents",
    "dconf": "use gsettings set",
}


def parse(commands):
    """commands (string, `&&`-joined, or list of strings) → list of steps.
    Raises Refused with a message the model can act on."""
    if isinstance(commands, list):
        commands = " && ".join(str(c) for c in commands)
    commands = (commands or "").strip()
    if not commands:
        raise Refused("commands is empty")
    try:
        argv = shlex.split(commands)
    except ValueError as e:
        raise Refused(f"could not parse: {e}")
    # Judged per TOKEN, after quoting is resolved: there is no shell, so
    # "['<Super>v']" inside quotes is inert text and must pass, while a bare
    # `>` or `|` token means the model expected a shell it doesn't have.
    for tok in argv:
        if tok != "&&" and (re.fullmatch(r"[|;<>&]+", tok) or re.match(r"\d?>", tok)
                            or tok.endswith(";") or "`" in tok or "$(" in tok):
            raise Refused(f"{tok!r}: pipes, ';', '||', redirects and $() are not "
                          f"allowed in a change — chain plain commands with && only")
    segs, cur = [], []
    for tok in argv:
        if tok == "&&":
            segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    segs.append(cur)
    if any(not s for s in segs):
        raise Refused("empty command around &&")
    if len(segs) > MAX_STEPS:
        raise Refused(f"at most {MAX_STEPS} steps per change — split it")
    steps = []
    _overlay["created"].clear()
    _overlay["removed"].clear()
    for seg in segs:
        prog = Path(seg[0]).name
        if prog in ("sudo", "pkexec") and len(seg) > 1:
            prog, seg = Path(seg[1]).name, seg[1:]   # tolerate it: root is detected anyway
        rule = RULES.get(prog)
        if rule is None:
            jt = _jt()
            if prog in jt.SAFE_COMMANDS and not jt._check_command(seg):
                raise Refused(f"`{_q(seg)}` only reads — run it with run_command, "
                              f"not as a change")
            hint = INSTEAD.get(prog)
            raise Refused(f"`{prog}` can't be used in a change"
                          + (f": {hint}" if hint else
                             ". Allowed: " + ", ".join(sorted(RULES))))
        step = rule(seg[1:])
        step["shown"] = _q(step["argv"])
        steps.append(step)
    return steps


# ------------------------------------------------------------------ store

def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def list_actions():
    return _load(PENDING, [])


def propose_action(commands=None, why="", undo_last=False, _undo_of=None):
    """Stage a change to his machine. Runs NOTHING."""
    if undo_last:
        return propose_undo()
    try:
        steps = parse(commands)
    except Refused as e:
        return {"error": str(e), "note": "nothing was staged"}
    pending = list_actions()
    aid = max([a["id"] for a in pending] + [a["id"] for a in _load(LOG, [])], default=0) + 1
    action = {"id": aid, "why": (why or "").strip()[:300],
              "created": datetime.now().isoformat(timespec="seconds"),
              "steps": steps, "root": any(s["root"] for s in steps),
              "undo_of": _undo_of}
    pending.append(action)
    _save(PENDING, pending)
    return {"proposed": True, "action_id": aid,
            "steps": [s["what"] for s in steps],
            "needs_password": action["root"],
            "note": "NOT run. Riadh reviews it in the Changes tab (F4) on the "
                    "PC and presses y to run it" + (" — root steps open the "
                    "system password dialog" if action["root"] else "")
                    + ". Tell him that; don't claim it's done."}


def discard_action(aid):
    pending = list_actions()
    rest = [a for a in pending if a["id"] != int(aid)]
    if len(rest) == len(pending):
        return {"error": f"no pending change {aid}"}
    _save(PENDING, rest)
    return {"discarded": int(aid)}


def _exec(step):
    argv = step["argv"]
    env = _jt()._child_env()
    if step["root"]:
        prefix = ["pkexec", "env", "DEBIAN_FRONTEND=noninteractive",
                  "PATH=/usr/sbin:/usr/bin:/sbin:/bin"]
        argv = prefix + argv
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=step.get("timeout", 120), env=env,
                           stdin=subprocess.DEVNULL)
        out = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {step.get('timeout', 120)}s"
    except OSError as e:
        return 127, str(e)
    if step["root"] and rc in (126, 127) and "pkexec" in argv[0]:
        out = ("password dialog was cancelled or not authorised" if rc == 126
               else "pkexec could not authenticate") + (f"\n{out}" if out else "")
    return rc, out


# ------------------------------------------------------------------ check after doing
#
# rc 0 means the program didn't complain, not that the machine changed: a
# user unit can exit 0 and die a second later, pkill exits 0 while the
# process ignores TERM, gsettings accepts a key a schema override pins. Each
# step is read back from the machine after it runs. No model call — this is
# a comparison, and a 4B model asked "did it work?" says yes.

def _settle(probe, tries=6, delay=0.5):
    """Re-run `probe` until it says True (services and processes take a
    moment to get there); returns its last (verdict, message)."""
    v, msg = probe()
    for _ in range(tries - 1):
        if v is True:
            break
        time.sleep(delay)
        v, msg = probe()
    return v, msg


_GV_TYPE = re.compile(r"^(@[a-z()]+|u?int(16|32|64)|byte|double)\s+")


def _gs_norm(v):
    """gsettings prints `'prefer-dark'`, `uint32 300`, `@as []`; the model
    writes prefer-dark, 300, []. Compare meaning, not spelling."""
    v = _GV_TYPE.sub("", (v or "").strip()).replace('"', "'")
    v = re.sub(r"\s*,\s*", ", ", v)
    if len(v) >= 2 and v[0] == v[-1] == "'":
        v = v[1:-1]
    try:
        return float(v)
    except ValueError:
        return v


def _mode_ok(spec, mode):
    """Does `mode` satisfy chmod `spec`? None when it depends on things not
    worth modelling here (=, X)."""
    if re.fullmatch(r"0?[0-7]{3}", spec):
        return mode == int(spec, 8)
    m = re.fullmatch(r"([ugoa]*)([+-])([rwx]+)", spec)
    if not m:
        return None
    bits = 0
    for w in (m[1] or "u").replace("a", "ugo"):
        for b in m[3]:
            bits |= {"r": 4, "w": 2, "x": 1}[b] << {"u": 6, "g": 3, "o": 0}[w]
    return (mode & bits) == bits if m[2] == "+" else not (mode & bits)


def _check(step):
    """Did this step take effect? → (True | False | None, message).
    None = no meaningful check (apt update, gsettings reset, chmod =…)."""
    a = step["argv"]
    prog = Path(a[0]).name
    try:
        if prog == "gsettings":
            if a[1] != "set":
                return None, "a reset is not checked"
            key, want = a[3], a[4]
            rc, out = _probe(["gsettings", "get", a[2], key])
            if rc != 0:
                return None, f"could not read {key} back"
            if _gs_norm(out) == _gs_norm(want):
                return True, f"{key} now reads {out.strip()}"
            return False, f"the setting still reads {out.strip()}"
        if prog == "systemctl":
            base = ["systemctl"] + (["--user"] if "--user" in a else [])
            sub, unit = a[-2], a[-1]
            if sub in ("enable", "disable"):
                st = _probe(base + ["is-enabled", unit])[1].strip()
                if (st == "enabled") != (sub == "enable"):
                    return False, f"{unit} is still {st or 'unknown'}"
                if "--now" not in a:
                    return True, f"{unit} is {st}"
            want = sub in ("start", "restart", "enable")

            def probe():
                st = _probe(base + ["is-active", unit])[1].strip()
                if st in ("activating", "deactivating", "reloading"):
                    return None, f"{unit} is still {st}"
                return (st == "active") == want, f"{unit} is {st or 'unknown'}"
            return _settle(probe)
        if prog in ("apt-get", "apt"):
            w = [x for x in a[1:] if not x.startswith("-")]
            if not w or w[0] not in ("install", "remove"):
                return None, f"apt {w[0] if w else ''} is not checked"
            inst = w[0] == "install"
            bad = [p for p in w[1:] if _installed(p) != inst]
            if bad:
                return False, f"{', '.join(bad)} {'still not installed' if inst else 'still installed'}"
            return True, f"{'installed' if inst else 'removed'}: {', '.join(w[1:])}"
        if prog == "flatpak":
            sub, app = a[1], a[-1]
            if sub not in ("install", "uninstall"):
                return None, "flatpak update is not checked"
            there = _probe(["flatpak", "info", app])[0] == 0
            if there != (sub == "install"):
                return False, f"{app} is still {'missing' if sub == 'install' else 'installed'}"
            return True, f"{app} {'installed' if there else 'removed'}"
        if prog == "docker":
            sub, name = a[1], a[2]
            if sub == "pull":
                ok = _probe(["docker", "image", "inspect", name])[0] == 0
                return ok, f"image {name} {'is present' if ok else 'is still missing'}"
            want = {"stop": ("exited", "created", "dead"),
                    "pause": ("paused",)}.get(sub, ("running",))

            def probe():
                st = _probe(["docker", "inspect", "-f", "{{.State.Status}}", name])[1].strip()
                return st in want, f"{name} is {st or 'unknown'}"
            return _settle(probe)
        if prog == "gnome-extensions":
            m = re.search(r"State:\s*(\w+)", _probe(["gnome-extensions", "info", a[2]])[1])
            st = m[1].upper() if m else "UNKNOWN"
            if (st in ("ENABLED", "ACTIVE")) != (a[1] == "enable"):
                return False, f"{a[2]} is still {st}"
            return True, f"{a[2]} is {st}"
        if prog == "xdg-mime":
            bad = [m for m in a[3:]
                   if _probe(["xdg-mime", "query", "default", m])[1].strip() != a[2]]
            if bad:
                return False, f"{', '.join(bad)} still opens with something else"
            return True, f"{a[2]} is the default"
        if prog in ("pkill", "kill"):
            gone = ((lambda: _probe(["pgrep", "-x", a[-1]])[0] != 0) if prog == "pkill"
                    else (lambda: not Path(f"/proc/{a[-1]}").exists()))
            return _settle(lambda: (gone(), f"{a[-1]} " + ("is gone" if gone() else "is still running")))
        if prog in ("mkdir", "touch", "cp", "ln"):
            p = Path(a[-1])
            ok = {"mkdir": p.is_dir, "ln": p.is_symlink}.get(prog, lambda: os.path.lexists(p))()
            return ok, f"{p} {'exists' if ok else 'does not exist'}"
        if prog == "mv":
            if not os.path.lexists(a[-1]):
                return False, f"{a[-1]} does not exist"
            if os.path.lexists(a[-2]):
                return False, f"{a[-2]} is still there"
            return True, f"{a[-1]} exists, {a[-2]} is gone"
        if prog == "chmod":
            mode = stat.S_IMODE(Path(a[-1]).stat().st_mode)
            v = _mode_ok(a[1], mode)
            return v, (f"mode still reads {oct(mode)[2:]}" if v is False
                       else f"mode is {oct(mode)[2:]}")
        if prog == "rmdir" or (prog == "gio" and a[1] == "trash"):
            left = [x for x in a[(1 if prog == "rmdir" else 2):] if os.path.lexists(x)]
            return not left, (f"still there: {', '.join(left)}" if left else "gone")
    except (OSError, IndexError, KeyError) as e:
        return None, f"could not check: {e}"
    return None, "no check for this step"


def run_action(aid, auto=False):
    """Run an approved change. Called from the UI on `y` — or by
    propose_action when an always-allow rule covers every step — never by
    the model's own choice."""
    pending = list_actions()
    action = next((a for a in pending if a["id"] == int(aid)), None)
    if not action:
        return {"error": f"no pending change {aid}"}
    # Re-check against the machine as it is NOW.
    try:
        fresh = parse(" && ".join(s["shown"] for s in action["steps"]))
    except Refused as e:
        _save(PENDING, [a for a in pending if a["id"] != action["id"]])
        return {"error": f"not run — no longer valid: {e}", "dropped": True}
    results, undo, no_undo, failure = [], [], [], None
    for step in fresh:
        rc, out = _exec(step)
        res = {"step": step["shown"], "rc": rc, "output": out[-1500:]}
        results.append(res)
        if rc != 0:
            failure = f"step {len(results)} failed (exit {rc}): {out[-300:] or 'no output'}"
            break
        res["verified"], res["check"] = _check(step)
        if res["verified"] is False:
            # It ran but didn't take effect, so its undo would act on a state
            # that doesn't exist: not recorded.
            failure = f"step {len(results)} ran, but {res['check']}"
            break
        if step.get("undo"):
            undo.insert(0, _q(step["undo"]))
        else:
            no_undo.append(step["what"] + (f" ({step['undo_note']})" if step.get("undo_note") else ""))
    ok = failure is None
    verified = ok and all(r.get("verified") is True for r in results)
    unchecked = [r["step"] for r in results if "verified" in r and r["verified"] is None]
    entry = {"id": action["id"], "why": action["why"], "undo_of": action.get("undo_of"),
             "turn": action.get("turn"), "auto": auto,
             "ran": datetime.now().isoformat(timespec="seconds"),
             "ok": ok, "verified": verified, "results": results, "undo": undo,
             "no_undo": no_undo,
             "undo_note": next((s.get("undo_note") for s in fresh if s.get("undo_note")), None)}
    log = _load(LOG, [])
    log.append(entry)
    _save(LOG, log[-LOG_KEEP:])
    _save(PENDING, [a for a in pending if a["id"] != action["id"]])
    return {"ok": ok, "id": action["id"], "steps_run": len(results),
            "steps_total": len(fresh), "undo": undo, "results": results,
            "verified": verified, "unchecked": unchecked,
            **({"error": failure} if failure else {})}


def describe_result(res):
    """One line for a toast/notification: 'done — verified' vs 'ran but not
    verified', so rc 0 is never presented as proof."""
    if res.get("error"):
        return res["error"]
    n = res.get("steps_run", 0)
    if res.get("verified"):
        return f"done — verified ({n} step{'s' * (n != 1)})"
    return f"ran but not verified ({', '.join(res.get('unchecked') or [])[:120]} has no check)"


def recent(n=10):
    return _load(LOG, [])[-n:]


def propose_undo(aid=None):
    """Stage the undo of the last (or given) applied change — approved like any other."""
    # An undo is not itself undoable via `u` — pressing it twice must not
    # quietly stage a "redo" of what he just reverted.
    # A half-failed change still lists the undo of the steps that DID run.
    log = [e for e in _load(LOG, []) if e.get("undo")
           and not e.get("undone") and not e.get("undo_of")]
    entry = (next((e for e in log if e["id"] == int(aid)), None) if aid
             else (log[-1] if log else None))
    if not entry:
        return {"error": "no applied change with an automatic undo"
                         + (" — a trashed file comes back from the Trash" if aid is None else "")}
    res = propose_action(" && ".join(entry["undo"]), why=f"undo change #{entry['id']}"
                         + (f" ({entry['why'][:80]})" if entry.get("why") else ""),
                         _undo_of=entry["id"])
    if res.get("proposed"):
        full = _load(LOG, [])
        for e in full:
            if e["id"] == entry["id"]:
                e["undone"] = res["action_id"]
        _save(LOG, full)
    return res


TOOL = {"type": "function", "function": {
    "name": "propose_action",
    "description": "CHANGE his machine: install/remove software (apt, flatpak), settings "
                   "(gsettings), services, docker, close apps, make/move/copy/trash files. "
                   "Stages it; runs only when he approves in F4. Steps joined with &&. "
                   "`undo_last`=true undoes the last change.",
    "parameters": {"type": "object", "properties": {
        "commands": {"type": "string"}, "why": {"type": "string"},
        "undo_last": {"type": "boolean"}}}}}
