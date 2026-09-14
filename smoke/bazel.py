"""Offline real Bazel tests, compressed cache round trip and minimal downloads.

All executions have fresh local output roots. Runtime inputs are provisioned by
setup-bazel.sh; neither the workspace nor its test rule fetches repositories.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.json_format import MessageToDict, ParseDict
from google.bytestream import bytestream_pb2 as bs
from google.rpc import status_pb2
from proto import build_event_stream_pb2 as bep
from proto import invocation_pb2, invocation_status_pb2, target_pb2
from proto import remote_execution_pb2 as remote
from proto.api.v1 import common_pb2 as api

VERSION = "8.4.2"
ROOT = Path(__file__).resolve().parents[1]
EXPECTED = b"BuildBuddy real Bazel smoke: exact cached output.\n"
# Alternate independent entropy and zeros: compressible, but the compressed
# stream still exceeds 1 MiB, forcing actual chunked ByteStream traffic.
LARGE = b"".join(hashlib.shake_256(b"BuildBuddy chunk smoke " + i.to_bytes(4, "big")).digest(1024)
                 + bytes(1024) for i in range(2048))
LARGE_HASH = hashlib.sha256(LARGE).hexdigest()
BUILD_TIMEOUT = 30
BUILD_TARGETS = ["//:artifact", "//:receipt"]
TEST_TARGETS = {"//:passing_test": bep.PASSED, "//:failing_test": bep.FAILED}


def _log_entry_class():
    """Wire-compatible projection of Bazel 8.4.2 remote_execution_log.proto.

    Source: https://github.com/bazelbuild/bazel/blob/8.4.2/src/main/protobuf/remote_execution_log.proto
    Use existing protobuf descriptors: no protoc/download at smoke runtime.
    Unknown fields are retained by protobuf; only evidence used below is decoded.
    """
    pool = descriptor_pool.Default()
    name = "smoke.bazel_log.LogEntry"
    try:
        return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
    except KeyError:
        pass
    fd = descriptor_pb2.FileDescriptorProto(name="smoke_bazel_log.proto", package="smoke.bazel_log", syntax="proto3")
    fd.dependency.extend([bs.DESCRIPTOR.name, remote.DESCRIPTOR.name, status_pb2.DESCRIPTOR.name])
    def msg(name, fields):
        m = fd.message_type.add(name=name)
        for number, field_name, kind, repeated in fields:
            f = m.field.add(number=number, name=field_name, label=3 if repeated else 1)
            if isinstance(kind, int):
                f.type = kind
            else:
                f.type = 11
                f.type_name = "." + kind
    p = "smoke.bazel_log."
    msg("Read", [(1, "request", "google.bytestream.ReadRequest", False),
                 (2, "num_reads", 3, False), (3, "bytes_read", 3, False)])
    msg("Write", [(1, "resource_names", 9, True), (2, "num_writes", 3, False), (3, "bytes_sent", 3, False),
                  (4, "response", "google.bytestream.WriteResponse", False)])
    msg("Action", [(1, "request", "build.bazel.remote.execution.v2.GetActionResultRequest", False),
                   (2, "response", "build.bazel.remote.execution.v2.ActionResult", False)])
    msg("Details", [(5, "read", p + "Read", False), (6, "write", p + "Write", False),
                    (8, "get_action_result", p + "Action", False)])
    msg("LogEntry", [(2, "status", "google.rpc.Status", False), (3, "method_name", 9, False),
                     (4, "details", p + "Details", False)])
    pool.AddSerializedFile(fd.SerializeToString())
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(name))


def _read_grpc_log(path):
    """Decode varint-length-delimited LogEntry messages, rejecting truncation."""
    data = path.read_bytes()
    cls = _log_entry_class()
    entries, offset = [], 0
    while offset < len(data):
        size = 0
        for shift in range(0, 70, 7):
            assert offset < len(data), "truncated gRPC log length"
            byte = data[offset]
            offset += 1
            size |= (byte & 127) << shift
            if byte < 128:
                break
        else:
            raise AssertionError("invalid gRPC log length")
        assert size > 0 and offset + size <= len(data), "truncated/empty gRPC log entry"
        entries.append(cls.FromString(data[offset:offset + size]))
        offset += size
    assert entries, "Bazel produced no remote RPC evidence"
    path.with_suffix(".json").write_text(json.dumps([
        MessageToDict(e, preserving_proto_field_name=True) for e in entries], indent=2) + "\n")
    return entries


def _invocation(ctx, invocation_id, artifact_dir, command="build", targets=None, exit_code=0):
    rpc = ctx.channel.unary_unary(
        "/buildbuddy.service.BuildBuddyService/GetInvocation",
        request_serializer=invocation_pb2.GetInvocationRequest.SerializeToString,
        response_deserializer=invocation_pb2.GetInvocationResponse.FromString)
    response = rpc(invocation_pb2.GetInvocationRequest(
        lookup=invocation_pb2.InvocationLookup(invocation_id=invocation_id)), timeout=ctx.timeout)
    (artifact_dir / "invocation.json").write_text(
        json.dumps(MessageToDict(response, preserving_proto_field_name=True), indent=2) + "\n")
    assert len(response.invocation) == 1, "real Bazel invocation was not ingested"
    inv = response.invocation[0]
    assert inv.invocation_id == invocation_id
    assert inv.success == (exit_code == 0)
    assert inv.bazel_exit_code == ("SUCCESS" if exit_code == 0 else "TESTS_FAILED")
    assert inv.invocation_status == invocation_status_pb2.COMPLETE_INVOCATION_STATUS
    assert inv.command == command and list(inv.pattern) == (targets or BUILD_TARGETS)
    events = {e.build_event.WhichOneof("payload"): e.build_event for e in inv.event}
    assert events["started"].started.uuid == invocation_id
    assert events["started"].started.build_tool_version == VERSION
    assert events["finished"].finished.exit_code.code == exit_code
    return inv


def _execute(ctx, phase, verb="build", targets=None, minimal=False, exit_code=0):
    binary = ROOT / ".tools" / f"bazel-{VERSION}"
    install = ROOT / ".tools" / f"bazel-install-{VERSION}"
    assert binary.is_file() and install.is_dir(), "Run scripts/setup-bazel.sh before the timed smoke run"
    if "bazel" not in ctx.state:
        base = ctx.output.resolve() / "bazel"
        base.mkdir()
        workspace = base / "workspace"
        shutil.copytree(ROOT / "fixtures" / "bazel", workspace)
        nonce = str(uuid.uuid4())
        (workspace / "nonce.txt").write_text(nonce + "\n")
        (workspace / "large-input.bin").write_bytes(LARGE)
        build = workspace / "BUILD.bazel"
        build.write_text(build.read_text().replace("SMOKE_NONCE", nonce))
        resolved = workspace / "resolved.bzl"
        resolved.write_text(resolved.read_text().replace("SMOKE_INSTALL_BASE", str(install)))
        ctx.state["bazel"] = {"base": str(base), "builds": []}
    base = Path(ctx.state["bazel"]["base"])
    workspace = base / "workspace"
    artifacts = base / phase
    artifacts.mkdir()
    output_root = artifacts / "output-user-root"
    assert not output_root.exists(), "each Bazel execution requires an empty local output root"
    invocation_id = str(uuid.uuid4())
    command = [str(binary), "--batch", "--ignore_all_rc_files",
        f"--install_base={install}", f"--output_user_root={output_root}",
        "--host_jvm_args=-XX:ActiveProcessorCount=2", "--host_jvm_args=-Xmx512m",
        verb, *(targets or BUILD_TARGETS), "--enable_bzlmod=false", "--enable_workspace=true",
        "--incompatible_autoload_externally=", "--repository_disable_download",
        "--experimental_resolved_file_instead_of_workspace=resolved.bzl",
        "--platforms=//:local", "--host_platform=//:local",
        "--jobs=2", "--loading_phase_threads=2", "--spawn_strategy=local", "--test_strategy=standalone",
        "--color=no", "--curses=no", "--noshow_progress", "--disk_cache=",
        f"--remote_cache=grpc://{ctx.grpc_target}", "--remote_timeout=5",
        "--remote_upload_local_results=true", "--remote_accept_cached=true",
        "--remote_cache_compression", "--experimental_remote_cache_compression_threshold=1024",
        "--remote_download_minimal" if minimal else "--remote_download_outputs=all",
        "--remote_cache_async=false", f"--remote_grpc_log={artifacts / 'grpc.log'}",
        f"--bes_backend=grpc://{ctx.grpc_target}", f"--bes_results_url={ctx.http_url}/invocation/",
        "--bes_upload_mode=wait_for_upload_complete", "--bes_timeout=5s",
        f"--invocation_id={invocation_id}",
        f"--build_event_json_file={artifacts / 'bep.jsonl'}",
        f"--profile={artifacts / 'profile.json.gz'}",
        "--build_metadata=DISABLE_COMMIT_STATUS_REPORTING=true"]
    if verb == "test":
        command += ["--test_output=all", "--nocache_test_results"]
    (artifacts / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(base), "LANG": "C.UTF-8", "TMPDIR": str(base)}
    started = time.monotonic()
    with (artifacts / "bazel.log").open("wb") as log:
        # Stay in the worker's supervised group: the hard deadline also reaps
        # action descendants. Never detach a JVM from the supervisor.
        process = subprocess.Popen(command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = process.wait(timeout=BUILD_TIMEOUT)
        except BaseException:
            process.kill()
            process.wait()
            raise
    elapsed = time.monotonic() - started
    text = (artifacts / "bazel.log").read_text(errors="replace")
    assert rc == exit_code, f"Bazel {phase} exited {rc}, expected {exit_code}:\n{text[-6000:]}"
    output_path = (workspace / "bazel-bin").resolve()
    assert output_path.is_relative_to(output_root), "Bazel output symlink points at a previous execution"
    inv = _invocation(ctx, invocation_id, artifacts, verb, targets, exit_code)
    item = {"phase": phase, "seconds": round(elapsed, 4), "invocation_id": invocation_id,
            "exit_code": rc, "output_user_root": str(output_root)}
    return artifacts, output_path, text, inv, item


def _record(ctx, item):
    state = ctx.state["bazel"]
    state["builds"].append(item)
    (Path(state["base"]) / "summary.json").write_text(json.dumps(state["builds"], indent=2) + "\n")


def _compressed_transfer(entries, direction):
    """Require the large digest on zstd ByteStream, not just a client flag."""
    suffix = f"compressed-blobs/zstd/{LARGE_HASH}/{len(LARGE)}"
    matches = []
    for e in entries:
        if direction == "write" and e.details.HasField("write"):
            d = e.details.write
            if any(r.endswith(suffix) for r in d.resource_names):
                assert e.status.code == 0, str(e)
                assert 1024 * 1024 < d.bytes_sent < len(LARGE), "compressed upload must still exceed 1 MiB"
                assert d.num_writes > 1, "large upload was not chunked into multiple requests"
                matches.append({"bytes": d.bytes_sent, "messages": d.num_writes})
        if direction == "read" and e.details.HasField("read"):
            d = e.details.read
            if d.request.resource_name.endswith(suffix):
                assert e.status.code == 0, str(e)
                assert 1024 * 1024 < d.bytes_read < len(LARGE), "compressed download must still exceed 1 MiB"
                assert d.num_reads > 1, "large download was not chunked into multiple responses"
                matches.append({"bytes": d.bytes_read, "messages": d.num_reads})
    assert matches, f"no successful compressed {direction} for the large output digest"
    return matches


def _run_build(ctx, phase):
    if phase > 1:
        assert len(ctx.state["bazel"]["builds"]) == phase - 1, "earlier cache phase must pass first"
    minimal = phase == 3
    artifacts, output, text, inv, item = _execute(ctx, f"build-{phase}", minimal=minimal)
    entries = _read_grpc_log(artifacts / "grpc.log")
    hits = sum(int(n) for n in re.findall(r"(\d+) remote cache hits?\b", text))
    actions = [e for e in entries if e.details.HasField("get_action_result")]
    if phase == 1:
        assert hits == 0, f"cold build unexpectedly hit remote cache: {text}"
        assert re.search(r"\b3 local\b", text), f"cold build did not execute all three actions: {text}"
        assert len(actions) == 3 and all(e.status.code == 5 for e in actions), "expected three AC misses"
        item["large_compressed_upload"] = _compressed_transfer(entries, "write")
    else:
        assert hits == 3, f"fresh root must report three remote cache hits: {text}"
        assert not re.search(r"\b[1-9]\d* local\b", text), f"cache build executed locally: {text}"
        assert len(actions) == 3 and all(e.status.code == 0 for e in actions), "expected three successful AC RPCs"
    expected_files = {"artifact.txt": EXPECTED, "large.txt": LARGE,
                      "receipt.txt": f"{LARGE_HASH}  -\n".encode()}
    if minimal:
        # Check both filesystem and wire traffic. A missing output alone could
        # hide a broken build; AC results must contain its correct digest too.
        result_files = [f for e in actions for f in e.details.get_action_result.response.output_files]
        assert all(not f.contents for f in result_files), 'minimal build downloaded inline output bytes via AC'
        result_digests = {f.digest.hash: f.digest.size_bytes for f in result_files}
        for name, data in expected_files.items():
            assert not (output / name).exists(), f"minimal build materialized {name}"
            assert result_digests.get(hashlib.sha256(data).hexdigest()) == len(data), f"AC omitted {name}"
        blob_reads = [e.details.read.request.resource_name for e in entries if e.details.HasField("read")]
        assert not any(LARGE_HASH in r for r in blob_reads), "minimal build downloaded the large intermediate"
        item["minimal_absent_outputs"] = list(expected_files)
        item["minimal_read_resources"] = blob_reads
    else:
        for name, data in expected_files.items():
            actual = (output / name).read_bytes()
            assert actual == data, f"{name}: exact output mismatch"
            (artifacts / name).write_bytes(actual)
        if phase == 2:
            item["large_compressed_download"] = _compressed_transfer(entries, "read")
    stats = None
    if inv.HasField("cache_stats") and inv.cache_stats.ListFields():
        stats = MessageToDict(inv.cache_stats, preserving_proto_field_name=True)
        if phase == 1:
            assert inv.cache_stats.action_cache_uploads >= 3, stats
            assert inv.cache_stats.action_cache_hits == 0, stats
        else:
            assert inv.cache_stats.action_cache_hits >= 3, stats
    item.update(remote_cache_hits=hits, cache_stats=stats, large_sha256=LARGE_HASH, large_size_bytes=len(LARGE))
    _record(ctx, item)


def _assert_test_events(events):
    """Check per-label result AND summary; invocation exit status is insufficient."""
    results, summaries = {}, {}
    for event in events:
        payload = event.WhichOneof("payload")
        if payload == "test_result":
            label = event.id.test_result.label
            assert label not in results, f"duplicate test result: {label}"
            assert not event.test_result.cached_locally, f"test did not execute: {label}"
            results[label] = event.test_result.status
            assert event.test_result.test_action_output, f"no test log output: {label}"
        if payload == "test_summary":
            label = event.id.test_summary.label
            assert label not in summaries, f"duplicate test summary: {label}"
            summary = event.test_summary
            assert summary.total_run_count == 1, f"test did not run exactly once: {label}"
            summaries[label] = summary.overall_status
    assert results == TEST_TARGETS, f"wrong or missing testResult events: {results}"
    assert summaries == TEST_TARGETS, f"wrong or missing testSummary events: {summaries}"


def _run_tests(ctx):
    artifacts, output, text, inv, item = _execute(
        ctx, "test", verb="test", targets=list(TEST_TARGETS), exit_code=3)
    # GetInvocation is paginated: test events are indexed into target groups,
    # not included in inv.event. Check the emitted BEP, then compare its actual
    # test payloads against the target detail RPC used by the app.
    emitted = [ParseDict(json.loads(line), bep.BuildEvent())
               for line in (artifacts / "bep.jsonl").read_text().splitlines()]
    _assert_test_events(emitted)
    results = {e.id.test_result.label: e for e in emitted if e.HasField("test_result")}
    summaries = {e.id.test_summary.label: e.test_summary for e in emitted if e.HasField("test_summary")}
    # Target detail RPC is the browser's source of test results and summaries.
    rpc = ctx.channel.unary_unary("/buildbuddy.service.BuildBuddyService/GetTarget",
        request_serializer=target_pb2.GetTargetRequest.SerializeToString,
        response_deserializer=target_pb2.GetTargetResponse.FromString)
    for label, expected in TEST_TARGETS.items():
        response = rpc(target_pb2.GetTargetRequest(invocation_id=inv.invocation_id, target_label=label), timeout=ctx.timeout)
        (artifacts / (label[3:] + "-target.json")).write_text(
            json.dumps(MessageToDict(response, preserving_proto_field_name=True), indent=2) + "\n")
        targets = [t for g in response.target_groups for t in g.targets]
        assert len(targets) == 1 and targets[0].metadata.label == label, str(response)
        target = targets[0]
        assert target.status == (api.PASSED if expected == bep.PASSED else api.FAILED), str(target)
        assert target.test_summary == summaries[label], "ingested testSummary differs from Bazel BEP"
        assert len(target.test_result_events) == 1, str(target)
        result = target.test_result_events[0]
        assert result.test_result == results[label].test_result, "ingested testResult differs from Bazel BEP"
        assert result.id == results[label].id, "ingested testResult ID differs from Bazel BEP"
    ctx.state["bazel"]["test_invocation_id"] = inv.invocation_id
    item["test_results"] = {label: bep.TestStatus.Name(status) for label, status in TEST_TARGETS.items()}
    _record(ctx, item)


def browser_targets_check(ctx, page):
    """Parent calls this in its existing browser AFTER bazel.cases(ctx).

    Does not own/close the page or browser; existing pageerror collection and
    offline routing remain active. Saves DOM/screenshot even on assertion failure.
    """
    from playwright.sync_api import expect
    state = ctx.state["bazel"]
    artifacts = Path(state["base"]) / "test"
    try:
        page.goto(ctx.http_url + "/invocation/" + state["test_invocation_id"], wait_until="domcontentloaded")
        page.locator('a.tab[href="#targets"]').click()
        expect(page.locator('a.tab[href="#targets"]')).to_have_class("tab selected")
        failure = page.locator(".invocation-targets-card.card-failure")
        success = page.locator(".invocation-targets-card.card-success").filter(has_text="test passed")
        expect(failure).to_contain_text("1 test failed")
        expect(failure.locator(".target-label")).to_have_text("//:failing_test")
        expect(success).to_contain_text("1 test passed")
        expect(success.locator(".target-label")).to_have_text("//:passing_test")
    finally:
        (artifacts / "targets-browser.txt").write_text(page.locator("body").inner_text())
        page.screenshot(path=str(artifacts / "targets-browser.png"), full_page=True)


def cases(ctx):
    return [
        ("bazel.cold_build_upload_and_bes", lambda: _run_build(ctx, 1)),
        ("bazel.fresh_root_remote_hit_and_bes", lambda: _run_build(ctx, 2)),
        ("bazel.fresh_root_minimal_download", lambda: _run_build(ctx, 3)),
        ("bazel.real_passing_and_failing_tests", lambda: _run_tests(ctx)),
    ]
