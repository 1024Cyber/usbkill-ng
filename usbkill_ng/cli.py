# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""Command line entry point for usbkill-ng."""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __author__, __github__, __twitter__, __url__, __version__
from .actions import LOG, lock_preflight, run_cmd
from .config import (COMPARE_KEYS, DEFAULT_CONFIG_PATH, DEFAULT_INI, DISABLED_FLAG_PATH, MAX_DELAY,
                     ConfigError, load_settings, parse_summary, settings_summary)
from .devices import DeviceReadError, read_devices
from .power import power_preflight, run_action
from .watcher import Watcher, make_source

PREFLIGHT = {"lock": lock_preflight, "shutdown": power_preflight, "restart": power_preflight}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="usbkill-ng",
        description="Lock, shut down or restart the computer when USB devices change. "
                    "Runs in DRY-RUN mode unless --live is given.")
    p.add_argument("--version", action="version", version=f"usbkill-ng {__version__}")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="config file path")
    p.add_argument("--live", action="store_true",
                   help="really perform actions (needs root). Default is dry-run: log only.")
    p.add_argument("--yes", action="store_true",
                   help="skip the YES confirmation for live shutdown/restart (for services)")
    p.add_argument("--arm-delay", type=float, default=None, metavar="SECONDS",
                   help="grace period after start during which nothing can trigger "
                        "(overrides arm_delay in the config)")
    p.add_argument("--watch", action="store_true",
                   help="watch for USB changes and run the configured action")
    p.add_argument("--test-lock", action="store_true",
                   help="run the lock action once, with no delay (dry-run unless --live)")
    p.add_argument("--test-action", action="store_true",
                   help="run the configured action once, delay included, without any USB event")
    p.add_argument("--about", action="store_true",
                   help="show the author, links, license and credits, then exit")
    p.add_argument("--pause", action="store_true",
                   help="create the pause file: triggers are logged but no action runs")
    p.add_argument("--resume", action="store_true", help="remove the pause file")
    p.add_argument("--status", action="store_true",
                   help="show service state, pause state, and whether the running service "
                        "matches the config file")
    p.add_argument("--show-config", action="store_true",
                   help="print the settings that will actually be used, then exit")
    p.add_argument("--list-devices", action="store_true",
                   help="print connected USB devices with ids for the whitelist/ignore lists")
    p.add_argument("--init-config", action="store_true",
                   help="write a default config file (never overwrites an existing one)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def setup_logging(log_file: Path, verbose: bool, live: bool) -> None:
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(LOG.handlers):
        handler.close()
        LOG.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    LOG.addHandler(console)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.close(os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError as exc:
        if live:
            LOG.warning("cannot write log file %s: %s", log_file, exc)


def init_config(path: Path) -> int:
    if path.exists():
        print(f"[ERROR] {path} already exists; not overwriting it.", file=sys.stderr)
        return 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_text(DEFAULT_INI)
        path.chmod(0o644)
    except OSError as exc:
        print(f"[ERROR] cannot write {path}: {exc} (try sudo)", file=sys.stderr)
        return 1
    print(f"[OK] wrote {path}")
    return 0


def show_config(settings, path: Path) -> int:
    note = "" if path.exists() else " - not found, showing built-in defaults"
    print(f"# effective settings (file: {path}{note})")
    for key, value in dataclasses.asdict(settings).items():
        if isinstance(value, (set, frozenset, tuple, list)):
            value = ", ".join(sorted(map(str, value)) if not isinstance(value, (tuple, list)) else value) or "(none)"
        print(f"{key} = {value}")
    return 0


def about() -> int:
    print(f"usbkill-ng {__version__}\n"
          f"  Author:   {__author__}\n"
          f"  GitHub:   {__github__}\n"
          f"  Twitter:  {__twitter__}\n"
          f"  Project:  {__url__}\n"
          "  License:  GPL-3.0-or-later (no warranty)\n"
          "  Inspired by usbkill by Hephaestos: https://github.com/hephaest0s/usbkill")
    return 0


def cmd_pause(settings) -> int:
    path = settings.pause_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:
        print(f"[ERROR] cannot create {path}: {exc} (try sudo)", file=sys.stderr)
        return 1
    print(f"[OK] paused: triggers are logged but no action runs, and a running countdown is "
          f"cancelled.\n     Resume with: usbkill-ng --resume   (removes {path})")
    return 0


def cmd_resume(settings) -> int:
    path = settings.pause_file
    if not path.exists():
        print(f"[OK] not paused ({path} does not exist).")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        print(f"[ERROR] cannot remove {path}: {exc} (try sudo)", file=sys.stderr)
        return 1
    print("[OK] resumed: actions are active again.")
    return 0


def last_started_settings(log_file: Path, tail_bytes: int = 262144):
    """Settings dict from the newest 'settings:' line in the log, or None if there is none."""
    with open(log_file, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - tail_bytes))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        marker = " settings: "
        if marker in line:
            return parse_summary(line.split(marker, 1)[1])
    return None


def cmd_status(settings, config_path: Path, run=run_cmd, disabled_flag: Path = DISABLED_FLAG_PATH) -> int:
    active = run(["systemctl", "is-active", "usbkill-ng"]).stdout.strip() or "unknown"
    enabled = run(["systemctl", "is-enabled", "usbkill-ng"]).stdout.strip() or "unknown"
    file_summary = parse_summary(settings_summary(settings, __version__))
    print(f"usbkill-ng {__version__} status")
    print(f"  service:         {active} (at boot: {enabled})")
    print(f"  paused:          {'YES - actions suppressed' if settings.pause_file.exists() else 'no'}"
          f"  ({settings.pause_file})")
    print(f"  start blocked:   {'YES - the service will refuse to start' if disabled_flag.exists() else 'no'}"
          f"  ({disabled_flag})")
    print(f"  config file:     {config_path}")
    print("                   " + " ".join(f"{k}={file_summary[k]}" for k in ("action", "delay", "shutdown_mode", "lock_first")))
    if active != "active":
        print("  running service: not active, so there is nothing to compare")
        return 0
    try:
        started = last_started_settings(settings.log_file)
    except PermissionError:
        print(f"  running service: cannot read {settings.log_file} (run with sudo to compare)")
        return 0
    except OSError as exc:
        print(f"  running service: cannot read the log ({exc})")
        return 0
    if started is None:
        print("  running service: no 'settings:' line in the log yet (started by an older version?)")
        return 0
    print("                   started with: " + " ".join(f"{k}={started.get(k, '?')}" for k in ("action", "delay", "shutdown_mode", "lock_first")))
    differing = [k for k in COMPARE_KEYS if started.get(k) != file_summary.get(k)]
    if differing:
        detail = ", ".join(f"{k} ({started.get(k, '?')} -> {file_summary[k]})" for k in differing)
        print(f"  WARNING: the config file changed after the service started: {detail}\n"
              "           Apply it with: sudo systemctl restart usbkill-ng")
    else:
        print("  OK: the running service matches the config file.")
    return 0


def list_devices() -> int:
    try:
        devices = read_devices()
    except DeviceReadError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"{'ID':<10} {'SERIAL':<20} {'PATH':<8} DESCRIPTION")
    for d in devices:
        desc = " ".join(x for x in (d.manufacturer, d.product) if x) or "-"
        print(f"{d.usb_id:<10} {d.serial or '-':<20} {d.path:<8} {desc}")
    print("\nAdd to the config as  whitelist = vid:pid  or  vid:pid:serial  (same for ignore).")
    return 0


