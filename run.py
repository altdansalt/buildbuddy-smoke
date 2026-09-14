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

ROOT = Path(__file__).resolve().parent


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
    p = subprocess.Popen([str(python), '-m', 'smoke.runner', '--binary', str(args.binary.resolve()),
                          '--output', str(args.output), '--profile', args.profile], env=env, start_new_session=True)
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
        # Kill the entire session's process group, including orphaned app/browser children.
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=1)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
    report_path = args.output / 'report.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {'tests': []}
    if failure or not report['tests'] or (rc and not any(t['status'] == 'FAIL' for t in report['tests'])):
        report['tests'].append({'name': 'supervisor', 'status': 'FAIL', 'seconds': 0,
                                'error': failure or f'Worker exited {rc} without a recorded test failure (or without any tests)'})
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
