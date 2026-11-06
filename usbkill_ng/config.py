# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""Configuration loading and validation for usbkill-ng."""
from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/usbkill/usbkill.ini")
DEFAULT_LOG_PATH = Path("/var/log/usbkill/usbkill.log")
DEFAULT_PAUSE_PATH = Path("/etc/usbkill/pause")
DISABLED_FLAG_PATH = Path("/etc/usbkill/disabled")   # makes the systemd unit refuse to start

VALID_ACTIONS = ("lock", "shutdown", "restart")
VALID_SHUTDOWN_MODES = ("graceful", "fast", "immediate")
MAX_DELAY = 300
VALID_LOCK_METHODS = ("loginctl", "xflock4")
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}(:[^\s,]+)?$")  # vid:pid or vid:pid:serial

DEFAULT_INI = """\
[config]
# What to do when a USB change is detected:  lock | shutdown | restart
action = lock

# Seconds to wait after a trigger before the action runs (0 = immediately, max 300).
# The countdown is cancelled by Ctrl+C or by creating the pause file (see below).
# Unplugging the device does NOT cancel it, so it cannot be defeated by plug-then-unplug.
delay = 0

# How forcefully shutdown/restart is carried out (ignored when action = lock):
#   graceful  : normal systemd shutdown. Safest for data; may take several seconds.
#   fast      : kills processes and unmounts/remounts read-only, without stopping
#               services one by one.
#   immediate : sync, then instant power-off with no unmount. Unsaved data can be lost.
shutdown_mode = graceful

# true  : lock the screen at the instant of the trigger, while any delay is counting and
#         while a graceful shutdown is still in progress.
# false : only run the action.
lock_first = true

# Seconds to wait after an action before taking a fresh baseline. Whatever is plugged in
# at that moment becomes the new normal; unplugging it later triggers again.
cooldown = 5

# Seconds between sysfs checks when pyudev is not available.
poll_interval = 0.25

# Seconds to wait after a USB event before rescanning, so the device finishes enumerating.
debounce = 0.3

# Grace period (seconds) after the watcher starts: the baseline keeps refreshing and nothing
# can trigger. Use about 20 when started at boot, because hubs and Bluetooth can finish
# enumerating late. 0 = armed immediately. The --arm-delay option overrides this value.
arm_delay = 0

# Lock methods, tried in this order. ALL listed methods run (locking twice is harmless).
#   loginctl : asks systemd-logind to lock every session
#   xflock4  : runs Xfce's lock command as the logged-in desktop user
lock_methods = loginctl, xflock4

# Devices that may be plugged in or removed without triggering the action.
# Format: vendor:product, or vendor:product:serial (stricter; harder to spoof).
# Comma separated. Run "usbkill-ng --list-devices" to see the real values.
whitelist =

# Built-in hardware whose disappearance/reappearance is ignored (e.g. a Bluetooth reset on
# resume). Same format and same effect as whitelist; kept separate so the file stays readable.
ignore =

# While this file exists, triggers are logged but the action is NOT performed.
#   pause:  sudo touch /etc/usbkill/pause      resume:  sudo rm /etc/usbkill/pause
pause_file = /etc/usbkill/pause

log_file = /var/log/usbkill/usbkill.log
"""


class ConfigError(Exception):
    """Raised for any problem with the configuration file."""


@dataclass(frozen=True)
class Settings:
    action: str = "lock"
    cooldown: float = 5.0
    poll_interval: float = 0.25
    debounce: float = 0.3
    delay: float = 0.0
    arm_delay: float = 0.0
    shutdown_mode: str = "graceful"
    lock_first: bool = True
    lock_methods: tuple = ("loginctl", "xflock4")
    whitelist: frozenset = frozenset()
    ignore: frozenset = frozenset()
    log_file: Path = DEFAULT_LOG_PATH
    pause_file: Path = DEFAULT_PAUSE_PATH


def parse_device_ids(text: str, key: str) -> frozenset:
    ids = set()
    for item in re.split(r"[,\s]+", text.strip()):
        if not item:
            continue
        item = item.lower()
        if not DEVICE_ID_RE.match(item):
            raise ConfigError(f"'{key}': '{item}' is not like 1d6b:0002 or 1d6b:0002:serial")
        ids.add(item)
    return frozenset(ids)