def confirm_live(settings, assume_yes: bool, stdin=None, ask=input) -> bool:
    """Shutdown/restart can lose unsaved work, so a live start must be confirmed.

    Every refusal is logged with its reason, so a run that does nothing is never silent.
    """
    if settings.action == "lock" or assume_yes:
        return True
    stdin = stdin or sys.stdin
    if not stdin.isatty():
        LOG.error("live %s needs confirmation but there is no terminal: nothing was started "
                  "(run it from a terminal, or add --yes)", settings.action)
        return False
    print(f"WARNING: LIVE mode. A USB change will {settings.action.upper()} this computer "
          f"({settings.shutdown_mode}, delay {settings.delay:g}s).\n"
          "Unsaved work in open programs can be lost.", file=sys.stderr)
    try:
        answer = ask("Type YES to continue: ").strip()
    except EOFError:
        answer = ""
    if answer.lower() == "yes":
        return True
    LOG.error("not confirmed (expected YES, got %r): nothing was started", answer)
    return False


def install_stop_handlers(stop: threading.Event) -> None:
    """Ctrl+C / SIGTERM request a clean stop (this also cancels a running countdown)."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())


def watch(settings, live: bool, assume_yes: bool, run) -> int:
    problems = PREFLIGHT[settings.action](settings, run)
    for problem in problems:
        if live:
            LOG.error("refusing to start: %s", problem)
        else:
            LOG.warning("%s (a --live run would refuse to start)", problem)
    if problems and live:
        return 1
    if live and not confirm_live(settings, assume_yes):
        return 1
    LOG.info("settings: %s", settings_summary(settings, __version__))
    stop = threading.Event()
    install_stop_handlers(stop)

    def trigger(changes):
        outcome = run_action(settings, dry_run=not live, run=run, stop=stop)
        if outcome.powered:
            stop.set()      # the machine is going down: stop watching

    watcher = Watcher(settings, read_devices, make_source(settings, stop), trigger, stop)
    try:
        watcher.run()
    except DeviceReadError as exc:
        LOG.error("refusing to start without a device baseline: %s", exc)
        return 1
    return 0


def main(argv=None, run=run_cmd) -> int:
    args = build_parser().parse_args(argv)
    if args.about:
        return about()
    if args.init_config:
        return init_config(args.config)
    if args.list_devices:
        return list_devices()

    lenient = (args.test_lock or args.test_action or args.watch or args.show_config
               or args.pause or args.resume or args.status)
    try:
        settings = load_settings(args.config, allow_missing=lenient)
    except ConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    if args.arm_delay is not None:
        if not 0 <= args.arm_delay <= MAX_DELAY:
            print(f"[ERROR] --arm-delay must be between 0 and {MAX_DELAY} seconds", file=sys.stderr)
            return 2
        settings = dataclasses.replace(settings, arm_delay=args.arm_delay)
    if args.show_config:
        return show_config(settings, args.config)
    if args.pause:
        return cmd_pause(settings)
    if args.resume:
        return cmd_resume(settings)
    if args.status:
        return cmd_status(settings, args.config, run)

    if args.live and os.geteuid() != 0:
        print("[ERROR] --live needs root (run with sudo).", file=sys.stderr)
        return 1

    setup_logging(settings.log_file, args.verbose, args.live)
    LOG.info("mode: %s", "LIVE" if args.live else "DRY-RUN (no actions are performed)")

    if args.test_lock:
        lock_only = dataclasses.replace(settings, action="lock", delay=0.0)
        return 0 if run_action(lock_only, dry_run=not args.live, run=run).ok else 1

    if args.test_action:
        if args.live and not confirm_live(settings, args.yes):
            return 1
        stop = threading.Event()
        install_stop_handlers(stop)
        outcome = run_action(settings, dry_run=not args.live, run=run, stop=stop)
        return 0 if (outcome.ok or outcome.cancelled) else 1

    if args.watch:
        if not args.config.exists():
            LOG.warning("no config file at %s; using built-in defaults", args.config)
        return watch(settings, args.live, args.yes, run)

    build_parser().print_usage(sys.stderr)
    print("Nothing to do: choose --watch, --status, --pause, --resume, --test-action, "
          "--test-lock, --show-config, --list-devices or --init-config.", file=sys.stderr)
    return 2
