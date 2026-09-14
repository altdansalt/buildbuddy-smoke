"""Fast black-box cache checks for the default v2.303.0 app configuration.

Contract: cases(ctx) -> [(name, zero_argument_callable), ...]; ctx supplies a
live grpc channel, timeout (seconds), and mutable state dict. No pytest needed.
Requires grpcio, generated proto modules, and zstandard (compression checks).

Source expectations: server/remote_cache/{capabilities_server,
content_addressable_storage_server,byte_stream_server,action_cache_server} in
BuildBuddy v2.303.0. In particular QueryWriteStatus is UNIMPLEMENTED, missing
GetTree nodes are FAILED_PRECONDITION, and malformed batch digests fail the
whole RPC, whereas data/digest mismatches have per-entry INVALID_ARGUMENT.

The persistence case writes JSON-serializable ctx.state['cache_restart']; call
restart_probe(ctx) against the replacement channel after restarting the app.
All other cases are independent and can continue after an earlier failure.
"""

import hashlib
import uuid
from collections import Counter

import grpc

from proto import remote_execution_pb2 as re, remote_execution_pb2_grpc as re_grpc
from google.bytestream import bytestream_pb2 as bs, bytestream_pb2_grpc as bs_grpc


OK = grpc.StatusCode.OK.value[0]
INVALID_ARGUMENT = grpc.StatusCode.INVALID_ARGUMENT.value[0]
NOT_FOUND = grpc.StatusCode.NOT_FOUND.value[0]
UNIMPLEMENTED = grpc.StatusCode.UNIMPLEMENTED.value[0]


def digest(data):
    return re.Digest(hash=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def _key(d):
    return d.hash, d.size_bytes


def _fresh(label):
    return ("buildbuddy-smoke:" + label + ":" + uuid.uuid4().hex).encode()


def _resource(d, instance="", upload=False, compressed=False):
    segments = [instance] if instance else []
    if upload:
        segments.extend(["uploads", str(uuid.uuid4())])
    segments.extend(["compressed-blobs/zstd" if compressed else "blobs", d.hash,
                     str(d.size_bytes)])
    return "/".join(segments)


def _error(code, operation):
    try:
        operation()
    except grpc.RpcError as exc:
        assert exc.code() == code, (code, exc.code(), exc.details())
        return exc
    raise AssertionError("Expected gRPC " + code.name)


def _responses(response, expected_digests):
    rows = response.responses
    assert Counter(_key(r.digest) for r in rows) == Counter(
        _key(d) for d in expected_digests), response
    return {_key(r.digest): r for r in rows}


class _Cache:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cas = re_grpc.ContentAddressableStorageStub(ctx.channel)
        self.ac = re_grpc.ActionCacheStub(ctx.channel)
        self.bs = bs_grpc.ByteStreamStub(ctx.channel)

    def put(self, *blobs, instance=""):
        ds = [digest(b) for b in blobs]
        rsp = self.cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(
            instance_name=instance, digest_function=re.DigestFunction.SHA256,
            requests=[re.BatchUpdateBlobsRequest.Request(digest=d, data=b)
                      for d, b in zip(ds, blobs)]), timeout=self.ctx.timeout)
        rows = _responses(rsp, ds)
        for d in ds:
            assert rows[_key(d)].status.code == OK, rsp
        return ds

    def missing(self, ds, instance=""):
        return self.cas.FindMissingBlobs(re.FindMissingBlobsRequest(
            instance_name=instance, blob_digests=ds,
            digest_function=re.DigestFunction.SHA256), timeout=self.ctx.timeout)

    def batch_read(self, ds, instance="", compressors=()):
        rsp = self.cas.BatchReadBlobs(re.BatchReadBlobsRequest(
            instance_name=instance, digests=ds, acceptable_compressors=compressors,
            digest_function=re.DigestFunction.SHA256), timeout=self.ctx.timeout)
        return _responses(rsp, ds)

    def read(self, d, instance="", offset=0, limit=0, compressed=False):
        return b"".join(r.data for r in self.bs.Read(bs.ReadRequest(
            resource_name=_resource(d, instance, compressed=compressed),
            read_offset=offset, read_limit=limit), timeout=self.ctx.timeout))

    def write(self, data, instance="", compressed=False, expected_digest=None):
        d = expected_digest if expected_digest is not None else digest(data)
        name = _resource(d, instance, upload=True, compressed=compressed)
        # Small, uneven chunks exercise stream offsets, omission of subsequent
        # resource names, and a separate zero-byte finish message.
        def chunks():
            for pos in range(0, len(data), 16381):
                yield bs.WriteRequest(resource_name=name if pos == 0 else "",
                                      write_offset=pos, data=data[pos:pos + 16381])
            yield bs.WriteRequest(resource_name=name if not data else "",
                                  write_offset=len(data), finish_write=True)
        rsp = self.bs.Write(chunks(), timeout=self.ctx.timeout)
        assert rsp.committed_size == len(data), rsp
        return d, name

    def get_action(self, d, instance="", **kwargs):
        return self.ac.GetActionResult(re.GetActionResultRequest(
            instance_name=instance, action_digest=d,
            digest_function=re.DigestFunction.SHA256, **kwargs), timeout=self.ctx.timeout)

    def update_action(self, d, result, instance=""):
        return self.ac.UpdateActionResult(re.UpdateActionResultRequest(
            instance_name=instance, action_digest=d, action_result=result,
            digest_function=re.DigestFunction.SHA256), timeout=self.ctx.timeout)

    def action_fixture(self, instance=""):
        data = _fresh("action-output") + b"\x00\xff\n"
        output, = self.put(data, instance=instance)
        command = re.Command(arguments=["/bin/echo", uuid.uuid4().hex],
                             output_paths=["out.bin"])
        command_d, = self.put(command.SerializeToString(), instance=instance)
        action = re.Action(command_digest=command_d, input_root_digest=digest(b""))
        action_d, = self.put(action.SerializeToString(), instance=instance)
        result = re.ActionResult(exit_code=0, stdout_raw=b"smoke stdout\n",
                                 stderr_raw=b"smoke stderr\n",
                                 output_files=[re.OutputFile(path="out.bin",
                                     digest=output, is_executable=True)])
        return action_d, result, data


