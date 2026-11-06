# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""The watch loop: event sources (pyudev or polling) and the trigger/cooldown/re-baseline logic."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List

from .config import Settings
from .devices import Changes, Device, DeviceReadError, diff_devices

LOG = logging.getLogger("usbkill")
WAKE_TIMEOUT = 1.0   # safety-net rescan interval when using udev events


class PollSource:
    """Fallback: wake up every poll_interval seconds."""

    def __init__(self, interval: float, stop: threading.Event):
        self.interval, self.stop = interval, stop

    def wait(self, timeout: float) -> bool:
        self.stop.wait(self.interval)
        return True


class UdevSource:
    """Wake up immediately on a kernel USB device add/remove event (needs python3-pyudev)."""

    def __init__(self):
        import pyudev  # ImportError is handled by make_source
        self._monitor = pyudev.Monitor.from_netlink(pyudev.Context())
        self._monitor.filter_by("usb", device_type="usb_device")
        self._monitor.start()

    def wait(self, timeout: float) -> bool:
        return self._monitor.poll(timeout=timeout) is not None


def make_source(settings: Settings, stop: threading.Event):
    try:
        source = UdevSource()
        LOG.info("event source: udev (pyudev), with a %.0fs safety rescan", WAKE_TIMEOUT)
        return source
    except Exception as exc:  # ImportError, OSError, ...
        LOG.warning("pyudev unavailable (%s); polling every %.2fs instead", exc, settings.poll_interval)
        return PollSource(settings.poll_interval, stop)


class Watcher:
    def __init__(self, settings: Settings, read: Callable[[], List[Device]], source,
                 trigger: Callable[[Changes], None], stop: threading.Event,
                 clock: Callable[[], float] = time.monotonic):
        self.settings, self.read, self.source = settings, read, source
        self.trigger, self.stop, self.clock = trigger, stop, clock
        self.exempt = frozenset(settings.whitelist) | frozenset(settings.ignore)

    def _log_changes(self, changes: Changes) -> None:
        for d in changes.added:
            LOG.warning("USB ADDED:   %s", d.describe())
        for d in changes.removed:
            LOG.warning("USB REMOVED: %s", d.describe())

    def run(self) -> None:
        baseline = self.read()   # a failure here propagates: we refuse to start blind
        grace = self.settings.arm_delay
        armed_at = self.clock() + grace
        armed = grace <= 0
        LOG.info("baseline: %d device(s); watching (whitelist=%d, ignore=%d)",
                 len(baseline), len(self.settings.whitelist), len(self.settings.ignore))
        if not armed:
            LOG.info("arming in %gs: changes during this grace period only refresh the baseline", grace)
        while not self.stop.is_set():
            woke_on_event = self.source.wait(WAKE_TIMEOUT)
            if self.stop.is_set():
                break
            if woke_on_event and self.settings.debounce > 0:
                self.stop.wait(self.settings.debounce)   # let enumeration settle
            try:
                current = self.read()
            except DeviceReadError as exc:
                LOG.warning("skipping this check: %s", exc)   # unknown is not a trigger
                continue
            if not armed:
                baseline = current            # grace period: refresh, never trigger
                if self.clock() >= armed_at:
                    armed = True
                    LOG.info("armed: now watching for USB changes (%d device(s))", len(baseline))
                continue
            changes = diff_devices(baseline, current, self.exempt)
            if not changes:
                continue
            self._log_changes(changes)
            if self.settings.pause_file.exists():
                LOG.warning("PAUSED (%s exists): action suppressed", self.settings.pause_file)
            else:
                self.trigger(changes)
            self.stop.wait(self.settings.cooldown)
            try:
                baseline = self.read()
                LOG.info("new baseline: %d device(s)", len(baseline))
            except DeviceReadError as exc:
                LOG.warning("could not re-baseline (%s); keeping the old one", exc)
        LOG.info("watcher stopped")
