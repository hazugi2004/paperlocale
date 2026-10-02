"""Real CLI descendants must stop before timeout recovery or cancellation returns."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paperlocale.domains import load_domain_pack
from paperlocale.providers import CodexLocalProvider, Segment, TranslationContext
from paperlocale.providers.codex_local import _run_codex


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class CodexProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.marker = self.root / "child-completed"
        self.ready = self.root / "child-ready"
        self.parent_pid = self.root / "parent-pid"
        self.executable = self.root / "fake-codex"
        self.context = TranslationContext("en", "zh-CN", load_domain_pack("atmospheric-science"))

    def make_cli(self, *, exit_parent=False):
        child = (
            "import os, time; from pathlib import Path; "
            f"Path({str(self.ready)!r}).write_text(str(os.getpid())); "
            "time.sleep(3); "
            f"Path({str(self.marker)!r}).write_text('child survived')"
        )
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import os, subprocess, sys, time\nfrom pathlib import Path\n"
            f"Path({str(self.parent_pid)!r}).write_text(str(os.getpid()))\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            + ("" if exit_parent else "time.sleep(5)\n")
        )
        self.executable.chmod(0o700)

    def translate(self):
        return CodexLocalProvider(codex_bin=self.executable, timeout_seconds=2).translate(
            [Segment("probe", "Soil moisture.")], self.context
        )

    def assert_stopped(self):
        self.assertTrue(self.ready.exists(), "the fixture must actually spawn its child")
        # Past the child's scheduled side effect, but never an indefinite leaked fixture.
        time.sleep(3.2)
        self.assertFalse(self.marker.exists(), "a descendant kept running after cleanup")
        # The direct child must have been reaped, not merely signalled.
        with self.assertRaises(ChildProcessError):
            os.waitpid(int(self.parent_pid.read_text()), os.WNOHANG)

    def test_timeout_stops_parent_and_child(self):
        self.make_cli()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.translate()
        self.assert_stopped()

    def test_timeout_stops_child_after_leader_exits_with_inherited_pipes(self):
        self.make_cli(exit_parent=True)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.translate()
        self.assertLess(time.monotonic() - started, 2.8)
        self.assert_stopped()
    def test_cleanup_does_not_kill_unrelated_process(self):
        self.make_cli()
        unrelated_marker = self.root / "unrelated-completed"
        code = f"import time; from pathlib import Path; time.sleep(3); Path({str(unrelated_marker)!r}).write_text('ok')"
        with subprocess.Popen([sys.executable, "-c", code]) as unrelated:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.translate()
            self.assertEqual(unrelated.wait(timeout=3), 0)
        self.assertTrue(unrelated_marker.exists())
        self.assert_stopped()

    def test_sigint_cancellation_stops_cli_descendant(self):
        self.make_cli()
        code = (
            "from paperlocale.providers import CodexLocalProvider, Segment, TranslationContext\n"
            "from paperlocale.domains import load_domain_pack\n"
            "try:\n"
            f" CodexLocalProvider(codex_bin={str(self.executable)!r}, timeout_seconds=10).translate("
            "[Segment('probe', 'Soil moisture.')], TranslationContext('en', 'zh-CN', load_domain_pack('atmospheric-science')))\n"
            "except KeyboardInterrupt:\n"
            " raise SystemExit(42)\n"
        )
        with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as caller:
            try:
                deadline = time.monotonic() + 5
                while not self.ready.exists() and caller.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.ready.exists(), "CLI child did not start")
                caller.send_signal(signal.SIGINT)
                _stdout, stderr = caller.communicate(timeout=5)
                self.assertEqual(caller.returncode, 42, stderr.decode())
            finally:
                if caller.poll() is None:
                    caller.kill()
                    caller.wait()
        self.assert_stopped()


class CodexProcessContractTests(unittest.TestCase):
    def test_unsupported_platform_is_rejected_before_launch(self):
        with patch("paperlocale.providers.codex_local.os.name", "nt"), \
                patch("paperlocale.providers.codex_local.subprocess.Popen") as launch:
            with self.assertRaisesRegex(NotImplementedError, "POSIX"):
                _run_codex(["codex", "--version"], timeout=1)
            launch.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
    def test_completed_call_preserves_utf8_input_output_and_exit_code(self):
        result = _run_codex(
            [sys.executable, "-c", "import sys; print(sys.stdin.read()); print('diagnostic', file=sys.stderr); sys.exit(7)"],
            input="土壤湿度。", timeout=5,
        )
        self.assertEqual(result.stdout, "土壤湿度。\n")
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertEqual(result.returncode, 7)