def restart_probe(ctx):
    """Verify persisted CAS bytes and the exact AC result, not just existence."""
    fixture = ctx.state["cache_restart"]
    c = _Cache(ctx)
    d = re.Digest(**fixture["digest"])
    instance = fixture["instance_name"]
    data = bytes.fromhex(fixture["data_hex"])
    assert c.read(d, instance=instance) == data
    rows = c.batch_read([d], instance=instance)
    assert rows[_key(d)].status.code == OK, rows
    assert rows[_key(d)].data == data
    assert not c.missing([d], instance=instance).missing_blob_digests
    action = c.get_action(re.Digest(**fixture["action_digest"]), instance=instance)
    assert action == re.ActionResult.FromString(bytes.fromhex(fixture["action_result_hex"]))


def cases(ctx):
    c = _Cache(ctx)

    def capabilities():
        rsp = re_grpc.CapabilitiesStub(ctx.channel).GetCapabilities(
            re.GetCapabilitiesRequest(), timeout=ctx.timeout)
        assert rsp.HasField("cache_capabilities"), rsp
        cap = rsp.cache_capabilities
        assert set(cap.digest_functions) == {
            re.DigestFunction.SHA256, re.DigestFunction.SHA384,
            re.DigestFunction.SHA512, re.DigestFunction.SHA1,
            re.DigestFunction.BLAKE3}, rsp
        assert cap.action_cache_update_capabilities.update_enabled, rsp
        assert set(cap.supported_compressors) == {re.Compressor.IDENTITY, re.Compressor.ZSTD}, rsp
        assert set(cap.supported_batch_update_compressors) == {
            re.Compressor.IDENTITY, re.Compressor.ZSTD}, rsp
        assert cap.max_batch_total_size_bytes == 0, rsp
        assert cap.symlink_absolute_path_strategy == re.SymlinkAbsolutePathStrategy.ALLOWED, rsp
        assert (rsp.low_api_version.major, rsp.low_api_version.minor) == (2, 0), rsp
        assert (rsp.high_api_version.major, rsp.high_api_version.minor) == (2, 11), rsp

    def batch_mixed():
        a, b, absent = _fresh("batch-a") + b"\x00\xff", _fresh("batch-b"), digest(_fresh("missing"))
        da, db = c.put(a, b)
        empty = digest(b"")
        rsp = c.missing([da, db, absent, empty])
        assert list(rsp.missing_blob_digests) == [absent], rsp
        rows = c.batch_read([da, absent, db, empty])
        for d, data in [(da, a), (db, b), (empty, b"")]:
            assert rows[_key(d)].status.code == OK, rows
            assert rows[_key(d)].data == data, rows
            assert rows[_key(d)].compressor == re.Compressor.IDENTITY, rows
        assert rows[_key(absent)].status.code == NOT_FOUND, rows
        assert rows[_key(absent)].data == b"", rows

    def empty_batches():
        assert not c.missing([]).missing_blob_digests
        assert c.batch_read([]) == {}
        assert c.put() == []
        empty, = c.put(b"")
        assert c.read(empty) == b""
        c.write(b"")

    def batch_invalid_data():
        good = _fresh("valid-entry")
        dg = digest(good)
        wrong_hash = digest(_fresh("wrong-hash-expected"))
        wrong_size_data = _fresh("wrong-size")
        wrong_size = digest(wrong_size_data)
        wrong_size.size_bytes += 1
        unsupported_data = _fresh("unsupported")
        du = digest(unsupported_data)
        rsp = c.cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(requests=[
            re.BatchUpdateBlobsRequest.Request(digest=wrong_hash, data=b"wrong bytes"),
            re.BatchUpdateBlobsRequest.Request(digest=dg, data=good),
            re.BatchUpdateBlobsRequest.Request(digest=wrong_size, data=wrong_size_data),
            re.BatchUpdateBlobsRequest.Request(digest=du, data=unsupported_data,
                                               compressor=re.Compressor.DEFLATE),
        ]), timeout=ctx.timeout)
        rows = _responses(rsp, [dg, wrong_hash, wrong_size, du])
        for d, code in [(dg, OK), (wrong_hash, INVALID_ARGUMENT),
                        (wrong_size, INVALID_ARGUMENT), (du, UNIMPLEMENTED)]:
            assert rows[_key(d)].status.code == code, rsp
        assert c.read(dg) == good
        missing = c.missing([wrong_hash, wrong_size, du]).missing_blob_digests
        assert {_key(d) for d in missing} == {_key(wrong_hash), _key(wrong_size), _key(du)}
        # A bad declared size must not accidentally persist the correctly-sized blob either.
        assert list(c.missing([digest(wrong_size_data)]).missing_blob_digests) == [digest(wrong_size_data)]

    def malformed_digests():
        for d in [re.Digest(hash="abc", size_bytes=1),
                  re.Digest(hash="g" * 64, size_bytes=1),
                  re.Digest(hash="A" * 64, size_bytes=1),
                  re.Digest(hash="1" * 64, size_bytes=-1),
                  re.Digest(hash="1" * 64, size_bytes=0)]:
            _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.batch_read([d]))
            _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.cas.BatchUpdateBlobs(
                re.BatchUpdateBlobsRequest(requests=[re.BatchUpdateBlobsRequest.Request(
                    digest=d, data=b"x")]), timeout=ctx.timeout))

    def digest_functions():
        # Explicit SHA1 and SHA512 check digest-function dispatch rather than
        # merely checking that capabilities advertises them.
        for algorithm, function in [("sha1", re.DigestFunction.SHA1),
                                    ("sha512", re.DigestFunction.SHA512)]:
            data = _fresh(algorithm)
            d = re.Digest(hash=hashlib.new(algorithm, data).hexdigest(), size_bytes=len(data))
            rsp = c.cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(
                digest_function=function,
                requests=[re.BatchUpdateBlobsRequest.Request(digest=d, data=data)]), timeout=ctx.timeout)
            assert _responses(rsp, [d])[_key(d)].status.code == OK, rsp
            read = c.cas.BatchReadBlobs(re.BatchReadBlobsRequest(
                digest_function=function, digests=[d]), timeout=ctx.timeout)
            row = _responses(read, [d])[_key(d)]
            assert row.status.code == OK and row.data == data, read

    def bytestream_roundtrip():
        # Larger than the default 256000-byte server read buffer, yet < 1 MiB.
        data = _fresh("stream") + bytes(range(256)) * 2200
        d, name = c.write(data)
        packets = list(c.bs.Read(bs.ReadRequest(resource_name=_resource(d)), timeout=ctx.timeout))
        assert len(packets) >= 2, "Expected a multi-response ByteStream read"
        assert b"".join(p.data for p in packets) == data
        assert c.read(d, offset=137, limit=7919) == data[137:137 + 7919]
        assert c.read(d, offset=len(data) - 79) == data[-79:]
        assert c.read(d, offset=len(data)) == b""
        assert c.read(d, offset=len(data) - 9, limit=100) == data[-9:]
        row = c.batch_read([d])[_key(d)]
        assert row.status.code == OK and row.data == data, row
        # Deduplicated writes still acknowledge the complete uncompressed size.
        c.write(data)
        _error(grpc.StatusCode.UNIMPLEMENTED, lambda: c.bs.QueryWriteStatus(
            bs.QueryWriteStatusRequest(resource_name=name), timeout=ctx.timeout))

    def invalid_reads():
        d, = c.put(_fresh("invalid-read"))
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.read(digest(_fresh("read-missing"))))
        for request, code in [
                (bs.ReadRequest(), grpc.StatusCode.INVALID_ARGUMENT),
                (bs.ReadRequest(resource_name="not-a-resource"), grpc.StatusCode.INVALID_ARGUMENT),
                (bs.ReadRequest(resource_name=_resource(d), read_offset=-1), grpc.StatusCode.OUT_OF_RANGE),
                (bs.ReadRequest(resource_name=_resource(d), read_limit=-1), grpc.StatusCode.OUT_OF_RANGE)]:
            _error(code, lambda: list(c.bs.Read(request, timeout=ctx.timeout)))

    def invalid_writes():
        # Never reuse an existing digest: already-present blobs may short-circuit
        # validation, by design. Every failure also verifies absence in CAS.
        for failure in ("missing-resource", "initial-offset", "offset-gap", "changed-resource",
                        "checksum", "size"):
            expected = _fresh("invalid-write-" + failure)
            d = digest(expected)
            if failure == "size":
                d.size_bytes += 1
            name = _resource(d, upload=True)
            if failure == "missing-resource":
                requests = [bs.WriteRequest(data=expected, finish_write=True)]
            elif failure == "initial-offset":
                requests = [bs.WriteRequest(resource_name=name, write_offset=1,
                                            data=expected, finish_write=True)]
            elif failure in ("offset-gap", "changed-resource"):
                requests = [bs.WriteRequest(resource_name=name, data=expected[:3]),
                            bs.WriteRequest(resource_name=_resource(d, upload=True)
                                            if failure == "changed-resource" else "",
                                            write_offset=4 if failure == "offset-gap" else 3,
                                            data=expected[3:], finish_write=True)]
            else:
                requests = [bs.WriteRequest(resource_name=name,
                                            data=b"incorrect" if failure == "checksum" else expected,
                                            finish_write=True)]
            _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.bs.Write(iter(requests), timeout=ctx.timeout))
            assert list(c.missing([d]).missing_blob_digests) == [d], failure
            _error(grpc.StatusCode.NOT_FOUND, lambda: c.read(d))

    def compression():
        import zstandard as zstd
        compressor = zstd.ZstdCompressor()
        decompressor = zstd.ZstdDecompressor()
        data = _fresh("zstd-batch") + bytes(range(256)) * 128
        d = digest(data)
        encoded = compressor.compress(data)
        rsp = c.cas.BatchUpdateBlobs(re.BatchUpdateBlobsRequest(requests=[
            re.BatchUpdateBlobsRequest.Request(digest=d, data=encoded,
                                               compressor=re.Compressor.ZSTD)]), timeout=ctx.timeout)
        assert _responses(rsp, [d])[_key(d)].status.code == OK, rsp
        assert c.read(d) == data
        row = c.batch_read([d], compressors=[re.Compressor.ZSTD])[_key(d)]
        assert row.status.code == OK and row.compressor == re.Compressor.ZSTD, row
        assert decompressor.decompress(row.data, max_output_size=len(data)) == data
        # The server's streaming encoder may omit content size in the frame.
        assert decompressor.decompress(c.read(d, compressed=True), max_output_size=len(data)) == data
        assert decompressor.decompress(c.read(d, compressed=True, offset=71, limit=501),
                                        max_output_size=501) == data[71:572]
        # Incompressible bytes ensure the compressed upload itself spans several
        # requests, exercising offsets in compressed (not uncompressed) bytes.
        stream_data = hashlib.shake_256(_fresh("zstd-stream")).digest(65536)
        stream_d = digest(stream_data)
        stream_encoded = compressor.compress(stream_data)
        assert len(stream_encoded) > 2 * 16381
        c.write(stream_encoded, compressed=True, expected_digest=stream_d)
        assert c.read(stream_d) == stream_data
        assert not c.missing([stream_d]).missing_blob_digests
        # Above the direct-write threshold, existing compressed blobs use the
        # protocol's -1 short-circuit sentinel (no old Bazel metadata supplied).
        duplicate = c.bs.Write(iter([bs.WriteRequest(
            resource_name=_resource(stream_d, upload=True, compressed=True),
            data=stream_encoded, finish_write=True)]), timeout=ctx.timeout)
        assert duplicate.committed_size == -1, duplicate
        # A valid zstd frame with an incorrect *uncompressed* checksum must fail.
        bad = digest(_fresh("zstd-wrong-checksum"))
        wrong = compressor.compress(b"incorrect")
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.write(
            wrong, compressed=True, expected_digest=bad))
        assert list(c.missing([bad]).missing_blob_digests) == [bad]

    def action_roundtrip():
        d, result, data = c.action_fixture()
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(d))
        updated = c.update_action(d, result)
        assert updated.output_files == result.output_files
        assert updated.stdout_raw == result.stdout_raw and updated.stderr_raw == result.stderr_raw
        assert updated.execution_metadata.worker, updated
        assert c.get_action(d) == updated
        inline = c.get_action(d, inline_output_files=["out.bin"])
        assert len(inline.output_files) == 1 and inline.output_files[0].contents == data, inline
        assert inline.output_files[0].digest == digest(data)
        # Inlining is response-only and must not mutate the stored AC object.
        assert c.get_action(d) == updated
        result.exit_code = 7
        result.stdout_raw = b"replacement\n"
        replacement = c.update_action(d, result)
        assert replacement.exit_code == 7
        assert c.get_action(d) == replacement

    def action_validation():
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.ac.GetActionResult(
            re.GetActionResultRequest(), timeout=ctx.timeout))
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.ac.UpdateActionResult(
            re.UpdateActionResultRequest(action_result=re.ActionResult()), timeout=ctx.timeout))
        d, result, _ = c.action_fixture()
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.ac.UpdateActionResult(
            re.UpdateActionResultRequest(action_digest=d), timeout=ctx.timeout))
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: c.get_action(
            re.Digest(hash="bad", size_bytes=1)))
        missing_output = digest(_fresh("missing-output"))
        result.output_files[0].digest.CopyFrom(missing_output)
        # v2.303.0 validates referenced CAS blobs on GET, not UPDATE.
        c.update_action(d, result)
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(d))

    def action_output_tree():
        d, result, data = c.action_fixture()
        tree = re.Tree(root=re.Directory(files=[re.FileNode(name="nested.bin", digest=digest(data))]))
        td, = c.put(tree.SerializeToString())
        del result.output_files[:]
        result.output_directories.add(path="output-dir", tree_digest=td)
        updated = c.update_action(d, result)
        assert c.get_action(d) == updated
        tree.root.files[0].digest.CopyFrom(digest(_fresh("missing-tree-file")))
        bad_td, = c.put(tree.SerializeToString())
        result.output_directories[0].tree_digest.CopyFrom(bad_td)
        c.update_action(d, result)
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(d))
        result.output_directories[0].tree_digest.CopyFrom(digest(_fresh("missing-tree")))
        c.update_action(d, result)
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(d))

    def get_tree():
        fd, = c.put(_fresh("tree-file"))
        leaf = re.Directory(files=[re.FileNode(name="file.bin", digest=fd)],
                            symlinks=[re.SymlinkNode(name="link", target="file.bin")])
        ld, = c.put(leaf.SerializeToString())
        middle = re.Directory(directories=[re.DirectoryNode(name="leaf", digest=ld)])
        md, = c.put(middle.SerializeToString())
        root = re.Directory(directories=[re.DirectoryNode(name="middle", digest=md)],
                            files=[re.FileNode(name="root.bin", digest=fd)])
        rd, = c.put(root.SerializeToString())
        expected = Counter(x.SerializeToString(deterministic=True) for x in [root, middle, leaf])
        for _ in range(2):
            # Page size is advisory; never assume server traversal/response order.
            responses = list(c.cas.GetTree(re.GetTreeRequest(root_digest=rd, page_size=1), timeout=ctx.timeout))
            actual = Counter(x.SerializeToString(deterministic=True)
                             for rsp in responses for x in rsp.directories)
            assert actual == expected, responses
            assert all(not rsp.next_page_token for rsp in responses), responses
        assert list(c.cas.GetTree(re.GetTreeRequest(root_digest=digest(b"")), timeout=ctx.timeout)) == []

    def get_tree_errors():
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: list(c.cas.GetTree(
            re.GetTreeRequest(), timeout=ctx.timeout)))
        _error(grpc.StatusCode.INVALID_ARGUMENT, lambda: list(c.cas.GetTree(
            re.GetTreeRequest(root_digest=re.Digest(hash="bad", size_bytes=1)), timeout=ctx.timeout)))
        absent = digest(_fresh("missing-directory"))
        _error(grpc.StatusCode.FAILED_PRECONDITION, lambda: list(c.cas.GetTree(
            re.GetTreeRequest(root_digest=absent), timeout=ctx.timeout)))
        root = re.Directory(directories=[re.DirectoryNode(name="missing", digest=absent)])
        rd, = c.put(root.SerializeToString())
        _error(grpc.StatusCode.FAILED_PRECONDITION, lambda: list(c.cas.GetTree(
            re.GetTreeRequest(root_digest=rd), timeout=ctx.timeout)))

    def instances():
        first = "smoke/" + uuid.uuid4().hex + "/one"
        second = first.rsplit("/", 1)[0] + "/two"
        data = _fresh("instance")
        d, = c.put(data, instance=first)
        assert c.read(d, instance=first) == data
        data2 = _fresh("instance-stream")
        d2, _ = c.write(data2, instance=first)
        row = c.batch_read([d2], instance=first)[_key(d2)]
        assert row.status.code == OK and row.data == data2
        action_d, result, _ = c.action_fixture(instance=first)
        stored = c.update_action(action_d, result, instance=first)
        assert c.get_action(action_d, instance=first) == stored
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(action_d, instance=second))
        _error(grpc.StatusCode.NOT_FOUND, lambda: c.get_action(action_d))
        # CAS namespace sharing is backend-dependent (memory vs disk cache).
        # AC namespace isolation, in contrast, is an invariant of both.
        alternate = re.ActionResult(exit_code=11, stdout_raw=b"other instance")
        other = c.update_action(action_d, alternate, instance=second)
        assert c.get_action(action_d, instance=second) == other
        assert c.get_action(action_d, instance=first) == stored

    def persistence_fixture():
        instance = "smoke/restart/" + uuid.uuid4().hex
        d, result, data = c.action_fixture(instance=instance)
        stored = c.update_action(d, result, instance=instance)
        out = digest(data)
        ctx.state["cache_restart"] = {
            "instance_name": instance,
            "digest": {"hash": out.hash, "size_bytes": out.size_bytes},
            "data_hex": data.hex(),
            "action_digest": {"hash": d.hash, "size_bytes": d.size_bytes},
            "action_result_hex": stored.SerializeToString().hex(),
        }
        restart_probe(ctx)

    return [
        ("cache.capabilities", capabilities),
        ("cache.cas.batch_mixed_hit_miss", batch_mixed),
        ("cache.cas.empty_batches_and_blob", empty_batches),
        ("cache.cas.batch_partial_failure", batch_invalid_data),
        ("cache.cas.malformed_digests", malformed_digests),
        ("cache.cas.alternate_digest_functions", digest_functions),
        ("cache.bytestream.chunked_roundtrip_offsets", bytestream_roundtrip),
        ("cache.bytestream.invalid_reads", invalid_reads),
        ("cache.bytestream.invalid_writes", invalid_writes),
        ("cache.compression.zstd_batch_and_stream", compression),
        ("cache.action.roundtrip_inline_overwrite", action_roundtrip),
        ("cache.action.invalid_and_missing_output", action_validation),
        ("cache.action.output_tree_validation", action_output_tree),
        ("cache.get_tree.nested", get_tree),
        ("cache.get_tree.invalid_and_missing", get_tree_errors),
        ("cache.instances.cas_roundtrip_ac_isolation", instances),
        ("cache.persistence.seed", persistence_fixture),
    ]
