"""Negative evidence checks; no Bazel, browser or running app required."""
import hashlib
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class BazelContractTest(unittest.TestCase):
    def test_contracts_in_pinned_environment(self):
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'generated'}:{ROOT}")
        result = subprocess.run(
            [str(ROOT / '.venv/bin/python'), str(Path(__file__).resolve()), '--contracts'],
            env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def contracts():
    from smoke import bazel
    from proto import build_event_stream_pb2 as bep

    class BazelEvidenceTest(unittest.TestCase):
        def test_large_fixture_is_deterministic_and_over_one_mib(self):
            self.assertEqual(bazel.EXPECTED, (ROOT / "fixtures/bazel/payload.txt").read_bytes())
            self.assertGreater(len(bazel.LARGE), 1024 * 1024)
            self.assertEqual(bazel.LARGE_HASH, hashlib.sha256(bazel.LARGE).hexdigest())

        def test_decode_real_wire_fields_and_multiple_entries(self):
            # Independent, hand-encoded projection: LogEntry.details(4), Read(5),
            # ReadRequest(1), resource_name(1); num_reads(2)=2, bytes_read(3)=7.
            raw = b'\x22\x0b\x2a\x09\x0a\x03\x0a\x01x\x10\x02\x18\x07'
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "grpc.log"
                path.write_bytes(bytes([len(raw)]) + raw + bytes([len(raw)]) + raw)
                entries = bazel._read_grpc_log(path)
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries[0].details.read.request.resource_name, "x")
                self.assertEqual(entries[0].details.read.bytes_read, 7)
                self.assertEqual(entries[0].details.read.num_reads, 2)
                self.assertTrue(path.with_suffix(".json").is_file())

        def test_reject_empty_truncated_and_invalid_framing(self):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "grpc.log"
                for data in (b"", b"\x80", b"\x80" * 10, b"\x02\x08", b"\x00"):
                    with self.subTest(data=data):
                        path.write_bytes(data)
                        with self.assertRaises(AssertionError):
                            bazel._read_grpc_log(path)

        def transfer(self, direction, compressed=True, size=2 * 1024 * 1024, status=0, messages=2):
            entry = bazel._log_entry_class()()
            entry.status.code = status
            path = f"{'compressed-blobs/zstd' if compressed else 'blobs'}/{bazel.LARGE_HASH}/{len(bazel.LARGE)}"
            if direction == "read":
                entry.details.read.request.resource_name = path
                entry.details.read.bytes_read = size
                entry.details.read.num_reads = messages
            else:
                entry.details.write.resource_names.append("uploads/uuid/" + path)
                entry.details.write.bytes_sent = size
                entry.details.write.num_writes = messages
            return entry

        def test_compression_requires_matching_successful_wire_transfer(self):
            for direction in ("read", "write"):
                self.assertEqual(bazel._compressed_transfer([self.transfer(direction)], direction), [{"bytes": 2 * 1024 * 1024, "messages": 2}])
                for entries in ([], [self.transfer(direction, compressed=False)],
                                [self.transfer(direction, size=0)],
                                [self.transfer(direction, size=623)],
                                [self.transfer(direction, messages=1)],
                                [self.transfer(direction, size=len(bazel.LARGE))],
                                [self.transfer(direction, status=14)]):
                    with self.subTest(direction=direction, entries=entries):
                        with self.assertRaises(AssertionError):
                            bazel._compressed_transfer(entries, direction)

        def events(self):
            events = []
            for label, status in bazel.TEST_TARGETS.items():
                result = bep.BuildEvent()
                result.id.test_result.label = label
                result.test_result.status = status
                result.test_result.test_action_output.add(name="test.log", uri="bytestream://local/blobs/hash/5")
                summary = bep.BuildEvent()
                summary.id.test_summary.label = label
                summary.test_summary.overall_status = status
                summary.test_summary.total_run_count = 1
                events += [result, summary]
            return events

        def test_both_real_test_results_and_summaries_required(self):
            bazel._assert_test_events(self.events())
            for index in range(4):
                events = self.events()
                events.pop(index)
                with self.subTest(missing=index), self.assertRaises(AssertionError):
                    bazel._assert_test_events(events)

        def test_reject_wrong_test_status_duplicate_and_cached_result(self):
            for mutation in ("result", "summary", "duplicate", "cached", "log", "run_count"):
                events = self.events()
                if mutation == "result":
                    events[2].test_result.status = bep.PASSED
                elif mutation == "summary":
                    events[3].test_summary.overall_status = bep.PASSED
                elif mutation == "duplicate":
                    events.append(events[0])
                elif mutation == "cached":
                    events[0].test_result.cached_locally = True
                elif mutation == "log":
                    events[0].test_result.ClearField("test_action_output")
                else:
                    events[1].test_summary.total_run_count = 0
                with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                    bazel._assert_test_events(events)

        def minimal_run(self, temp, materialized=False, downloaded=False, wrong_digest=False):
            artifacts = Path(temp)
            output = artifacts / "bin"
            output.mkdir()
            if materialized:
                (output / "large.txt").write_bytes(bazel.LARGE)
            entries = []
            expected = [bazel.EXPECTED, bazel.LARGE, f"{bazel.LARGE_HASH}  -\n".encode()]
            for index, data in enumerate(expected):
                entry = bazel._log_entry_class()()
                file = entry.details.get_action_result.response.output_files.add(path=str(index))
                file.digest.hash = hashlib.sha256(data).hexdigest()
                file.digest.size_bytes = len(data) + (1 if wrong_digest else 0)
                entries.append(entry)
            if downloaded:
                entries.append(self.transfer("read"))
            ctx = SimpleNamespace(state={"bazel": {"builds": [{}, {}], "base": temp}})
            inv = SimpleNamespace(HasField=lambda name: False)
            with patch.object(bazel, "_execute", return_value=(artifacts, output, "3 remote cache hits", inv, {})), \
                 patch.object(bazel, "_read_grpc_log", return_value=entries):
                bazel._run_build(ctx, 3)
            return ctx

        def test_minimal_requires_metadata_and_absent_files_and_no_read(self):
            with tempfile.TemporaryDirectory() as temp:
                ctx = self.minimal_run(temp)
                self.assertEqual(ctx.state["bazel"]["builds"][-1]["minimal_read_resources"], [])
            for kwargs in ({"materialized": True}, {"downloaded": True}, {"wrong_digest": True}):
                with tempfile.TemporaryDirectory() as temp:
                    with self.subTest(kwargs=kwargs), self.assertRaises(AssertionError):
                        self.minimal_run(temp, **kwargs)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BazelEvidenceTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--contracts" in sys.argv:
        raise SystemExit(contracts())
    unittest.main()
