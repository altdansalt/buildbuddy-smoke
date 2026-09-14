"""Synthetic Bazel BES streams, invocation metadata, and durable console logs.

Uses BuildBuddy's native gRPC service (registered by server/libmain/libmain.go),
not the separately authenticated public API. Only the request/response protos
are needed, avoiding the large buildbuddy_service.proto dependency closure.
"""

import hashlib
import queue
import threading
import time
import uuid

import grpc

from proto import build_event_stream_pb2 as bep
from proto import build_events_pb2 as envelope
from proto import eventlog_pb2
from proto import invocation_pb2
from proto import invocation_status_pb2
from proto import publish_build_event_pb2 as publish
from proto import publish_build_event_pb2_grpc


_DURATION_MS = 1250
_PATTERN = "//..."


def _fixture(invocation_id, success):
    outcome = "success" if success else "failure"
    marker = f"BUILDBUDDY_SMOKE_{outcome.upper()}_{invocation_id}"
    return {
        "invocation_id": invocation_id,
        "success": success,
        "marker": marker,
        "log": f"{marker}\nsmoke result: {outcome}\n",
        "exit_code": "SUCCESS" if success else "BUILD_FAILURE",
        "workspace": {"BUILD_USER": "smoke-user", "BUILD_HOST": "smoke-host"},
        "metadata": {
            "REPO_URL": "https://github.com/buildbuddy-io/buildbuddy",
            "COMMIT_SHA": hashlib.sha1(invocation_id.encode()).hexdigest(),
            "BRANCH_NAME": f"smoke/{outcome}",
            "ROLE": "CI",
            "SMOKE_MARKER": marker,
            "DISABLE_COMMIT_STATUS_REPORTING": "true",
        },
    }


def _events(fixture):
    """A closed BEP event graph, followed by the BES stream-finished envelope."""
    finish_ms = time.time_ns() // 1_000_000
    start_ms = finish_ms - _DURATION_MS
    started_id = bep.BuildEventId(started={})
    progress_id = bep.BuildEventId(progress={"opaque_count": 0})
    final_progress_id = bep.BuildEventId(progress={"opaque_count": 1})
    workspace_id = bep.BuildEventId(workspace_status={})
    metadata_id = bep.BuildEventId(build_metadata={})
    pattern_id = bep.BuildEventId(pattern={"pattern": [_PATTERN]})
    finished_id = bep.BuildEventId(build_finished={})

    started = bep.BuildEvent(
        id=started_id,
        children=[progress_id, workspace_id, metadata_id, pattern_id, finished_id],
        started=bep.BuildStarted(
            uuid=fixture["invocation_id"],
            start_time_millis=start_ms,
            build_tool_version="8.0.0",
            # Important: an empty options_description makes the server buffer
            # the stream waiting for OptionsParsed instead of creating the row.
            options_description="--color=no --curses=no",
            command="build",
            working_directory="/tmp/buildbuddy-smoke/workspace",
            workspace_directory="/tmp/buildbuddy-smoke/workspace",
            server_pid=1,
        ),
    )
    started.started.start_time.FromMilliseconds(start_ms)
    finished = bep.BuildEvent(
        id=finished_id,
        finished=bep.BuildFinished(
            overall_success=fixture["success"],
            exit_code=bep.BuildFinished.ExitCode(
                name=fixture["exit_code"], code=0 if fixture["success"] else 1
            ),
            finish_time_millis=finish_ms,
        ),
    )
    finished.finished.finish_time.FromMilliseconds(finish_ms)
    return [
        started,
        bep.BuildEvent(
            id=progress_id,
            children=[final_progress_id],
            progress=bep.Progress(stdout=fixture["marker"] + "\n"),
        ),
        bep.BuildEvent(
            id=metadata_id, build_metadata=bep.BuildMetadata(metadata=fixture["metadata"])
        ),
        bep.BuildEvent(
            id=workspace_id,
            workspace_status=bep.WorkspaceStatus(
                item=[
                    bep.WorkspaceStatus.Item(key=k, value=v)
                    for k, v in fixture["workspace"].items()
                ]
            ),
        ),
        bep.BuildEvent(id=pattern_id, expanded=bep.PatternExpanded()),
        finished,
        bep.BuildEvent(
            id=final_progress_id,
            last_message=True,
            progress=bep.Progress(stderr=fixture["log"].split("\n", 1)[1]),
        ),
    ]


