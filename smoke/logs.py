"""Fail closed on unexpected application errors, including during shutdown.

Run by the supervisor AFTER descendant cleanup, not by the worker before exit.
Warnings are retained but not failed; there are deliberately no blanket error
allowlists (in particular, artifact-persistence errors are never suppressed).
"""
import json
from pathlib import Path
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
LEVEL = re.compile(r'^(?:\d{4}[/\-]\d{2}[/\-]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z)?\s+)?(?:ERR|ERROR|FTL|FATAL)\b')
CRASH = re.compile(r'^(?:panic:|fatal error:|runtime: out of memory|goroutine \d+ \[[^\]]+\]:)')


def inspect(output):
    files = sorted(Path(output).glob('app-[0-9]*.log'))
    findings = []
    for path in files:
        for number, original in enumerate(path.read_text(errors='replace').splitlines(), 1):
            line = ANSI.sub('', original).strip()
            kind = 'crash' if CRASH.search(line) else 'error' if LEVEL.search(line) else None
            if line.startswith('{'):
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and str(record.get('level', '')).lower() in ('error', 'fatal', 'panic'):
                        kind = 'error'
                except ValueError:
                    pass
            if kind:
                findings.append({'file': path.name, 'line': number, 'kind': kind, 'message': line})
    return {'files': [p.name for p in files], 'findings': findings}


def report_case(output):
    result = inspect(output)
    (Path(output) / 'log-hygiene.json').write_text(json.dumps(result, indent=2) + '\n')
    if not result['files']:
        return None  # Worker may have failed before it could launch the app.
    case = {'name': 'app.log_hygiene', 'status': 'PASS', 'seconds': 0,
            'files_scanned': len(result['files'])}
    if result['findings']:
        case['status'] = 'FAIL'
        lines = [f"{r['file']}:{r['line']}: {r['message']}" for r in result['findings'][:12]]
        case['error'] = (f"{len(result['findings'])} unexpected server error/crash lines; "
                         'see log-hygiene.json for all findings:\n' + '\n'.join(lines))
    return case
