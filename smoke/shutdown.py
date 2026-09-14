"""BES-acknowledged artifacts must survive shutdown outside the hot CAS.

A control is published early. A second artifact is published immediately before
SIGTERM; do not sleep/poll for its asynchronous persistence before shutting down.
After restart with an EMPTY cache, HTTP download must fall back to blob storage.
"""
import json
import time
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen

from proto import build_event_stream_pb2 as bep
from proto import build_events_pb2 as envelope
from proto import publish_build_event_pb2 as publish
from proto import publish_build_event_pb2_grpc

from smoke import bes, cache


def seed(ctx, label):
    iid = str(uuid.uuid4())
    data = f'BuildBuddy shutdown persistence: {label}: {iid}\n'.encode()
    digest = cache._Cache(ctx).put(data)[0]
    uri = f'bytestream://{ctx.grpc_target}/blobs/{digest.hash}/{digest.size_bytes}'
    requests = bes._requests(bes._fixture(iid, True))
    log_id = bep.BuildEventId(build_tool_logs={})
    started = bep.BuildEvent()
    assert requests[0].ordered_build_event.event.bazel_event.Unpack(started)
    started.children.append(log_id)
    requests[0].ordered_build_event.event.bazel_event.Pack(started)
    last = bep.BuildEvent()
    assert requests[-2].ordered_build_event.event.bazel_event.Unpack(last)
    last.last_message = False
    requests[-2].ordered_build_event.event.bazel_event.Pack(last)
    event = bep.BuildEvent(id=log_id, last_message=True,
        build_tool_logs=bep.BuildToolLogs(log=[bep.File(name='shutdown-proof.txt', uri=uri)]))
    wrapped = envelope.BuildEvent()
    wrapped.event_time.GetCurrentTime()
    wrapped.bazel_event.Pack(event)
    requests.insert(-1, publish.PublishBuildToolEventStreamRequest(
        ordered_build_event=publish.OrderedBuildEvent(
            stream_id=requests[0].ordered_build_event.stream_id,
            sequence_number=len(requests), event=wrapped)))
    requests[-1].ordered_build_event.sequence_number = len(requests)
    responses = list(publish_build_event_pb2_grpc.PublishBuildEventStub(ctx.channel).
        PublishBuildToolEventStream(iter(requests), timeout=ctx.timeout))
    acknowledged = time.monotonic()
    assert [r.sequence_number for r in responses] == list(range(1, len(requests) + 1))
    assert all(r.stream_id == requests[0].ordered_build_event.stream_id for r in responses)
    ctx.state.setdefault('shutdown_artifacts', {})[label] = {
        'invocation_id': iid, 'uri': uri, 'data_hex': data.hex(),
        'digest': {'hash': digest.hash, 'size_bytes': digest.size_bytes},
        'acknowledged_monotonic': acknowledged,
    }


def verify_original_cas(ctx):
    """Distinguish lost blobstore persistence from losing the original CAS data."""
    c = cache._Cache(ctx)
    for label in ('control', 'pending'):
        fixture = ctx.state['shutdown_artifacts'][label]
        d = cache.re.Digest(**fixture['digest'])
        assert c.read(d) == bytes.fromhex(fixture['data_hex']), f'{label}: original CAS bytes lost on restart'


def select_empty_cache(ctx):
    # Configuration-level cache replacement, not deleting/mutating backend files.
    # The original cache remains intact in the artifacts for diagnosis.
    config = json.loads(ctx.app.config.read_text())
    fresh = ctx.output / 'state' / 'empty-cache-after-shutdown'
    assert not fresh.exists(), 'Shutdown fallback probe requires a genuinely fresh CAS'
    config['cache']['disk']['root_directory'] = str(fresh)
    ctx.app.config.write_text(json.dumps(config, indent=2))


def verify(ctx, label):
    fixture = ctx.state['shutdown_artifacts'][label]
    inv = bes.verify_invocation(ctx, fixture['invocation_id'])
    declared = [f.uri for e in inv.event if e.build_event.WhichOneof('payload') == 'build_tool_logs'
                for f in e.build_event.build_tool_logs.log]
    assert declared == [fixture['uri']], f'{label}: acknowledged artifact declaration was not persisted'
    d = cache.re.Digest(**fixture['digest'])
    c = cache._Cache(ctx)
    assert list(c.missing([d]).missing_blob_digests) == [d], 'Artifact is still in CAS; fallback was not tested'
    query = urlencode({'invocation_id': fixture['invocation_id'], 'bytestream_url': fixture['uri'],
                       'filename': 'shutdown-proof.txt'})
    try:
        with urlopen(ctx.http_url + '/file/download?' + query, timeout=ctx.timeout) as response:
            actual = response.read()
            assert response.status == 200 and actual == bytes.fromhex(fixture['data_hex']), (
                f'{label}: persisted artifact byte mismatch after shutdown and CAS replacement')
    except Exception as exc:
        raise AssertionError(
            f'{label}: BES acknowledged artifact cannot be downloaded after SIGTERM with an empty CAS; '
            f'invocation={fixture["invocation_id"]}: {exc}') from exc
    assert list(c.missing([d]).missing_blob_digests) == [d], 'Unexpected CAS repopulation obscured blobstore fallback'
