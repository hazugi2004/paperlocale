"""Exercise the real subprocess timeout and the existing bounded recovery loop."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from paperlocale.diagnostics import LocatedError
from paperlocale.domains import load_domain_pack
from paperlocale.providers import CodexLocalProvider, Segment, TranslationContext
from paperlocale.recovery import run_waiting


class CodexTimeoutTests(unittest.TestCase):
    def check_timeout(self, persistent):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'codex-fixture'
            count = root / 'calls'
            executable.write_text(
                f'#!{sys.executable}\n'
                'import json, sys, time\n'
                'from pathlib import Path\n'
                f'count = Path({str(count)!r})\n'
                'n = int(count.read_text()) + 1 if count.exists() else 1\n'
                'count.write_text(str(n))\n'
                f'if n == 1 or {persistent!r}: time.sleep(10)\n'
                'output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])\n'
                'output.write_text(json.dumps({"translations": {"s1": "土壤湿度。"}}))\n'
            )
            executable.chmod(0o700)
            provider = CodexLocalProvider(codex_bin=executable, timeout_seconds=2)
            context = TranslationContext('en', 'zh-CN', load_domain_pack('atmospheric-science'))
            waits = []

            def operation():
                try:
                    return provider.translate([Segment('probe', 'Soil moisture.')], context)
                except Exception as error:
                    # The pipeline wraps service failures before recovery sees them.
                    raise LocatedError(str(error), [], 'provider') from error

            def wait(delay):
                waits.append(delay)
                record = json.loads((root / 'waiting.json').read_text())
                if len(waits) == 1:
                    self.assertEqual(record['state'], 'retrying')
                    self.assertEqual(count.read_text(), '1')
                else:
                    self.assertEqual(record['state'], 'waiting')
                    self.assertEqual(count.read_text(), '2')
                    raise KeyboardInterrupt()

            if persistent:
                with self.assertRaises(KeyboardInterrupt):
                    run_waiting(operation, root, sleep=wait)
                self.assertEqual(len(waits), 2)
            else:
                result = run_waiting(operation, root, sleep=wait)
                self.assertEqual(result[0].target, '土壤湿度。')
                self.assertEqual(waits, [30])
            self.assertEqual(count.read_text(), '2')

    def test_timed_out_process_retries_once_and_returns_complete_response(self):
        self.check_timeout(False)

    def test_repeated_process_timeout_waits_after_one_retry(self):
        self.check_timeout(True)
