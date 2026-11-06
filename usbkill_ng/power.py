# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""Shutdown / restart actions, the countdown timer, and the orchestration of one trigger.

Safety rules:
  * Dry-run never executes a power command and never actually waits out a delay.
  * Every power action has a fallback chain; if the whole chain fails we fall back to LOCK,
    so a failure never leaves the machine open.
  * The countdown is cancelled only by Ctrl+C or the pause file, never by unplugging the device.
"""
from __future__ import annotations

import logging
import math
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .actions import RunFn, Step, lock, lock_preflight, run_cmd
from .config import Settings

LOG = logging.getLogger("usbkill")
_VERB = {"shutdown": "poweroff", "restart": "reboot"}


def _chains(action: str, mode: str) -> Tuple[list, list]:
    """(prelude commands, ordered fallback chain) as argv lists."""
    verb = _VERB[action]
    systemd = ["systemctl", verb]
    if mode == "graceful":
        # 2nd entry overrides inhibitor locks (root only); 3rd is the forced last resort
        return [], [systemd, systemd + ["-i"], [verb, "-f"]]
    if mode == "fast":
        return [], [systemd + ["-f"], [verb, "-f"]]
    return [["sync"]], [systemd + ["-f", "-f"], [verb, "-f", "-f"]]   # immediate


def plan_power(action: str, mode: str, which: Optional[Callable] = None) -> Tuple[List[Step], List[Step]]:
    """Pure function: builds the prelude and fallback chain, runs nothing."""
    which = which or shutil.which
    prelude, chain = _chains(action, mode)

    def to_step(argv):
        reason = None if which(argv[0]) else f"{argv[0]} not found"
        return Step(" ".join(argv), tuple(argv), reason)

    return [to_step(a) for a in prelude], [to_step(a) for a in chain]


def countdown(delay: float, stop: threading.Event, pause_file: Path, dry_run: bool) -> bool:
    """Wait `delay` seconds. True if it completed, False if it was cancelled."""
    if delay <= 0:
        return True
    if dry_run:
        LOG.info("DRY-RUN would wait %gs before the action (not waiting)", delay)
        return True
    LOG.warning("action in %gs - cancel with Ctrl+C or by creating %s", delay, pause_file)
    deadline = time.monotonic() + delay
    last_logged = math.ceil(delay)       # the opening message already covers this second
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if stop.is_set():
            LOG.warning("countdown cancelled: stop requested")
            return False
        if pause_file.exists():
            LOG.warning("countdown cancelled: %s exists", pause_file)
            return False
        whole = math.ceil(remaining)
        if whole != last_logged and (whole <= 3 or whole % 5 == 0):
            LOG.warning("action in %ds", whole)
            last_logged = whole
        stop.wait(min(0.2, remaining))


def execute_chain(prelude: List[Step], chain: List[Step], dry_run: bool, run: RunFn = run_cmd) -> bool:
    """Run the first working command in the chain. True if one succeeded (or would, in dry-run)."""
    for step in chain:
        if step.skip_reason:
            LOG.warning("SKIP %s: %s", step.name, step.skip_reason)
    usable = [s for s in chain if not s.skip_reason]
    if not usable:
        return False
    if dry_run:
        for s in prelude:
            LOG.info("DRY-RUN would run first: %s", s.name)
        fallbacks = f" (fallbacks: {'; '.join(s.name for s in usable[1:])})" if usable[1:] else ""
        LOG.info("DRY-RUN would run [%s]%s", usable[0].name, fallbacks)
        return True
    for s in prelude:
        run(s.argv, timeout=10)           # 'sync': best effort, result ignored
    for s in usable:
        result = run(s.argv, timeout=30)
        if result.returncode == 0:
            LOG.info("ran [%s] ok", s.name)
            return True
        LOG.error("[%s] failed (exit %s): %s", s.name, result.returncode, result.stderr.strip())
    return False


@dataclass(frozen=True)
class Outcome:
    ok: bool
    cancelled: bool = False
    powered: bool = False      # a real shutdown/restart command was accepted (never true in dry-run)


def run_action(settings: Settings, dry_run: bool = True, run: RunFn = run_cmd,
               stop: Optional[threading.Event] = None, which: Optional[Callable] = None,
               euid: Optional[int] = None) -> Outcome:
    """Carry out settings.action for one trigger: [lock now] -> countdown -> action -> fallback."""
    stop = stop or threading.Event()
    which = which or shutil.which

    if settings.action == "lock":
        if not countdown(settings.delay, stop, settings.pause_file, dry_run):
            return Outcome(False, cancelled=True)
        return Outcome(lock(settings, dry_run, run, euid))

    prelude, chain = plan_power(settings.action, settings.shutdown_mode, which)
    lock_thread = None
    if settings.lock_first:
        lock_thread = threading.Thread(target=lock, args=(settings, dry_run, run, euid), daemon=True)
        lock_thread.start()
        if dry_run:
            lock_thread.join()            # keep dry-run log output in a readable order

    if not countdown(settings.delay, stop, settings.pause_file, dry_run):
        if lock_thread:
            lock_thread.join(15)
        return Outcome(False, cancelled=True)

    ok = execute_chain(prelude, chain, dry_run, run)
    if lock_thread:
        lock_thread.join(3)
    if not ok:
        LOG.error("no %s command worked; falling back to lock", settings.action)
        if lock_thread is None:
            lock(settings, dry_run, run, euid)
    return Outcome(ok, powered=ok and not dry_run)


def power_preflight(settings: Settings, run: RunFn = run_cmd, euid: Optional[int] = None,
                    which: Optional[Callable] = None) -> list:
    """Problems that would stop the power action from working. Empty list means ready."""
    _, chain = plan_power(settings.action, settings.shutdown_mode, which)
    problems = []
    if all(s.skip_reason for s in chain):
        problems.append("no usable power command: " + "; ".join(s.skip_reason for s in chain))
    if settings.lock_first:
        for problem in lock_preflight(settings, run, euid):
            LOG.warning("lock_first is on but %s", problem)
    return problems