def load_settings(path: Path, allow_missing: bool = False) -> Settings:
    path = Path(path)
    if not path.exists():
        if allow_missing:
            return Settings()
        raise ConfigError(f"config file not found: {path} (create it with --init-config)")

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read_string(path.read_text())
    except (configparser.Error, OSError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if "config" not in parser:
        raise ConfigError(f"{path}: missing [config] section")
    sec = parser["config"]

    action = sec.get("action", "lock").strip().lower()
    if action not in VALID_ACTIONS:
        raise ConfigError(f"'action' must be one of {', '.join(VALID_ACTIONS)} (got '{action}')")

    try:
        cooldown = sec.getfloat("cooldown", 5.0)
        poll = sec.getfloat("poll_interval", 0.25)
        debounce = sec.getfloat("debounce", 0.3)
        delay = sec.getfloat("delay", 0.0)
        arm_delay = sec.getfloat("arm_delay", 0.0)
        lock_first = sec.getboolean("lock_first", True)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if cooldown < 1:
        raise ConfigError("'cooldown' must be at least 1 second")
    if not 0.05 <= poll <= 5:
        raise ConfigError("'poll_interval' must be between 0.05 and 5 seconds")

    if not 0 <= delay <= MAX_DELAY:
        raise ConfigError(f"'delay' must be between 0 and {MAX_DELAY} seconds")
    if not 0 <= arm_delay <= MAX_DELAY:
        raise ConfigError(f"'arm_delay' must be between 0 and {MAX_DELAY} seconds")
    mode = sec.get("shutdown_mode", "graceful").strip().lower()
    if mode not in VALID_SHUTDOWN_MODES:
        raise ConfigError(f"'shutdown_mode' must be one of {', '.join(VALID_SHUTDOWN_MODES)} (got '{mode}')")
    if not 0 <= debounce <= 2:
        raise ConfigError("'debounce' must be between 0 and 2 seconds")

    methods = tuple(m for m in re.split(r"[,\s]+", sec.get("lock_methods", "loginctl, xflock4").strip().lower()) if m)
    if not methods:
        raise ConfigError("'lock_methods' is empty")
    for m in methods:
        if m not in VALID_LOCK_METHODS:
            raise ConfigError(f"unknown lock method '{m}' (valid: {', '.join(VALID_LOCK_METHODS)})")

    return Settings(
        action=action,
        cooldown=cooldown,
        poll_interval=poll,
        debounce=debounce,
        delay=delay,
        arm_delay=arm_delay,
        shutdown_mode=mode,
        lock_first=lock_first,
        lock_methods=methods,
        whitelist=parse_device_ids(sec.get("whitelist", ""), "whitelist"),
        ignore=parse_device_ids(sec.get("ignore", ""), "ignore"),
        log_file=Path(sec.get("log_file", str(DEFAULT_LOG_PATH)).strip()),
        pause_file=Path(sec.get("pause_file", str(DEFAULT_PAUSE_PATH)).strip()),
    )


# Keys compared by --status to detect a service that started with older settings.
# (arm_delay is excluded on purpose: the service overrides it with --arm-delay.)
COMPARE_KEYS = ("action", "delay", "shutdown_mode", "lock_first", "cooldown", "debounce",
                "poll_interval", "lock_methods", "whitelist", "ignore")


def settings_summary(settings: Settings, version: str = "") -> str:
    """One log-friendly line of key=value pairs (no spaces inside values)."""
    def flat(items):
        return ",".join(sorted(items)) or "-"

    pairs = [
        ("version", version or "-"),
        ("action", settings.action),
        ("delay", f"{settings.delay:g}"),
        ("shutdown_mode", settings.shutdown_mode),
        ("lock_first", str(settings.lock_first).lower()),
        ("arm_delay", f"{settings.arm_delay:g}"),
        ("cooldown", f"{settings.cooldown:g}"),
        ("debounce", f"{settings.debounce:g}"),
        ("poll_interval", f"{settings.poll_interval:g}"),
        ("lock_methods", ",".join(settings.lock_methods)),
        ("whitelist", flat(settings.whitelist)),
        ("ignore", flat(settings.ignore)),
    ]
    return " ".join(f"{k}={v}" for k, v in pairs)


def parse_summary(text: str) -> dict:
    """Inverse of settings_summary: 'k=v k=v ...' -> dict."""
    return dict(token.split("=", 1) for token in text.split() if "=" in token)
