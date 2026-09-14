"""Bounded server-log inspection, invoked AFTER descendant cleanup.

Unexpected errors AND incomplete inspection fail closed. This script also runs
in a separately timed subprocess so a blocked filesystem cannot hold up the
supervisor past its remaining inspection budget.
"""
import itertools
import json
from pathlib import Path
import re
import stat
import sys

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
LEVEL = re.compile(r'^(?:\d{4}[/\-]\d{2}[/\-]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z)?\s+)?(?:ERR|ERROR|FTL|FATAL)\b')
CRASH = re.compile(r'^(?:panic:|fatal error:|runtime: out of memory|goroutine \d+ \[[^\]]+\]:)')
MAX_FILES = 16
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_SAMPLES = 20


def inspect(output):
    files = sorted(itertools.islice(Path(output).glob('app-[0-9]*.log'), MAX_FILES + 1))
    result = {'files': [p.name for p in files], 'scanned_files': [], 'findings': [],
              'findings_total': 0, 'inspection_errors': [], 'complete': True}
    if len(files) > MAX_FILES:
        result['inspection_errors'].append(f'More than {MAX_FILES} app logs; remaining files not inspected')
    total = 0
    for path in files[:MAX_FILES]:
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('not a regular log file')
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError(f'exceeds {MAX_FILE_BYTES}-byte per-file scan limit')
            if total + info.st_size > MAX_TOTAL_BYTES:
                raise ValueError(f'exceeds {MAX_TOTAL_BYTES}-byte aggregate scan limit')
            total += info.st_size
            consumed = 0
            with path.open('rb') as stream:
                number = 0
                while raw := stream.readline(MAX_LINE_BYTES + 1):
                    number += 1
                    consumed += len(raw)
                    if len(raw) > MAX_LINE_BYTES:
                        raise ValueError(f'line {number} exceeds {MAX_LINE_BYTES}-byte scan limit')
                    if consumed > info.st_size:
                        raise ValueError('log grew after process cleanup; inspection incomplete')
                    line = ANSI.sub('', raw.decode(errors='replace')).strip()
                    kind = 'crash' if CRASH.search(line) else 'error' if LEVEL.search(line) else None
                    if line.startswith('{'):
                        try:
                            record = json.loads(line)
                            if isinstance(record, dict) and str(record.get('level', '')).lower() in ('error', 'fatal', 'panic'):
                                kind = 'error'
                        except ValueError:
                            pass
                    if kind:
                        result['findings_total'] += 1
                        if len(result['findings']) < MAX_SAMPLES:
                            result['findings'].append({'file': path.name, 'line': number,
                                                      'kind': kind, 'message': line[:1000]})
            if consumed != info.st_size:
                raise ValueError('log size changed during inspection')
            result['scanned_files'].append(path.name)
        except (OSError, ValueError) as exc:
            result['inspection_errors'].append(f'{path.name}: {exc}')
    result['complete'] = not result['inspection_errors']
    return result


def report_case(output):
    result = inspect(output)
    (Path(output) / 'log-hygiene.json').write_text(json.dumps(result, indent=2) + '\n')
    if not result['files']:
        return None  # Worker may have failed before it could launch the app.
    case = {'name': 'app.log_hygiene', 'status': 'PASS', 'seconds': 0,
            'files_scanned': len(result['scanned_files'])}
    if result['findings_total'] or not result['complete']:
        case['status'] = 'FAIL'
        lines = [f"{r['file']}:{r['line']}: {r['message']}" for r in result['findings'][:12]]
        case['error'] = (f"{result['findings_total']} unexpected server error/crash lines; "
                         'see log-hygiene.json (bounded samples):\n' + '\n'.join(lines))
        if result['inspection_errors']:
            case['error'] += '\nIncomplete inspection: ' + '; '.join(result['inspection_errors'])
    return case


if __name__ == '__main__':
    print(json.dumps(report_case(Path(sys.argv[1]))))
