"""Unit regressions for server-log classification (no app dependencies)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import run
from smoke.logs import inspect, report_case


class LogHygieneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def test_colored_shutdown_error_is_not_a_clean_exit(self):
        (self.output / 'app-1.log').write_text(
            '\x1b[90m2026/09/14 19:41:55.184\x1b[0m \x1b[1m\x1b[31mERR\x1b[0m '
            'Failed to stream to blobstore\n'
            '2026/09/14 19:41:55.194 INF Server "buildbuddy-server" stopped.\n')
        result = report_case(self.output)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('app-1.log:1', result['error'])
        self.assertEqual(len(json.loads((self.output / 'log-hygiene.json').read_text())['findings']), 1)

    def test_all_generations_and_crash_markers(self):
        for n, text in enumerate(['panic: bad\n', 'fatal error: concurrent map writes\n',
                                   'goroutine 7 [running]:\n', '{"level":"error","message":"late failure"}\n'], 1):
            (self.output / f'app-{n}.log').write_text(text)
        result = inspect(self.output)
        self.assertEqual(len(result['files']), 4)
        self.assertEqual(len(result['findings']), 4)

    def test_warnings_and_error_words_in_info_are_not_error_records(self):
        (self.output / 'app-1.log').write_text(
            '2026/09/14 19:41:55.184 WRN rejected invalid digest\n'
            '2026/09/14 19:41:55.184 INF expected ERR from fixture\n')
        self.assertEqual(report_case(self.output)['status'], 'PASS')

    def _supervised_report(self, worker_tests, late_message):
        root = self.output / 'repo'
        (root / '.venv/bin').mkdir(parents=True)
        (root / '.venv/bin/python').touch()
        binary = root / 'app'
        binary.touch()
        output = root / 'result'

        def launch(*args, **kwargs):
            (output / 'report.json').write_text(json.dumps({'tests': worker_tests}))
            return Mock(wait=Mock(return_value=0))

        def cleanup(worker):
            # Deliberately emitted AFTER the worker has exited successfully.
            (output / 'app-1.log').write_text(late_message)
            return []

        with patch.object(run, 'ROOT', root), patch.object(run, 'become_subreaper'), \
             patch.object(run, 'cleanup', side_effect=cleanup), \
             patch.object(run.subprocess, 'Popen', side_effect=launch), \
             patch.object(run.signal, 'signal'), \
             patch.object(run.sys, 'argv', ['run.py', '--binary', str(binary), '--output', str(output)]):
            result = run.main()
        return result, json.loads((output / 'report.json').read_text())

    def test_late_error_overrides_successful_worker_exit(self):
        result, report = self._supervised_report(
            [{'name': 'worker.test', 'status': 'PASS', 'seconds': 0}],
            '2026/09/14 19:41:55.184 ERR Failed to stream to blobstore\n')
        self.assertEqual(result, 1)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['tests'][-1]['name'], 'app.log_hygiene')

    def test_clean_log_cannot_make_empty_worker_suite_green(self):
        result, report = self._supervised_report([], '2026/09/14 19:41:55.184 INF done\n')
        self.assertEqual(result, 1)
        self.assertEqual(report['tests'][-1]['name'], 'supervisor')

    def test_missing_logs_are_not_a_passing_case(self):
        self.assertIsNone(report_case(self.output))


if __name__ == '__main__':
    unittest.main()
