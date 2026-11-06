# usbkill-ng

By 1024cyber (https://github.com/1024cyber) | [Twitter](https://twitter.com/1024cyber)

Lock, shut down or restart a Linux computer when a USB device is plugged in or removed.

usbkill-ng watches your USB ports. When a device is added or removed, it can lock the screen, shut
the computer down, or restart it, optionally after a countdown you can cancel. You choose in one
config file. It runs from the command line or as a systemd service.

It is a rewrite of [usbkill](https://github.com/hephaest0s/usbkill) by Hephaestos, which has not
been updated since 2016 and no longer runs cleanly on current systems. It is **Linux only for now**;
a Windows version is planned.

> **Use it only on computers you own or are authorised to administer.** It can power off or
> restart the computer, and unsaved work can be lost.

## Quick start

Download or clone this repository, then from its folder:

```
python3 -m usbkill_ng --list-devices
```
Lists the USB devices connected right now. Read-only.

```
python3 -m usbkill_ng --watch
```
Starts the watcher in dry-run mode: it logs what it *would* do and performs nothing. Plug a USB
stick in and out and read the log lines. Stop it with Ctrl+C.

```
sudo python3 -m usbkill_ng --live --watch
```
The real thing, with the default action (`lock`): plugging or unplugging a device locks the
screen. Stop it with Ctrl+C. To change the action, set a timer, or run it as a service, see
[Install](#install) and [Configuration](#configuration-etcusbkillusbkillini).

## Key Concepts

- **Baseline** - the USB devices present when the watcher starts, or after a trigger and
  its cooldown. A device added to or missing from the baseline is a change.
- **Trigger** - a change that is not whitelisted or ignored. It runs the configured action.
- **Action** - `lock`, `shutdown` or `restart`.
- **Delay** - seconds between a trigger and the action. The screen can lock at once while
  the delay counts down (`lock_first`).
- **Cooldown** - seconds after an action before a fresh baseline is taken.
- **Arm delay** - a grace period after the watcher starts during which nothing can trigger.
- **Dry-run** - the default. The program logs what it *would* do and performs nothing.
  Only `--live` performs actions, and it needs root.
- **Pause file** - `/etc/usbkill/pause`. While it exists, triggers are logged but no action
  runs, and a running countdown is cancelled.
- **Disabled file** - `/etc/usbkill/disabled`. While it exists, the systemd service refuses
  to start (boot-loop protection).
- **sysfs / udev** - the kernel's device list and device events. The watcher reads sysfs and
  wakes on udev events (via `pyudev`) or by polling.

## Requirements

- Linux with systemd (tested on Kali, Xfce desktop), Python 3.8 or newer
- Root, for live mode
- `loginctl` (systemd-logind) for locking; `xflock4` is used on Xfce
- Optional: `python3-pyudev` for instant event detection (otherwise it polls 4 times a second)
- Optional: `logrotate` (a rule is installed to keep the log from growing forever)

## Install

From the unpacked folder:

```
sudo ./install.sh --dry-run
```
Shows every step without changing anything.

```
sudo ./install.sh
```
Copies the program to `/opt/usbkill-ng`, adds the `usbkill-ng` command, installs the
systemd unit, a logrotate rule, and creates `/etc/usbkill/usbkill.ini` (an existing config is kept, and the new
template is written next to it as `usbkill.ini.dist`). It does **not** enable or start the service.

```
sudo ./uninstall.sh
```
Undoes the install. Your config and logs are kept. Add `--purge` to delete those as well.

You can also run it without installing: `cd` into the folder and use `python3 -m usbkill_ng`.

## Configuration: `/etc/usbkill/usbkill.ini`

| Key | Default | Meaning |
|---|---|---|
| `action` | `lock` | `lock`, `shutdown` or `restart` |
| `delay` | `0` | Seconds to wait after a trigger before the action (0-300) |
| `shutdown_mode` | `graceful` | `graceful`, `fast` or `immediate` (shutdown/restart only) |
| `lock_first` | `true` | Lock the screen at the instant of the trigger, before the delay |
| `cooldown` | `5` | Seconds after an action before the new baseline is taken |
| `arm_delay` | `0` | Grace period after start (0-300). The service uses 20 |
| `debounce` | `0.3` | Wait after a USB event so the device finishes enumerating |
| `poll_interval` | `0.25` | Polling interval when `pyudev` is not installed |
| `lock_methods` | `loginctl, xflock4` | All listed methods run, in parallel |
| `whitelist` | empty | Devices that may come and go: `vid:pid` or `vid:pid:serial` |
| `ignore` | empty | Same effect as whitelist; for built-in hardware, to keep the file readable |
| `pause_file` | `/etc/usbkill/pause` | See Pause file above |
| `log_file` | `/var/log/usbkill/usbkill.log` | Root-readable log |

Shutdown modes: **graceful** is a normal systemd shutdown (safest for data). **fast** kills
processes and unmounts or remounts read-only without stopping services one by one.
**immediate** syncs, then powers off with no unmount; unsaved data can be lost.

Every power action has a fallback chain (for example `systemctl poweroff`, then
`systemctl poweroff -i`, then `poweroff -f`). If every command fails, it locks the screen, so
a failure never leaves the machine open.

## Commands

| Command | What it does |
|---|---|
| `usbkill-ng --list-devices` | Show connected USB devices with ids for the whitelist and ignore lists |
| `usbkill-ng --status` | Service state, pause state, and whether the running service still matches the config file |
| `usbkill-ng --about` | Show the author, links, license and credits |
| `usbkill-ng --pause` / `--resume` | Create / remove the pause file (needs sudo): triggers are logged, no action runs |
| `usbkill-ng --show-config` | Print the settings that will actually be used (from the file, not from the running service) |
| `usbkill-ng --watch` | Dry-run watcher: logs what it would do |
| `sudo usbkill-ng --live --watch` | The real watcher (asks you to type YES for shutdown/restart) |
| `sudo usbkill-ng --live --watch --yes` | Same, without the question (what the service uses) |
| `usbkill-ng --test-action` | Run the configured action once, no USB event (dry-run unless `--live`) |
| `usbkill-ng --test-lock` | Run only the lock action, no delay |
| `usbkill-ng --arm-delay N` | Override `arm_delay` for this run |
| `usbkill-ng --init-config` | Write a default config (never overwrites) |

Without `--live` nothing is ever performed.

## Test it safely, in this order

1. `usbkill-ng --show-config` - check the settings.
2. `usbkill-ng --watch` (dry-run) - plug and unplug a stick; read the log lines.
3. `sudo usbkill-ng --live --watch` with `action = lock` - the screen should lock.
4. Set `action = restart`, `delay = 10`. Run `sudo usbkill-ng --live --test-action`, and cancel
   with Ctrl+C or the pause file to test the cancel. Then let it finish to test the restart.
5. Repeat with `action = shutdown`. Last, try `shutdown_mode = fast`.
6. Only then start the service. **Save your work before every live shutdown or restart test.**

## Run it as a service

```
sudo systemctl start usbkill-ng
```
Starts it now, not at boot. Stop it with `sudo systemctl stop usbkill-ng`.

```
sudo systemctl status usbkill-ng
```
Shows whether it is running. Logs are in `/var/log/usbkill/usbkill.log` and `journalctl -u usbkill-ng`.

```
sudo systemctl enable usbkill-ng
```
Starts it at every boot. Undo: `sudo systemctl disable usbkill-ng`.

The unit starts with `--arm-delay 20`, so devices that finish enumerating late during boot
cannot trigger a shutdown.

## Pause or stop while it is running

| Goal | Command | Undo |
|---|---|---|
| Keep running but suppress actions, and cancel a running countdown | `sudo usbkill-ng --pause` | `sudo usbkill-ng --resume` |
| Stop the service (also cancels a countdown) | `sudo systemctl stop usbkill-ng` | `sudo systemctl start usbkill-ng` |
| Do not start at boot | `sudo systemctl disable usbkill-ng` | `sudo systemctl enable usbkill-ng` |
| Block it from starting at all (recovery switch) | `sudo touch /etc/usbkill/disabled` | `sudo rm /etc/usbkill/disabled` |

`--pause` must be in place before the USB event: the pause file is checked when a trigger
happens and during a countdown. **The countdown (`delay`) is the only cancel window.** Once it
ends and the shutdown or restart command has been handed to systemd, nothing can reliably take it
back, and with `delay = 0` there is no window at all.

After editing the config, restart the service (`sudo systemctl restart usbkill-ng`): it reads the
file only at start. `usbkill-ng --status` warns when the file and the running service disagree.

## Recovery

- **Stop it now:** `sudo systemctl stop usbkill-ng`, or Ctrl+C in a terminal.
- **Cancel a countdown or suppress actions:** `sudo usbkill-ng --pause` (undo with `sudo usbkill-ng --resume`).
- **Stuck in a shutdown/restart loop at boot:** boot to recovery mode or a live USB, mount the
  disk, and create the file `etc/usbkill/disabled` on it. The service then refuses to start.
  Remove the file later to re-enable it. You can also run `systemctl disable usbkill-ng` from
  a root shell.
- **Service behaves like an old config:** `usbkill-ng --status` shows what it started with; the log's `settings:` line records it too. Then `sudo systemctl restart usbkill-ng`.
- **Config errors:** `usbkill-ng --show-config` prints the exact problem.

## Logs

`/var/log/usbkill/usbkill.log` is root-readable only. At every start the watcher logs a
`settings:` line with the action, delay and other values it is really using. A logrotate rule
rotates the log weekly and keeps 8 compressed copies.

## Limits to know about

- **It reacts to USB devices, not to what they do.** Matching by vendor:product is easy to
  spoof; use `vid:pid:serial` where possible, and note that some devices have no serial.
- **SD cards are not seen.** Inserting a card into a built-in reader does not add or remove a
  USB device, because the reader controller stays connected.
- **A screen lock is not a security boundary** against someone who already has a running
  session or process. Shutdown is what protects data, and **only if the disk is encrypted**.
- **The cooldown window.** After a trigger, whatever is plugged in at the end of the cooldown
  becomes the new baseline.
- **Unplugging does not cancel a countdown.** Otherwise plug-then-unplug would defeat it.
- **Removed from the original usbkill:** file shredding, RAM and swap wiping, and
  self-deleting. They are the riskiest features and are not carried over.
- **Linux only.** A Windows backend is planned as a separate phase.

## Troubleshooting

- **Nothing happens after `mode: LIVE`:** for shutdown/restart you must type `YES` (any case),
  or add `--yes`. A refused prompt is logged with the reason.
- **Triggers are logged but nothing happens:** a pause file exists (`ls /etc/usbkill/`).
- **`No module named usbkill_ng`:** run from the folder that contains `usbkill_ng/`, or use the
  installed `usbkill-ng` command.
- **Screen does not lock:** run `usbkill-ng --test-lock` and read the log; `xfce4-screensaver`
  should be installed on Xfce.
- **False triggers from built-in hardware:** run `usbkill-ng --list-devices` and add that
  device to `ignore`. Keep the list short.

## Why a rewrite

The original usbkill has the right idea, but an audit of its source found problems that make it
unreliable on a current system:

| Severity | Finding |
|---|---|
| High | `--no-shut-down` is not a dry-run: it still runs file shredding, `kill_commands` and the RAM/swap wipe, and with `melt_usbkill` it deletes the program. |
| High | The program exits after the first trigger, so it cannot support an action like lock that must keep watching. |
| High | It polls `lsusb` through a shell four times a second, so a plug-and-unplug between polls is missed. |
| High | The first-run settings copy looks in the wrong folder and is missing a `/`, so a fresh install cannot start. |
| Medium | A dict-style whitelist entry crashes on Python 3 (`dict_values` is not subscriptable). |
| Medium | `DeviceCountSet.__add__` returns a plain dict, and its list branch compares a type to strings so it never runs. |
| Medium | Startup error paths raise `NameError`, and three different errors all say "srm not installed". |
| Medium | Paths are passed to `os.system()` unquoted. |
| Low | `setup.py` uses `distutils`, removed from the standard library in Python 3.12. |
| Low | `#!/usr/bin/env python` shebang, invalid-escape warnings, a log folder with default permissions, and docs and paths that disagree. |

The original's open issues and pull requests were not reviewed. **Removed on purpose:** file
shredding, RAM and swap wiping, and self-delete. They are the riskiest features and are not carried over.

## Design decisions

1. **Dry-run is the default.** Without `--live` nothing is performed, and a delay is logged, not waited.
2. **Plan, then execute.** Every command is an argument list (no shell). Building the plan and running it are separate steps.
3. **sysfs plus udev.** Devices are read from `/sys/bus/usb/devices`; `pyudev` events wake the watcher, with a polling fallback. A sysfs read failure is "unknown", never "all devices removed".
4. **Baseline and diff.** A trigger is a device added or removed that is not whitelisted or ignored. After an action and the cooldown, the current devices become the new baseline.
5. **Matching** is `vid:pid` or the stricter `vid:pid:serial`.
6. **Lock methods all run, in parallel,** so a slow `xflock4` (about 2 s) never delays `loginctl`.
7. **The desktop user is found from the active logind session,** never hardcoded.
8. **Power actions have fallback chains and end in a lock,** so a failure never leaves the machine open.
9. **Lock first.** The screen locks at the instant of the trigger while the delay counts down or a graceful shutdown proceeds.
10. **Unplugging does not cancel a countdown;** only Ctrl+C, `systemctl stop` or the pause file does.
11. **Live shutdown and restart must be confirmed** (`YES`, any case) unless `--yes` is given; every refusal is logged.
12. **Boot safety.** The service has a grace period (`--arm-delay 20`), refuses to start if `/etc/usbkill/disabled` exists, and is installed but not enabled. The unit has no `After=multi-user.target`, which could form an ordering loop.
13. **No silent stale config.** The service logs a `settings:` line at start, and `--status` compares it with the config file.
14. **Logs rotate** weekly through logrotate with `copytruncate`, so the running service needs no restart.
15. **Licensing.** GPL-3.0-or-later with credit to the original author.

## Test status

86 automated tests run without hardware, using a fake command runner and a fake sysfs folder. Real
hardware testing so far is one machine: **Kali Linux, Xfce desktop, bare metal.**

| Capability | Status |
|---|---|
| Lock on USB plug and unplug; both lock methods (`loginctl`, `xflock4`) | Tested on hardware |
| Restart after a 10 s countdown, lock first | Tested on hardware |
| Cancel a countdown with Ctrl+C inside `--watch` | Tested on hardware |
| USB plug leading to lock and then restart, through the running service | Tested on hardware |
| `action = shutdown` | Tested on hardware |
| `install.sh` and the systemd service running | Tested on hardware |
| `--pause`, `--resume`, `--status`, the `settings:` startup line | Automated tests only |
| Cancel a countdown with the pause file | Automated tests only |
| `arm_delay` during a real boot | Automated tests only |
| `fast` and `immediate` shutdown modes; the fallback chain | Automated tests only |
| `pyudev` event source; whitelist and ignore with real devices | Automated tests only |
| logrotate rule | Content-checked only (not validated by `logrotate`) |
| Service enabled at boot, then a reboot | Not yet tested |

**Known, unexplained:** once, right after a reboot, `systemctl start usbkill-ng` appeared to hang.
The cause was not established, and the service has run normally since. The ordering line most
likely responsible (`After=multi-user.target`) has been removed, but that fix is untested on hardware.

Only Kali with Xfce has been tried. Other desktops should work through `loginctl`, but are untested.

## Roadmap

- Windows backend (detection through WMI or the device notification API; `shutdown /s /t 0`; a service).
- A desktop notification during the countdown.
- Detecting SD-card insertion through block-device events.
- Different actions for plug and for unplug.
- A `--learn` mode that builds the whitelist from the devices currently connected.
- Reloading the config without a restart.
- `.deb` or `pipx` packaging.

## Development

```
python3 -m unittest discover -v
```
Runs the test suite (no hardware needed; power commands are faked).

## Author

Innocent I.

- GitHub: (https://github.com/1024cyber)
- Twitter: (https://twitter.com/1024cyber)

Questions and bug reports: the [Issues page](https://github.com/1024cyber/usbkill-ng/issues) of this repository.

## License and credits

Copyright (C) 2026 Innocent I..

GPL-3.0-or-later (see `LICENSE` and `NOTICE`). usbkill-ng is an independent rewrite inspired by
[usbkill](https://github.com/hephaest0s/usbkill) by Hephaestos, which is also GPL-3.0-or-later.
It comes with no warranty. Use it only on computers you own or are authorised to administer.
