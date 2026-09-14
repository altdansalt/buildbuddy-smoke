"""Real, offline Bazel BES + remote-cache round trip (setup is separate).

Both builds use --batch and different output_user_root directories, so the
second build cannot reuse an action result from Bazel's local output cache.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from google.protobuf.json_format import MessageToDict
from proto import invocation_pb2
from proto import invocation_status_pb2

VERSION = "8.4.2"
ROOT = Path(__file__).resolve().parents[1]
EXPECTED = b"BuildBuddy real Bazel smoke: exact cached output.\n"
BUILD_TIMEOUT = 30


def _invocation(ctx, invocation_id, artifact_dir):
    rpc = ctx.channel.unary_unary(
        "/buildbuddy.service.BuildBuddyService/GetInvocation",
        request_serializer=invocation_pb2.GetInvocationRequest.SerializeToString,
        response_deserializer=invocation_pb2.GetInvocationResponse.FromString,
    )
    response = rpc(invocation_pb2.GetInvocationRequest(
        lookup=invocation_pb2.InvocationLookup(invocation_id=invocation_id)),
        timeout=ctx.timeout)
    (artifact_dir / "invocation.json").write_text(
        json.dumps(MessageToDict(response, preserving_proto_field_name=True), indent=2) + "\n")
    assert len(response.invocation) == 1, "real Bazel invocation was not ingested"
    inv = response.invocation[0]
    assert inv.invocation_id == invocation_id
    assert inv.success and inv.bazel_exit_code == "SUCCESS"
    assert inv.invocation_status == invocation_status_pb2.COMPLETE_INVOCATION_STATUS
    assert inv.command == "build" and list(inv.pattern) == ["//:artifact"]
    events = {e.build_event.WhichOneof("payload"): e.build_event for e in inv.event}
    assert events["started"].started.uuid == invocation_id
    assert events["started"].started.build_tool_version == VERSION
    assert events["finished"].finished.exit_code.code == 0
    return inv


def _run_build(ctx, phase):
    binary = ROOT / ".tools" / f"bazel-{VERSION}"
    install = ROOT / ".tools" / f"bazel-install-{VERSION}"
    assert binary.is_file() and install.is_dir(), "Run scripts/setup-bazel.sh before the timed smoke run"
    if phase == 1:
        base = ctx.output.resolve() / "bazel"
        base.mkdir()
        workspace = base / "workspace"
        shutil.copytree(ROOT / "fixtures" / "bazel", workspace)
        (workspace / "nonce.txt").write_text(str(uuid.uuid4()) + "\n")
        ctx.state["bazel"] = {"base": str(base), "builds": []}
    state = ctx.state["bazel"]
    assert len(state["builds"]) == phase - 1, "Bazel cold build must pass before cache-hit build"
    base = Path(state["base"])
    workspace = base / "workspace"
    artifacts = base / f"build-{phase}"
    artifacts.mkdir()
    output_root = artifacts / "output-user-root"
    assert not output_root.exists(), "each Bazel build requires an empty local output root"
    invocation_id = str(uuid.uuid4())
    command = [str(binary), "--batch", "--ignore_all_rc_files",
        f"--install_base={install}", f"--output_user_root={output_root}",
        "--host_jvm_args=-XX:ActiveProcessorCount=2", "--host_jvm_args=-Xmx512m",
        "build", "//:artifact", "--enable_bzlmod=false", "--enable_workspace=true",
        "--incompatible_autoload_externally=", "--repository_disable_download",
        "--experimental_resolved_file_instead_of_workspace=resolved.bzl",
        "--platforms=//:local", "--host_platform=//:local",
        "--jobs=2", "--loading_phase_threads=2", "--spawn_strategy=local",
        "--color=no", "--curses=no", "--noshow_progress", "--disk_cache=",
        f"--remote_cache=grpc://{ctx.grpc_target}", "--remote_timeout=5",
        "--remote_upload_local_results=true", "--remote_accept_cached=true",
        "--remote_download_outputs=all", "--remote_cache_async=false",
        f"--bes_backend=grpc://{ctx.grpc_target}", f"--bes_results_url={ctx.http_url}/invocation/",
        "--bes_upload_mode=wait_for_upload_complete", "--bes_timeout=5s",
        f"--invocation_id={invocation_id}",
        f"--build_event_json_file={artifacts / 'bep.jsonl'}",
        f"--profile={artifacts / 'profile.json.gz'}",
        "--build_metadata=DISABLE_COMMIT_STATUS_REPORTING=true"]
    (artifacts / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    # No host credentials, user Bazel rc, proxy, or language toolchain needed.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(base), "LANG": "C.UTF-8",
           "TMPDIR": str(base)}
    started = time.monotonic()
    with (artifacts / "bazel.log").open("wb") as log:
        # Inherit the worker's supervised process group: detaching here would
        # let the batch JVM escape the outer hard wall-clock deadline.
        process = subprocess.Popen(command, cwd=workspace, env=env, stdout=log,
                                   stderr=subprocess.STDOUT)
        try:
            rc = process.wait(timeout=BUILD_TIMEOUT)
        except BaseException:
            # Kill/reap the direct batch process on a local timeout. Any action
            # descendants remain in the worker group for supervisor cleanup.
            process.kill()
            process.wait()
            raise
    elapsed = time.monotonic() - started
    text = (artifacts / "bazel.log").read_text(errors="replace")
    assert rc == 0, f"Bazel build {phase} exited {rc}:\n{text[-6000:]}"
    hits = sum(int(n) for n in re.findall(r"(\d+) remote cache hits?\b", text))
    if phase == 1:
        assert hits == 0, f"first build unexpectedly hit remote cache: {text}"
        assert re.search(r"\b1 local\b", text), f"first build did not execute its action: {text}"
    else:
        assert hits == 1, f"fresh-output-root build must report one remote cache hit: {text}"
        assert not re.search(r"\b[1-9]\d* local\b", text), f"cache hit build executed locally: {text}"
    output_path = (workspace / "bazel-bin" / "artifact.txt").resolve()
    assert output_path.is_relative_to(output_root), "Bazel output symlink still points at a previous build"
    actual = output_path.read_bytes()
    (artifacts / "artifact.txt").write_bytes(actual)
    assert actual == EXPECTED, f"build {phase}: exact output mismatch: {actual!r}"
    inv = _invocation(ctx, invocation_id, artifacts)
    # The standalone server can return a present-but-empty CacheStats message
    # when its collector is disabled. Record that as unavailable; client-side
    # hit assertions above are always required, never optional.
    stats = None
    if inv.HasField("cache_stats") and inv.cache_stats.ListFields():
        stats = MessageToDict(inv.cache_stats, preserving_proto_field_name=True)
        if phase == 1:
            assert inv.cache_stats.action_cache_uploads >= 1, f"no action-cache upload: {stats}"
            assert inv.cache_stats.action_cache_hits == 0, f"cold invocation had cache hits: {stats}"
        else:
            assert inv.cache_stats.action_cache_hits >= 1, f"server did not count cache hit: {stats}"
    item = {"phase": phase, "seconds": round(elapsed, 4), "invocation_id": invocation_id,
            "remote_cache_hits": hits, "output_user_root": str(output_root),
            "cache_stats": stats}
    state["builds"].append(item)
    (base / "summary.json").write_text(json.dumps(state["builds"], indent=2) + "\n")


def cases(ctx):
    return [
        ("bazel.cold_build_upload_and_bes", lambda: _run_build(ctx, 1)),
        ("bazel.fresh_root_remote_hit_and_bes", lambda: _run_build(ctx, 2)),
    ]
