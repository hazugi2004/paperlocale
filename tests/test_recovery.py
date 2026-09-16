"""验证真实恢复边界：一次重试、保持等待、旧信号、取消与结果语义。"""
import json
import tempfile
import unittest
from pathlib import Path

from paperlocale.recovery import run_waiting, resume_waiting


class RecoveryTests(unittest.TestCase):
    def test_transient_retries_once_and_returns_real_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calls, waits = Path(directory), [], []
            def operation():
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionError('temporary')
                return {'complete': True}
            self.assertEqual(run_waiting(operation, root, sleep=waits.append), {'complete': True})
            self.assertEqual(waits, [30])
            self.assertEqual(len(calls), 2)
            self.assertEqual(json.loads((root / 'waiting.json').read_text())['state'], 'completed')

    def test_persistent_error_needs_explicit_signal_without_busy_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calls, sleeps = Path(directory), [], []
            def operation():
                calls.append(1)
                if len(calls) < 3:
                    raise ConnectionError('still offline')
                return 42
            def sleep(delay):
                sleeps.append(delay)
                if len(sleeps) == 2:
                    self.assertEqual(len(calls), 2)
                    resume_waiting(root)
            self.assertEqual(run_waiting(operation, root, sleep=sleep), 42)
            self.assertEqual(len(calls), 3)

    def test_wrapped_connection_error_retries_and_cyclic_cause_waits(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calls = Path(directory), []
            def operation():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError('provider wrapper') from ConnectionError('offline')
                return 'complete'
            self.assertEqual(run_waiting(operation, root, sleep=lambda _: None), 'complete')
            self.assertEqual(len(calls), 2)
            def cyclic_failure():
                error = RuntimeError('cyclic cause')
                error.__cause__ = error
                raise error
            def cancel(_):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                run_waiting(cyclic_failure, root, sleep=cancel)
            self.assertEqual(json.loads((root / 'waiting.json').read_text())['state'], 'waiting')

    def test_contract_error_waits_and_resumes_without_automatic_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calls = Path(directory), []
            def operation():
                calls.append(1)
                if len(calls) == 1:
                    raise ValueError('formula mismatch')
                return 'verified'
            def sleep(_):
                self.assertEqual(len(calls), 1)
                resume_waiting(root)
            self.assertEqual(run_waiting(operation, root, sleep=sleep), 'verified')

    def test_stale_signal_does_not_resume_and_user_can_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'resume.request').write_text('{"error_id":"old"}')
            def fail():
                raise ValueError('not fixed')
            def cancel(_):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                run_waiting(fail, root, sleep=cancel)
            self.assertEqual(json.loads((root / 'waiting.json').read_text())['state'], 'waiting')

    def test_secret_not_written_in_wait_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail():
                raise ValueError('key sk-secret-value Bearer private-token')
            def cancel(_):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                run_waiting(fail, root, sleep=cancel)
            text = (root / 'waiting.json').read_text()
            self.assertNotIn('sk-secret', text)
            self.assertNotIn('private-token', text)