def _requests(fixture):
    stream_id = envelope.StreamId(
        build_id=str(uuid.uuid4()),
        invocation_id=fixture["invocation_id"],
        component=envelope.StreamId.TOOL,
    )
    requests = []
    for event in _events(fixture):
        wrapped = envelope.BuildEvent()
        wrapped.event_time.GetCurrentTime()
        wrapped.bazel_event.Pack(event)
        requests.append(
            publish.PublishBuildToolEventStreamRequest(
                ordered_build_event=publish.OrderedBuildEvent(
                    stream_id=stream_id, sequence_number=len(requests) + 1, event=wrapped
                )
            )
        )
    final = envelope.BuildEvent(
        component_stream_finished=envelope.BuildEvent.BuildComponentStreamFinished(
            type=envelope.BuildEvent.BuildComponentStreamFinished.FINISHED
        )
    )
    final.event_time.GetCurrentTime()
    requests.append(
        publish.PublishBuildToolEventStreamRequest(
            ordered_build_event=publish.OrderedBuildEvent(
                stream_id=stream_id, sequence_number=len(requests) + 1, event=final
            )
        )
    )
    return requests


def _rpc(ctx, method, request, response_type):
    rpc = ctx.channel.unary_unary(
        f"/buildbuddy.service.BuildBuddyService/{method}",
        request_serializer=type(request).SerializeToString,
        response_deserializer=response_type.FromString,
    )
    return rpc(request, timeout=ctx.timeout)


def verify_invocation(ctx, invocation_id, success=True, attempt=1):
    """Recheck metadata AND persisted logs; safe to call after server restart."""
    fixture = _fixture(invocation_id, success)
    response = _rpc(
        ctx,
        "GetInvocation",
        invocation_pb2.GetInvocationRequest(
            lookup=invocation_pb2.InvocationLookup(invocation_id=invocation_id)
        ),
        invocation_pb2.GetInvocationResponse,
    )
    assert len(response.invocation) == 1, f"GetInvocation returned {len(response.invocation)} rows"
    inv = response.invocation[0]
    expected = {
        "invocation_id": invocation_id,
        "success": success,
        "invocation_status": invocation_status_pb2.COMPLETE_INVOCATION_STATUS,
        "command": "build",
        "user": fixture["workspace"]["BUILD_USER"],
        "host": fixture["workspace"]["BUILD_HOST"],
        "repo_url": fixture["metadata"]["REPO_URL"],
        "commit_sha": fixture["metadata"]["COMMIT_SHA"],
        "branch_name": fixture["metadata"]["BRANCH_NAME"],
        "role": "CI",
        "bazel_exit_code": fixture["exit_code"],
        "duration_usec": _DURATION_MS * 1000,
        "has_chunked_event_logs": True,
        "attempt": attempt,
    }
    for field, value in expected.items():
        actual = getattr(inv, field)
        assert actual == value, f"{invocation_id}: {field}: expected {value!r}, got {actual!r}"
    assert list(inv.pattern) == [_PATTERN], f"unexpected patterns: {list(inv.pattern)!r}"
    assert inv.last_chunk_id, "completed invocation has no stored log chunk ID"
    assert inv.created_at_usec > 0 and inv.updated_at_usec >= inv.created_at_usec

    # Verify events are fetched from the BEP blob as well as the summary DB row.
    # A dict alone would hide duplicated events after a reconnect/replay.
    event_ids = [e.build_event.id.SerializeToString() for e in inv.event]
    assert len(event_ids) == len(set(event_ids)), "GetInvocation duplicated BEP event IDs"
    sequences = [e.sequence_number for e in inv.event]
    assert sequences == sorted(set(sequences)), f"duplicated/unordered events: {sequences}"
    events = {e.build_event.WhichOneof("payload"): e.build_event for e in inv.event}
    for kind in ("started", "workspace_status", "build_metadata", "finished"):
        assert kind in events, f"GetInvocation omitted {kind} event"
    assert events["started"].started.uuid == invocation_id
    actual_workspace = {i.key: i.value for i in events["workspace_status"].workspace_status.item}
    for key, value in fixture["workspace"].items():
        assert actual_workspace.get(key) == value, f"workspace item {key} did not round-trip"
    for key, value in fixture["metadata"].items():
        assert events["build_metadata"].build_metadata.metadata.get(key) == value, (
            f"build metadata {key} did not round-trip"
        )
    assert events["finished"].finished.exit_code.code == (0 if success else 1)

    _verify_log(ctx, invocation_id, fixture["log"].encode(), live=False)
    return inv


