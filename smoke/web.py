"""HTTP checks plus a real browser proving that bundled JS renders BES data."""
import json
import re
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError


def cases(ctx):
    def get(path):
        return urlopen(ctx.http_url + path, timeout=ctx.timeout)

    def health():
        for route in ('healthz', 'readyz'):
            with get('/' + route + '?server-type=buildbuddy-server') as r:
                assert r.status == 200 and r.read() == b'OK'

    def bundle():
        with get('/') as r:
            assert 'text/html' in r.headers.get('Content-Type', '')
            html = r.read().decode()
        scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)', html)
        bundles = [s for s in scripts if '/app/' in s and '.js' in s]
        assert bundles, 'No bundled app script in HTML'
        for path in bundles:
            assert path.startswith('/'), 'Unexpected nonlocal app bundle'
            with get(path) as r:
                body = r.read()
                assert r.status == 200 and len(body) > 10000
                assert 'javascript' in r.headers.get('Content-Type', ''), r.headers
                assert not body.lstrip().startswith(b'<!'), 'SPA fallback returned HTML for JS'

    def rpc():
        payload = json.dumps({'host': f'127.0.0.1:{ctx.app.http}', 'protocol': 'http:'}).encode()
        request = Request(ctx.http_url + '/rpc/BuildBuddyService/GetBazelConfig', data=payload,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=ctx.timeout) as r:
            value = json.load(r)
        assert 'bes_backend' in json.dumps(value) and str(ctx.app.grpc) in json.dumps(value), value

    def metrics():
        with urlopen(f'http://127.0.0.1:{ctx.app.monitor}/metrics', timeout=ctx.timeout) as r:
            text = r.read().decode()
        assert '# HELP' in text and 'go_goroutines' in text, text[:1000]

    def malformed_rpc():
        request = Request(ctx.http_url + '/rpc/BuildBuddyService/GetInvocation', data=b'{bad json',
                          headers={'Content-Type': 'application/json'})
        try:
            urlopen(request, timeout=ctx.timeout)
        except HTTPError as e:
            assert e.code == 400, str(e)
        else:
            raise AssertionError('Malformed JSON RPC accepted')

    return [('web.liveness_and_readiness', health), ('web.html_and_embedded_js_bundle', bundle),
            ('web.http_json_rpc_bazel_config', rpc), ('web.reject_malformed_json_rpc', malformed_rpc),
            ('web.prometheus_metrics', metrics)]


def artifact_cases(ctx):
    def download_cache():
        fixture = ctx.state['cache_restart']
        d = fixture['digest']
        uri = f"bytestream://{ctx.grpc_target}/{fixture['instance_name']}/blobs/{d['hash']}/{d['size_bytes']}"
        query = urlencode({'bytestream_url': uri, 'filename': 'smoke-output.txt'})
        with urlopen(ctx.http_url + '/file/download?' + query, timeout=ctx.timeout) as r:
            assert r.read() == bytes.fromhex(fixture['data_hex'])
            assert r.headers['Content-Type'] == 'application/octet-stream'
            assert 'attachment;' in r.headers['Content-Disposition']
        with urlopen(ctx.http_url + '/file/view?' + query, timeout=ctx.timeout) as r:
            assert r.read() == bytes.fromhex(fixture['data_hex'])
            assert 'inline;' in r.headers['Content-Disposition']

    def download_logs():
        from smoke import bes
        for key, success in [('invocation_id', True), ('failed_invocation_id', False)]:
            iid = ctx.state[key]
            invocation = bes.verify_invocation(ctx, iid, success)
            query = urlencode({'invocation_id': iid, 'artifact': 'buildlog', 'attempt': invocation.attempt})
            with urlopen(ctx.http_url + '/file/download?' + query, timeout=ctx.timeout) as r:
                assert r.read() == bes._fixture(iid, success)['log'].encode()
            query = urlencode({'invocation_id': iid, 'artifact': 'raw_json'})
            with urlopen(ctx.http_url + '/file/download?' + query, timeout=ctx.timeout) as r:
                events = json.load(r)
            from google.protobuf.json_format import ParseDict
            from proto import build_event_stream_pb2 as bep
            assert isinstance(events, list) and len(events) >= 4, 'Raw export is not a build-event array'
            decoded = [ParseDict(event, bep.BuildEvent()) for event in events]
            started = [e.started for e in decoded if e.WhichOneof('payload') == 'started']
            finished = [e.finished for e in decoded if e.WhichOneof('payload') == 'finished']
            metadata = [e.build_metadata for e in decoded if e.WhichOneof('payload') == 'build_metadata']
            assert len(started) == len(finished) == len(metadata) == 1
            assert started[0].uuid == iid and started[0].command == 'build'
            assert finished[0].exit_code.code == (0 if success else 1)
            assert finished[0].exit_code.name == ('SUCCESS' if success else 'BUILD_FAILURE')
            assert metadata[0].metadata['SMOKE_MARKER'] == bes._fixture(iid, success)['marker']

    return [('web.download_and_view_cas_artifact', download_cache),
            ('web.download_build_logs_and_raw_events', download_logs)]


def browser_check(ctx):
    from playwright.sync_api import sync_playwright, expect
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        # A core smoke run must not depend on internet or accidentally use third-party trackers.
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(ctx.http_url + '/') else route.abort())
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(ctx.http_url + '/invocation/' + ctx.state['invocation_id'], wait_until='domcontentloaded')
            expect(page.locator('body')).to_contain_text('Succeeded')
            expect(page.locator('body')).to_contain_text(ctx.state['invocation_marker'])
            page.locator('a.tab[href="#details"]').click()
            expect(page.locator('body')).to_contain_text('smoke-host')
            expect(page.locator('a.tab[href="#details"]')).to_have_class('tab selected')
            page.locator('a.tab[href="#log"]').click()
            expect(page.locator('a.tab[href="#log"]')).to_have_class('tab selected')
            expect(page.locator('body')).to_contain_text(ctx.state['invocation_marker'])
            page.screenshot(path=str(ctx.output / 'invocation-success.png'), full_page=True)
            page.goto(ctx.http_url + '/invocation/' + ctx.state['failed_invocation_id'], wait_until='domcontentloaded')
            expect(page.locator('body')).to_contain_text('Build failed')
            expect(page.locator('body')).to_contain_text(ctx.state['failed_invocation_marker'])
            page.screenshot(path=str(ctx.output / 'invocation-failed.png'), full_page=True)
            if ctx.profile == 'full':
                from smoke import bazel
                bazel.browser_targets_check(ctx, page)
            assert not errors, errors
        finally:
            (ctx.output / 'browser-errors.json').write_text(json.dumps(errors, indent=2))
            (ctx.output / 'browser-last-page.txt').write_text(page.locator('body').inner_text())
            page.screenshot(path=str(ctx.output / 'browser-last-page.png'), full_page=True)
            context.close()
            browser.close()
