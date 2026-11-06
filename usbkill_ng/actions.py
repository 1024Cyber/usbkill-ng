# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""Action framework. Module 2 implements only the 'lock' action.

Design rules:
  * No shell strings. Every command is an argv list, so paths/usernames can't inject anything.
  * Planning and executing are separate. Dry-run builds the plan and logs it; it never executes.
  * Nothing about the user is hardcoded: the desktop user comes from the active logind session.
"""
from __future__ import annotations

import logging
import os
import pwd
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Settings

LOG = logging.getLogger("usbkill")
RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


def run_cmd(argv, timeout: float = 5) -> "subprocess.CompletedProcess[str]":
    """Run argv without a shell. Never raises for missing programs or timeouts."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timed out")


@dataclass(frozen=True)
class GraphicalSession:
    session_id: str
    user: str
    uid: int
    display: str
    home: Path


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple
    skip_reason: Optional[str] = None


def parse_props(text: str) -> dict:
    props = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def find_graphical_session(run: RunFn = run_cmd) -> Optional[GraphicalSession]:
    """Return the active x11/wayland user session known to systemd-logind, if any."""
    listing = run(["loginctl", "list-sessions", "--no-legend"])
    if listing.returncode != 0:
        return None
    for line in listing.stdout.splitlines():
        if not line.split():
            continue
        sid = line.split()[0]
        shown = run(["loginctl", "show-session", sid, "-p", "Name", "-p", "User",
                     "-p", "Type", "-p", "Active", "-p", "Class", "-p", "Display"])
        props = parse_props(shown.stdout)
        if (props.get("Active") == "yes" and props.get("Class") == "user"
                and props.get("Type") in ("x11", "wayland")):
            try:
                uid = int(props["User"])
                home = Path(pwd.getpwuid(uid).pw_dir)
            except (KeyError, ValueError):
                continue
            return GraphicalSession(sid, props.get("Name", ""), uid, props.get("Display") or ":0", home)
    return None


def plan_lock(methods, session: Optional[GraphicalSession], euid: int,
              which: Optional[Callable[[str], Optional[str]]] = None) -> list:
    """Build the list of lock steps. Pure function: runs nothing."""
    which = which or shutil.which  # resolved at call time so it can be substituted
    steps = []
    for method in methods:
        if method == "loginctl":
            if which("loginctl"):
                steps.append(Step("loginctl", ("loginctl", "lock-sessions")))
            else:
                steps.append(Step("loginctl", (), "loginctl not found"))
        elif method == "xflock4":
            if session is None:
                steps.append(Step("xflock4", (), "no active graphical session"))
            elif not which("xflock4"):
                steps.append(Step("xflock4", (), "xflock4 not found"))
            elif euid == 0 and not which("runuser"):
                steps.append(Step("xflock4", (), "runuser not found"))
            elif euid not in (0, session.uid):
                steps.append(Step("xflock4", (), "running as a different non-root user than the desktop"))
            else:
                runtime = f"/run/user/{session.uid}"
                env = [f"DISPLAY={session.display}",
                       f"XAUTHORITY={session.home / '.Xauthority'}",
                       f"XDG_RUNTIME_DIR={runtime}",
                       f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus"]
                inner = ["env", *env, "xflock4"]
                argv = ["runuser", "-u", session.user, "--", *inner] if euid == 0 else inner
                steps.append(Step("xflock4", tuple(argv)))
    return steps


def execute_steps(steps, dry_run: bool, run: RunFn = run_cmd) -> bool:
    """Execute (or, in dry-run, only log) the steps. True if any step ran/would run OK.

    Live steps run in parallel so a slow method (xflock4 can take ~2s) never delays a fast one.
    """
    runnable = []
    for step in steps:
        if step.skip_reason:
            LOG.warning("SKIP %s: %s", step.name, step.skip_reason)
        else:
            runnable.append(step)
    if not runnable:
        return False
    if dry_run:
        for step in runnable:
            LOG.info("DRY-RUN would run [%s]: %s", step.name, " ".join(shlex.quote(a) for a in step.argv))
        return True
    ok = False
    with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
        futures = [(s, pool.submit(run, s.argv, timeout=10)) for s in runnable]
        for step, future in futures:
            result = future.result()
            if result.returncode == 0:
                LOG.info("ran [%s] ok", step.name)
                ok = True
            else:
                LOG.error("[%s] failed (exit %s): %s", step.name, result.returncode, result.stderr.strip())
    return ok


def lock(settings: Settings, dry_run: bool = True, run: RunFn = run_cmd,
         euid: Optional[int] = None) -> bool:
    session = find_graphical_session(run)
    if session:
        LOG.info("desktop session %s: user=%s uid=%d display=%s",
                 session.session_id, session.user, session.uid, session.display)
    else:
        LOG.warning("no active graphical session found")
    steps = plan_lock(settings.lock_methods, session, os.geteuid() if euid is None else euid)
    return execute_steps(steps, dry_run, run)


def lock_preflight(settings: Settings, run: RunFn = run_cmd, euid: Optional[int] = None) -> list:
    """Problems that would make the lock action do nothing. Empty list means ready."""
    steps = plan_lock(settings.lock_methods, find_graphical_session(run),
                      os.geteuid() if euid is None else euid)
    if any(not s.skip_reason for s in steps):
        return []
    return ["no usable lock method: " + "; ".join(f"{s.name}: {s.skip_reason}" for s in steps)]


ACTIONS = {"lock": lock}
PREFLIGHT = {"lock": lock_preflight}
