#!/usr/bin/env python3
"""Regression tests for the test harness itself; no live BuildBuddy required."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SupervisorTest(unittest.TestCase):
    def run_fake(self, code, budget=15):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        binary = root / 'fake-app'
        binary.write_text('#!/usr/bin/python3\n' + code)
        binary.chmod(0o755)
        output = root / 'result'
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(ROOT / 'run.py'), '--binary', str(binary),
            '--output', str(output), '--budget', str(budget)], capture_output=True, text=True, timeout=budget + 2)
        elapsed = time.monotonic() - start
        return result, json.loads((output / 'report.json').read_text()), output, elapsed

    def test_crashed_binary_fails_and_reports(self):
        result, report, output, elapsed = self.run_fake('raise SystemExit(17)\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(report['failed'], 1)
        self.assertIn('App exited 17', next(t['error'] for t in report['tests'] if t['status'] == 'FAIL'))
        self.assertTrue((output / 'junit.xml').exists())
        self.assertLess(elapsed, 10)

    def test_hanging_binary_has_hard_deadline_and_no_live_child(self):
        result, report, output, elapsed = self.run_fake(
            'import os,time\nprint(os.getpid(), flush=True)\ntime.sleep(1000)\n', budget=5)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['tests'][-1]['name'], 'supervisor')
        self.assertLess(elapsed, 5)
        pid = int((output / 'app-1.log').read_text().strip())
        stat = Path(f'/proc/{pid}/stat')
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z', 'Timed-out app still running')

    def test_budget_cannot_exceed_two_minutes(self):
        result = subprocess.run([sys.executable, str(ROOT / 'run.py'), '--budget', '121'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('between 5 and 120', result.stderr)


if __name__ == '__main__':
    unittest.main()
