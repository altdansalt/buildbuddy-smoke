"""Synthetic Bazel BES streams, invocation metadata, and durable console logs.

Uses BuildBuddy's native gRPC service (registered by server/libmain/libmain.go),
not the separately authenticated public API. Only the request/response protos
are needed, avoiding the large buildbuddy_service.proto dependency closure.
"""

import hashlib
import time
import uuid

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


def verify_invocation(ctx, invocation_id, success=True):
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
    }
    for field, value in expected.items():
        actual = getattr(inv, field)
        assert actual == value, f"{invocation_id}: {field}: expected {value!r}, got {actual!r}"
    assert list(inv.pattern) == [_PATTERN], f"unexpected patterns: {list(inv.pattern)!r}"
    assert inv.last_chunk_id, "completed invocation has no stored log chunk ID"
    assert inv.created_at_usec > 0 and inv.updated_at_usec >= inv.created_at_usec

    # Verify events are fetched from the BEP blob as well as the summary DB row.
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

    log = _rpc(
        ctx,
        "GetEventLogChunk",
        eventlog_pb2.GetEventLogChunkRequest(
            # Omit chunk_id to resolve the real final chunk. In this version the
            # DB's last_chunk_id can retain the pre-close "ffff" sentinel even
            # though Close has successfully persisted the first log chunk.
            invocation_id=invocation_id, min_lines=100, type=eventlog_pb2.BUILD_LOG,
        ),
        eventlog_pb2.GetEventLogChunkResponse,
    )
    assert not log.live, "completed build returned a live (unpersisted) log chunk"
    assert not log.previous_chunk_id, "tiny log unexpectedly spans chunks"
    assert log.buffer == fixture["log"].encode(), (
        f"{invocation_id}: exact log mismatch: expected {fixture['log']!r}, got {log.buffer!r}"
    )
    # v2.303.0 returns a next-chunk cursor even at EOF. Following that cursor
    # for a completed invocation must return an empty, non-live terminal page.
    if log.next_chunk_id:
        end = _rpc(
            ctx,
            "GetEventLogChunk",
            eventlog_pb2.GetEventLogChunkRequest(
                invocation_id=invocation_id, chunk_id=log.next_chunk_id,
                type=eventlog_pb2.BUILD_LOG,
            ),
            eventlog_pb2.GetEventLogChunkResponse,
        )
        assert not end.buffer and not end.next_chunk_id and not end.live, (
            "completed log did not terminate after its sole chunk"
        )
        assert end.previous_chunk_id == "0000", "tiny log should have exactly one durable chunk"
    return inv


def _publish_and_verify(ctx, success):
    invocation_id = str(uuid.uuid4())
    fixture = _fixture(invocation_id, success)
    requests = _requests(fixture)
    stub = publish_build_event_pb2_grpc.PublishBuildEventStub(ctx.channel)
    # The server sends ACKs only after EOF + finalization, so exhaust the request
    # iterator; waiting for an ACK before sending the next event would deadlock.
    responses = list(stub.PublishBuildToolEventStream(iter(requests), timeout=ctx.timeout))
    expected_sequences = list(range(1, len(requests) + 1))
    actual_sequences = [r.sequence_number for r in responses]
    assert actual_sequences == expected_sequences, (
        f"BES ACKs must be complete, consecutive and monotonic: {actual_sequences!r}"
    )
    for response in responses:
        assert response.stream_id == requests[0].ordered_build_event.stream_id, (
            f"BES ACK {response.sequence_number} has the wrong stream ID"
        )
    verify_invocation(ctx, invocation_id, success)
    key = "invocation_id" if success else "failed_invocation_id"
    ctx.state[key] = invocation_id
    marker_key = "invocation_marker" if success else "failed_invocation_marker"
    ctx.state[marker_key] = fixture["marker"]
    ctx.state.setdefault("bes_invocations", {})[invocation_id] = success


def verify_persisted(ctx):
    """Parent restart case can reuse every assertion without publishing again."""
    invocations = ctx.state.get("bes_invocations", {})
    assert len(invocations) == 2, "both BES fixtures must pass before the restart check"
    for invocation_id, success in invocations.items():
        verify_invocation(ctx, invocation_id, success)


def cases(ctx):
    return [
        ("bes.successful_invocation", lambda: _publish_and_verify(ctx, True)),
        ("bes.failed_invocation", lambda: _publish_and_verify(ctx, False)),
    ]
