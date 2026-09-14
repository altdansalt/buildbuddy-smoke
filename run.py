#!/usr/bin/env python3
"""Dependency-free supervisor: the budget includes worker startup and teardown."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from smoke.processes import become_subreaper, cleanup

ROOT = Path(__file__).resolve().parent


def log_hygiene_case(output, timeout):
    if timeout <= 0:
        raise TimeoutError('No remaining time for complete server-log inspection')
    result = subprocess.run([sys.executable, str(ROOT / 'smoke/logs.py'), str(output)],
                            capture_output=True, text=True, check=True, timeout=timeout)
    if len(result.stdout) > 200_000:
        raise ValueError('Log scanner exceeded its bounded result size')
    return json.loads(result.stdout)


def main():
    start = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / '.tools/buildbuddy-enterprise')
    parser.add_argument('--profile', choices=['core', 'full'], default='full',
                        help='core: protocols/browser/auth; full: also real Bazel cache reuse')
    parser.add_argument('--budget', type=float, default=120, help='Hard wall-clock limit, 5..120 seconds')
    parser.add_argument('--output', type=Path, default=ROOT / 'results' / time.strftime('%Y%m%d-%H%M%S'))
    args = parser.parse_args()
    if not 5 <= args.budget <= 120:
        parser.error('--budget must be between 5 and 120 seconds')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    python = ROOT / '.venv/bin/python'
    if not python.exists() or not args.binary.is_file():
        parser.error('Run scripts/setup.sh first (or supply --binary PATH)')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT / 'generated') + os.pathsep + str(ROOT)
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONOPTIMIZE'] = '0'  # Never allow the caller's environment to disable assertions.
    become_subreaper()
    p = subprocess.Popen([str(python), '-m', 'smoke.runner', '--binary', str(args.binary.resolve()),
                          '--output', str(args.output), '--profile', args.profile], env=env, start_new_session=True)
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    failure = None
    try:
        rc = p.wait(timeout=max(.01, args.budget - 3 - (time.monotonic() - start)))
    except subprocess.TimeoutExpired:
        failure = f'Hard runtime budget exceeded ({args.budget:g}s including cleanup)'
        rc = 124
    except KeyboardInterrupt:
        failure = 'Interrupted by user'
        rc = 130
    finally:
        # Chromium calls setsid(): process groups alone cannot contain it.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        survivors = cleanup(p)
    report_path = args.output / 'report.json'
    report = {'tests': []}
    if report_path.exists():
        try:
            with report_path.open('rb') as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                report_path.rename(args.output / 'oversized-worker-report.json')
                raise ValueError('Worker report exceeds 4 MiB; original retained separately')
            report = json.loads(raw)
        except (OSError, ValueError) as exc:
            report = {'tests': [{'name': 'supervisor.worker_report', 'status': 'FAIL',
                                  'seconds': 0, 'error': str(exc)}]}
    has_worker_tests = bool(report['tests'])
    try:
        remaining = start + args.budget - .25 - time.monotonic()
        log_case = log_hygiene_case(args.output, timeout=min(1.0, remaining))
    except Exception as exc:
        log_case = {'name': 'app.log_hygiene', 'status': 'FAIL', 'seconds': 0,
                    'error': f'Incomplete application-log inspection: {exc}'}
        (args.output / 'log-hygiene.json').write_text(json.dumps(
            {'complete': False, 'inspection_errors': [log_case['error']]}, indent=2) + '\n')
    if log_case:
        report['tests'].append(log_case)
        print(f"{log_case['status']:4}         {log_case['name']}")
        if 'error' in log_case:
            print(log_case['error'])
    if failure or not has_worker_tests or (rc and not any(t['status'] == 'FAIL' for t in report['tests'])):
        report['tests'].append({'name': 'supervisor', 'status': 'FAIL', 'seconds': 0,
                                'error': failure or f'Worker exited {rc} without a recorded test failure (or without any tests)'})
    if survivors:
        report['tests'].append({'name': 'supervisor.cleanup', 'status': 'FAIL', 'seconds': 0,
                                'error': f'Child processes survived SIGKILL: {survivors}'})
    report['elapsed_seconds'] = round(time.monotonic() - start, 3)
    report['profile'] = args.profile
    report['budget_seconds'] = args.budget
    report['binary'] = str(args.binary.resolve())
    report['passed'] = sum(t['status'] == 'PASS' for t in report['tests'])
    report['failed'] = sum(t['status'] == 'FAIL' for t in report['tests'])
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    suite = ET.Element('testsuite', name='buildbuddy-smoke', tests=str(len(report['tests'])),
                       failures=str(report['failed']), time=str(report['elapsed_seconds']))
    for t in report['tests']:
        case = ET.SubElement(suite, 'testcase', name=t['name'], time=str(t['seconds']))
        if t['status'] == 'FAIL':
            ET.SubElement(case, 'failure', message=t.get('error', '')).text = t.get('error', '')
    ET.ElementTree(suite).write(args.output / 'junit.xml', encoding='unicode', xml_declaration=True)
    print(f"\n{report['passed']} passed, {report['failed']} failed in {report['elapsed_seconds']:.3f}s; artifacts: {args.output}")
    return rc or bool(report['failed'])


if __name__ == '__main__':
    sys.exit(main())