def _log_chunk(ctx, invocation_id, chunk_id=""):
    return _rpc(
        ctx, "GetEventLogChunk",
        eventlog_pb2.GetEventLogChunkRequest(
            invocation_id=invocation_id, chunk_id=chunk_id,
            min_lines=100, type=eventlog_pb2.BUILD_LOG,
        ),
        eventlog_pb2.GetEventLogChunkResponse,
    )


def _verify_log(ctx, invocation_id, expected, live):
    # Request the live cursor explicitly: an omitted cursor means tail durable
    # chunks, not the volatile chunk, in pinned source 6fc014.
    chunk = _log_chunk(ctx, invocation_id, "0000")
    assert chunk.live == live, f"expected live={live}, got {chunk!r}"
    assert not chunk.previous_chunk_id, f"tiny log unexpectedly spans chunks: {chunk!r}"
    assert chunk.buffer == expected, (
        f"{invocation_id}: exact log mismatch: expected {expected!r}, got {chunk.buffer!r}"
    )
    assert chunk.next_chunk_id == ("0000" if live else "0001"), (
        f"unexpected {'live' if live else 'durable'} cursor: {chunk!r}"
    )
    if not live:
        # Resolve the actual tail instead of using Invocation.last_chunk_id:
        # this version can retain the pre-close 'ffff' sentinel in the DB.
        tail = _log_chunk(ctx, invocation_id)
        assert tail == chunk, "tail lookup differs from sole durable chunk"
        end = _log_chunk(ctx, invocation_id, chunk.next_chunk_id)
        assert not end.buffer and not end.next_chunk_id and not end.live, (
            "completed log did not terminate after its sole chunk"
        )
        assert end.previous_chunk_id == "0000", "tiny log should have exactly one durable chunk"
    return chunk


def _verify_acks(responses, requests):
    actual = [r.sequence_number for r in responses]
    expected = [r.ordered_build_event.sequence_number for r in requests]
    assert actual == expected, f"BES ACKs must be exact, consecutive and monotonic: {actual!r}"
    for response in responses:
        assert response.stream_id == requests[0].ordered_build_event.stream_id, (
            f"BES ACK {response.sequence_number} has the wrong stream ID"
        )


