"""HTTP checks plus a real browser proving that bundled JS renders BES data."""
import json
import re
from urllib.request import Request, urlopen


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

    return [('web.liveness_and_readiness', health), ('web.html_and_embedded_js_bundle', bundle),
            ('web.http_json_rpc_bazel_config', rpc), ('web.prometheus_metrics', metrics)]


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
            page.screenshot(path=str(ctx.output / 'invocation-success.png'), full_page=True)
            page.goto(ctx.http_url + '/invocation/' + ctx.state['failed_invocation_id'], wait_until='domcontentloaded')
            expect(page.locator('body')).to_contain_text('Failed')
            expect(page.locator('body')).to_contain_text(ctx.state['failed_invocation_marker'])
            page.screenshot(path=str(ctx.output / 'invocation-failed.png'), full_page=True)
            assert not errors, errors
        finally:
            (ctx.output / 'browser-errors.json').write_text(json.dumps(errors, indent=2))
            (ctx.output / 'browser-last-page.txt').write_text(page.locator('body').inner_text())
            page.screenshot(path=str(ctx.output / 'browser-last-page.png'), full_page=True)
            context.close()
            browser.close()
