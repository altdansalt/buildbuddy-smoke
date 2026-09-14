"""Deterministic Remote Asset -> HTTP origin -> CAS cross-service checks."""
import base64
import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
from google.bytestream import bytestream_pb2 as bs, bytestream_pb2_grpc as bsg
from proto import remote_asset_pb2 as ra, remote_asset_pb2_grpc as rag
from proto import remote_execution_pb2 as re, remote_execution_pb2_grpc as reg


def cases(ctx):
    data = b"buildbuddy-smoke remote asset\x00\xff\n" * 193
    digest = re.Digest(hash=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    requests = []
    disabled = [False]

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('X-Smoke')))
            if self.path != '/asset' or disabled[0]:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    threading.Thread(target=origin.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()
    ctx.cleanups.append(lambda: (origin.shutdown(), origin.server_close()))
    url = f'http://127.0.0.1:{origin.server_port}'
    fetch = rag.FetchStub(ctx.channel)
    cas = reg.ContentAddressableStorageStub(ctx.channel)
    stream = bsg.ByteStreamStub(ctx.channel)
    checksum = ra.Qualifier(name='checksum.sri', value='sha256-' + base64.b64encode(hashlib.sha256(data).digest()).decode())

    def call(uris, qualifiers=(), instance='asset-smoke'):
        return fetch.FetchBlob(ra.FetchBlobRequest(instance_name=instance, uris=uris,
            qualifiers=qualifiers, digest_function=re.DigestFunction.SHA256), timeout=ctx.timeout)

    def verify(r):
        assert r.status.code == 0, str(r)
        assert r.blob_digest == digest, str(r)
        assert r.digest_function == re.DigestFunction.SHA256, str(r)
        resource = f'asset-smoke/blobs/{digest.hash}/{digest.size_bytes}'
        got = b''.join(x.data for x in stream.Read(bs.ReadRequest(resource_name=resource), timeout=ctx.timeout))
        assert got == data, 'Remote Asset CAS bytes differ from HTTP body'

    def fetch_and_read():
        # The app's own gRPC client starts dialing before its listener exists.
        # /readyz can precede its reconnect. Permit only this known transport
        # warm-up, within a fixed 3s window; never retry assertion/data errors.
        deadline = time.monotonic() + 3
        attempts = 0
        while True:
            requests.clear()
            r = call([url + '/missing', url + '/asset'], [checksum, ra.Qualifier(name='http_header:X-Smoke', value='origin-proof')])
            attempts += 1
            transient = r.status.code == 5 and 'code = Unavailable' in r.status.message and 'connection refused' in r.status.message
            if not transient or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        ctx.state['asset_startup_attempts'] = attempts
        verify(r)
        assert r.uri == url + '/asset', str(r)
        assert requests == [('/missing', 'origin-proof'), ('/asset', 'origin-proof')], requests

    def reuse_without_origin():
        before = len(requests)
        disabled[0] = True
        try:
            verify(call([url + '/asset'], [checksum]))
            assert len(requests) == before, 'Checksum cache hit contacted origin'
        finally:
            disabled[0] = False

    def bad_checksum():
        wrong = hashlib.sha256(b'wrong asset').digest()
        r = call([url + '/asset'], [ra.Qualifier(name='checksum.sri', value='sha256-' + base64.b64encode(wrong).decode())])
        assert r.status.code == 5 and ('checksum' in r.status.message or 'digest' in r.status.message), str(r)
        assert requests[-1] == ('/asset', None), requests
        missing = re.Digest(hash=wrong.hex(), size_bytes=len(data))
        got = cas.FindMissingBlobs(re.FindMissingBlobsRequest(instance_name='asset-smoke', blob_digests=[missing]), timeout=ctx.timeout)
        assert list(got.missing_blob_digests) == [missing], str(got)

    def not_found():
        r = call([url + '/missing'])
        assert r.status.code == 5, str(r)

    def private_ip_blocked():
        r = call([f'http://127.0.0.2:{origin.server_port}/asset'])
        assert r.status.code == 5 and 'IP address not allowed' in r.status.message, str(r)

    def unsupported_qualifier():
        try:
            call([url + '/asset'], [ra.Qualifier(name='smoke.unsupported', value='x')])
        except grpc.RpcError as e:
            assert e.code() == grpc.StatusCode.INVALID_ARGUMENT, str(e)
        else:
            raise AssertionError('Unsupported qualifier accepted')

    def directory_contract():
        try:
            fetch.FetchDirectory(ra.FetchDirectoryRequest(uris=[url + '/asset']), timeout=ctx.timeout)
        except grpc.RpcError as e:
            assert e.code() == grpc.StatusCode.UNIMPLEMENTED, str(e)
        else:
            raise AssertionError('FetchDirectory behavior changed; add positive coverage')

    return [
        ('asset.fetch_fallback_headers_checksum_and_cas', fetch_and_read),
        ('asset.checksum_hit_without_origin', reuse_without_origin),
        ('asset.reject_checksum_mismatch', bad_checksum),
        ('asset.missing_origin_response_status', not_found),
        ('asset.block_non_allowlisted_loopback', private_ip_blocked),
        ('asset.reject_unknown_qualifier', unsupported_qualifier),
        ('asset.fetch_directory_explicitly_unimplemented', directory_contract),
    ]