class _OpenStream:
    """Real bidi RPC with a queue held open independently of the ACK reader.

    Never wait for an ACK to send events: build_event_server only ACKs after
    request EOF and finalization. A separate channel lets us drop the transport
    without taking the GetInvocation/GetEventLogChunk observation channel down.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.requests = queue.Queue()
        self.responses = []
        self.lock = threading.Lock()
        self.error = None
        self.done = threading.Event()
        self.iterator_done = threading.Event()
        self.channel = grpc.insecure_channel(ctx.grpc_target)
        self.call = publish_build_event_pb2_grpc.PublishBuildEventStub(
            self.channel
        ).PublishBuildToolEventStream(self._iterate(), timeout=ctx.timeout * 4)
        self.reader = threading.Thread(target=self._read, name="smoke-bes-acks", daemon=True)
        self.reader.start()

    def _iterate(self):
        try:
            while True:
                request = self.requests.get()
                if request is None:
                    return
                yield request
        finally:
            self.iterator_done.set()

    def _read(self):
        try:
            for response in self.call:
                with self.lock:
                    self.responses.append(response)
        except Exception as error:
            self.error = error
        finally:
            self.done.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.call.cancel()
        self.channel.close()
        self.requests.put(None)  # unblock gRPC's request-iterator thread too
        self.reader.join(self.ctx.timeout)
        assert not self.reader.is_alive(), "BES ACK reader leaked after cleanup"
        assert self.iterator_done.wait(self.ctx.timeout), "BES request iterator leaked after cleanup"

    def send(self, requests):
        for request in requests:
            self.requests.put(request)

    def assert_open(self):
        with self.lock:
            assert not self.responses, f"BES ACKed before EOF: {self.responses!r}"
        assert not self.done.is_set(), f"BES closed before EOF: {self.error!r}"
        assert self.call.is_active(), "BES transport is no longer active"
        assert not self.iterator_done.is_set(), "BES request iterator reached EOF prematurely"

    def finish(self, remaining, all_requests):
        self.assert_open()
        self.send(remaining)
        self.requests.put(None)
        assert self.done.wait(self.ctx.timeout), "BES did not ACK and finish after EOF"
        if self.error:
            raise self.error
        _verify_acks(self.responses, all_requests)

    def disconnect(self):
        self.assert_open()
        # Close the HTTP/2 channel, not an orderly request half-close. No
        # BuildFinished or component_stream_finished has been sent at this point.
        self.channel.close()
        self.requests.put(None)
        assert self.done.wait(self.ctx.timeout), "transport close did not terminate BES"
        assert isinstance(self.error, grpc.RpcError), f"expected transport cancellation: {self.error!r}"
        assert self.error.code() == grpc.StatusCode.CANCELLED, repr(self.error)
        assert not self.responses, f"disconnected prefix was incorrectly ACKed: {self.responses!r}"


def _poll(ctx, description, read, predicate):
    """Bounded observation, not a retry of writes or of failed assertions/RPCs."""
    deadline = time.monotonic() + ctx.timeout
    while True:
        value = read()
        if predicate(value):
            return value
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for {description}; last observation: {value!r}"
        time.sleep(min(.025, remaining))


def _get_invocation(ctx, invocation_id, allow_missing=False):
    try:
        response = _rpc(
            ctx, "GetInvocation",
            invocation_pb2.GetInvocationRequest(
                lookup=invocation_pb2.InvocationLookup(invocation_id=invocation_id)
            ),
            invocation_pb2.GetInvocationResponse,
        )
    except grpc.RpcError as error:
        # The initial row is asynchronously created after the Started event.
        # Permission/internal/deadline errors are NOT acceptable observations.
        if allow_missing and error.code() == grpc.StatusCode.NOT_FOUND:
            return None
        raise
    assert len(response.invocation) == 1, f"expected exactly one invocation: {response!r}"
    return response.invocation[0]


def _wait_status(ctx, fixture, status, attempt, allow_missing=False):
    inv = _poll(
        ctx, f"{fixture['invocation_id']} status={status}, attempt={attempt} with metadata",
        lambda: _get_invocation(ctx, fixture["invocation_id"], allow_missing),
        lambda inv: inv is not None and inv.invocation_status == status
        and inv.attempt == attempt and inv.command == "build"
        and inv.user == fixture["workspace"]["BUILD_USER"]
        and inv.host == fixture["workspace"]["BUILD_HOST"]
        and inv.repo_url == fixture["metadata"]["REPO_URL"]
        and inv.commit_sha == fixture["metadata"]["COMMIT_SHA"]
        and inv.branch_name == fixture["metadata"]["BRANCH_NAME"]
        and inv.role == "CI" and inv.has_chunked_event_logs,
    )
    assert inv.invocation_id == fixture["invocation_id"]
    assert inv.created_at_usec > 0 and inv.updated_at_usec >= inv.created_at_usec
    return inv


def _observe_open(ctx, stream, fixture, attempt):
    inv = _wait_status(
        ctx, fixture, invocation_status_pb2.PARTIAL_INVOCATION_STATUS, attempt,
        allow_missing=attempt == 1,
    )
    stream.assert_open()
    expected = (fixture["marker"] + "\n").encode()
    _poll(
        ctx, "exact live chunk 0000 (MemoryKeyValStore suffices; Redis is not required)",
        lambda: _log_chunk(ctx, fixture["invocation_id"], "0000"),
        lambda chunk: chunk.live and chunk.buffer == expected
        and chunk.next_chunk_id == "0000" and not chunk.previous_chunk_id,
    )
    _verify_log(ctx, fixture["invocation_id"], expected, live=True)
    stream.assert_open()  # prove the bytes were visible BEFORE EOF/ACKs
    return inv


def _stream_and_verify(ctx, reconnect=False):
    invocation_id = str(uuid.uuid4())
    fixture = _fixture(invocation_id, True)
    requests = _requests(fixture)
    # WorkspaceStatus flushes the BEP prefix; no finish event is sent yet.
    prefix = requests[:5]
    with _OpenStream(ctx) as stream:
        stream.send(prefix)
        _observe_open(ctx, stream, fixture, attempt=1)
        if not reconnect:
            stream.finish(requests[5:], requests)
        else:
            # Expose exactly which deliberate negative test may emit cancellation
            # warnings. Never exempt other invocations or arbitrary log errors.
            ctx.state["bes_disconnect_invocation_id"] = invocation_id
            stream.disconnect()
            _wait_status(ctx, fixture, invocation_status_pb2.DISCONNECTED_INVOCATION_STATUS, 1)
            _verify_log(ctx, invocation_id, (fixture["marker"] + "\n").encode(), live=False)

    attempt = 2 if reconnect else 1
    if reconnect:
        # Resend the exact same StreamId, sequence numbers, timestamps and bytes
        # from 1: the cancelled transport received ZERO acknowledgements.
        with _OpenStream(ctx) as stream:
            stream.send(prefix)
            _observe_open(ctx, stream, fixture, attempt=2)
            stream.finish(requests[5:], requests)

    # Keep original successful/failed UI fixture mapping at exactly two entries.
    ctx.state.setdefault("bes_stream_invocations", {})[invocation_id] = attempt
    verify_invocation(ctx, invocation_id, attempt=attempt)


def _publish_and_verify(ctx, success):
    invocation_id = str(uuid.uuid4())
    fixture = _fixture(invocation_id, success)
    requests = _requests(fixture)
    stub = publish_build_event_pb2_grpc.PublishBuildEventStub(ctx.channel)
    # The server sends ACKs only after EOF + finalization, so exhaust the request
    # iterator; waiting for an ACK before sending the next event would deadlock.
    responses = list(stub.PublishBuildToolEventStream(iter(requests), timeout=ctx.timeout))
    _verify_acks(responses, requests)
    # Publishing and UI/restart coverage should remain independent from API
    # assertions: retain acknowledged invocation IDs even if verification fails.
    key = "invocation_id" if success else "failed_invocation_id"
    ctx.state[key] = invocation_id
    marker_key = "invocation_marker" if success else "failed_invocation_marker"
    ctx.state[marker_key] = fixture["marker"]
    ctx.state.setdefault("bes_invocations", {})[invocation_id] = success
    verify_invocation(ctx, invocation_id, success)


def verify_persisted(ctx):
    """Parent restart case can reuse every assertion without publishing again."""
    invocations = ctx.state.get("bes_invocations", {})
    assert len(invocations) == 2, "both BES fixtures must be published before the restart check"
    for invocation_id, success in invocations.items():
        verify_invocation(ctx, invocation_id, success)
    streams = ctx.state.get("bes_stream_invocations", {})
    assert sorted(streams.values()) == [1, 2], "live and reconnected BES fixtures must precede restart"
    for invocation_id, attempt in streams.items():
        verify_invocation(ctx, invocation_id, attempt=attempt)


def cases(ctx):
    return [
        ("bes.successful_invocation", lambda: _publish_and_verify(ctx, True)),
        ("bes.failed_invocation", lambda: _publish_and_verify(ctx, False)),
        ("bes.live_partial_to_complete", lambda: _stream_and_verify(ctx)),
        ("bes.transport_disconnect_and_resend", lambda: _stream_and_verify(ctx, reconnect=True)),
    ]
