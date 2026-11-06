# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from usbkill_ng import __version__
from usbkill_ng.cli import main


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_version_is_set(self):
        self.assertTrue(__version__)
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_no_args_explains(self):
        code, _, err = run_cli("--config", "/nonexistent/x.ini")
        self.assertEqual(code, 2)

    def test_init_config_never_overwrites(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "usbkill.ini"
        self.assertEqual(run_cli("--init-config", "--config", str(path))[0], 0)
        self.assertTrue(path.exists())
        self.assertEqual(run_cli("--init-config", "--config", str(path))[0], 1)

    def test_list_devices_runs(self):
        code, out, err = run_cli("--list-devices")
        self.assertIn(code, (0, 1))          # 1 only if this machine has no sysfs usb tree
        if code == 0:
            self.assertIn("DESCRIPTION", out)


if __name__ == "__main__":
    unittest.main()
