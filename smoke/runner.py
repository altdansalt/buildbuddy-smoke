"""App lifecycle and small assertion-based case runner (invoked by run.py)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import traceback
from types import SimpleNamespace
from urllib.request import Request, urlopen

import grpc


def ports(n):
    sockets = [socket.socket() for _ in range(n)]
    try:
        for s in sockets:
            s.bind(('127.0.0.1', 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


class App:
    def __init__(self, binary, output, root):
        self.binary, self.output = binary, output
        self.http, self.grpc, self.monitor, self.internal = ports(4)
        self.http_url = f'http://127.0.0.1:{self.http}'
        self.process = None
        self.generation = 0
        # JSON is a subset of YAML and safely quotes temporary paths.
        config = {
            'app': {'build_buddy_url': self.http_url,
                    'events_api_url': f'grpc://127.0.0.1:{self.grpc}',
                    'cache_api_url': f'grpc://127.0.0.1:{self.grpc}',
                    'invocation_log_streaming_enabled': True},
            'auth': {'enable_anonymous_usage': True},
            'database': {'data_source': f'sqlite3://{root}/app.db'},
            'storage': {'disk': {'root_directory': f'{root}/storage'}, 'enable_chunked_event_logs': True},
            'cache': {'disk': {'root_directory': f'{root}/cache'}, 'max_size_bytes': 100_000_000,
                      'zstd_transcoding_enabled': True},
            'remote_asset': {'allowed_private_ips': ['127.0.0.1/32']},
            'remote_execution': {'enable_remote_exec': False},
        }
        self.config = output / 'config.yaml'
        self.config.write_text(json.dumps(config, indent=2))

    def start(self):
        self.generation += 1
        log_path = self.output / f'app-{self.generation}.log'
        env = os.environ.copy()
        # Never pass host cloud/GitHub credentials or HTTP proxy settings into the app.
        env = {k: v for k, v in env.items() if not any(x in k.upper() for x in ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'PROXY', 'API_KEY'))}
        env['GOMAXPROCS'] = '4'
        with log_path.open('wb') as log:
            self.process = subprocess.Popen([str(self.binary), f'--config_file={self.config}',
                '--listen=127.0.0.1', f'--port={self.http}', f'--grpc_port={self.grpc}',
                f'--monitoring_port={self.monitor}', f'--internal_grpc_port={self.internal}',
                '--disable_telemetry=true', '--telemetry_port=-1', '--max_shutdown_duration=2s'],
                stdout=log, stderr=subprocess.STDOUT, env=env)
        deadline = time.monotonic() + 15
        last = ''
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f'App exited {self.process.returncode}: {log_path.read_text()[-6000:]}')
            try:
                with urlopen(Request(self.http_url + '/readyz', headers={'server-type': 'buildbuddy-server'}), timeout=.5) as r:
                    assert r.status == 200 and r.read() == b'OK'
                return
            except Exception as e:
                last = str(e)
                time.sleep(.05)
        raise AssertionError('Readiness timed out: ' + last)

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is not None:
            raise AssertionError(f'App unexpectedly exited {self.process.returncode}')
        self.process.terminate()
        try:
            rc = self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError('App did not shut down within 4 seconds')
        finally:
            self.process = None
        assert rc == 0, f'Unclean app shutdown: {rc}'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'tests': []}

    def save():
        temp = args.output / 'report.tmp'
        temp.write_text(json.dumps(report, indent=2) + '\n')
        temp.replace(args.output / 'report.json')

    def case(name, fn):
        start = time.monotonic()
        item = {'name': name, 'status': 'PASS'}
        try:
            fn()
        except Exception:
            item.update(status='FAIL', error=traceback.format_exc())
        item['seconds'] = round(time.monotonic() - start, 4)
        report['tests'].append(item)
        save()
        print(f"{item['status']:4} {item['seconds']:7.3f}s {name}")
        if 'error' in item:
            print(item['error'])
        return item['status'] == 'PASS'

    # App database/cache are retained as artifacts, including on supervisor timeout.
    root = args.output / 'state'
    root.mkdir()
    app = App(args.binary, args.output, root)
    ctx = SimpleNamespace(timeout=5, state={}, cleanups=[], http_url=app.http_url,
                          grpc_target=f'127.0.0.1:{app.grpc}', output=args.output, app=app)
    ctx.channel = grpc.insecure_channel(ctx.grpc_target)
    try:
        if not case('app.cold_start_sqlite_migration_ready', app.start):
            return 1
        from smoke import cache, bes, asset, web
        for module in (web, cache, asset, bes):
            for name, fn in module.cases(ctx):
                case(name, fn)
        case('web.chromium_invocation_and_logs', lambda: web.browser_check(ctx))
        case('app.graceful_shutdown', app.stop)
        ctx.channel.close()
        if case('app.restart_existing_storage_ready', app.start):
            ctx.channel = grpc.insecure_channel(ctx.grpc_target)
            case('persistence.cache_and_invocation', lambda: persistence(ctx))
            case('app.final_graceful_shutdown', app.stop)
    except Exception:
        def failed():
            raise RuntimeError(traceback_text)
        traceback_text = traceback.format_exc()
        case('harness.unexpected_exception', failed)
    finally:
        ctx.channel.close()
        for cleanup in reversed(ctx.cleanups):
            try:
                cleanup()
            except Exception:
                case('harness.cleanup', lambda: (_ for _ in ()).throw(RuntimeError(traceback.format_exc())))
        if app.process and app.process.poll() is None:
            case('app.cleanup_shutdown', app.stop)
    return int(any(t['status'] == 'FAIL' for t in report['tests']))


def persistence(ctx):
    # Modules expose durable-state probes; read with a NEW channel after restart.
    from smoke import cache, bes
    cache.restart_probe(ctx)
    bes.verify_persisted(ctx)


if __name__ == '__main__':
    raise SystemExit(main())
