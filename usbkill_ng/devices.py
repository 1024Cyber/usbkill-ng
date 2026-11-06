# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
"""USB device discovery via sysfs, and the baseline/diff logic.

Safety rule: if sysfs cannot be listed at all we raise DeviceReadError. We never return an
empty list for a read failure, because "everything vanished" would look like a mass removal
and fire the action by mistake.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

SYSFS_USB = Path("/sys/bus/usb/devices")


class DeviceReadError(Exception):
    """sysfs could not be read; the caller must treat this as 'unknown', not 'no devices'."""


@dataclass(frozen=True)
class Device:
    path: str            # sysfs name, e.g. "1-4" (bus 1, port 4) or "usb1" (root hub)
    vid: str
    pid: str
    serial: str = ""
    manufacturer: str = ""
    product: str = ""

    @property
    def usb_id(self) -> str:
        return f"{self.vid}:{self.pid}"

    @property
    def identity(self) -> tuple:
        return (self.path, self.vid, self.pid, self.serial)

    def describe(self) -> str:
        label = " ".join(x for x in (self.manufacturer, self.product) if x) or "(no description)"
        serial = f" serial={self.serial}" if self.serial else ""
        return f"{self.usb_id} at {self.path}{serial} - {label}"


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def read_devices(root: Path = SYSFS_USB) -> List[Device]:
    """Return every USB *device* (not interface) currently in sysfs."""
    try:
        entries = sorted(Path(root).iterdir())
    except OSError as exc:
        raise DeviceReadError(f"cannot list {root}: {exc}") from exc
    devices = []
    for entry in entries:
        if ":" in entry.name:        # "1-4:1.0" is an interface of a device, not a device
            continue
        vid, pid = _read(entry / "idVendor").lower(), _read(entry / "idProduct").lower()
        if not vid or not pid:       # device vanished mid-read or not a USB device node
            continue
        devices.append(Device(entry.name, vid, pid, _read(entry / "serial"),
                              _read(entry / "manufacturer"), _read(entry / "product")))
    return devices


def matches(device: Device, entry: str) -> bool:
    """entry is 'vid:pid' or 'vid:pid:serial' (already lower-cased by the config loader)."""
    parts = entry.split(":", 2)
    if f"{parts[0]}:{parts[1]}" != device.usb_id:
        return False
    return len(parts) == 2 or parts[2] == device.serial.lower()


def is_exempt(device: Device, exempt: Iterable[str]) -> bool:
    return any(matches(device, entry) for entry in exempt)


@dataclass(frozen=True)
class Changes:
    added: tuple = ()
    removed: tuple = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed)


def diff_devices(baseline: Iterable[Device], current: Iterable[Device], exempt=frozenset()) -> Changes:
    """Devices added/removed since the baseline, ignoring any that match an exempt entry."""
    base = {d.identity: d for d in baseline}
    cur = {d.identity: d for d in current}
    added = sorted((d for k, d in cur.items() if k not in base and not is_exempt(d, exempt)),
                   key=lambda d: d.path)
    removed = sorted((d for k, d in base.items() if k not in cur and not is_exempt(d, exempt)),
                     key=lambda d: d.path)
    return Changes(tuple(added), tuple(removed))
