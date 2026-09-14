"""BES harness regressions; the stdlib test runner delegates to the pinned venv."""
from pathlib import Path
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BESContractTest(unittest.TestCase):
    def test_contracts_in_pinned_environment(self):
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'generated'}:{ROOT}")
        result = subprocess.run(
            [str(ROOT / '.venv/bin/python'), str(Path(__file__).resolve()), '--contracts'],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def contracts():
    from concurrent import futures
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    import grpc
    from smoke import bes

    class Checks(unittest.TestCase):
        def setUp(self):
            self.ctx = SimpleNamespace(timeout=.05)
            self.fixture = bes._fixture('11111111-1111-4111-8111-111111111111', True)
            self.requests = bes._requests(self.fixture)
            self.acks = [bes.publish.PublishBuildToolEventStreamResponse(
                stream_id=r.ordered_build_event.stream_id,
                sequence_number=r.ordered_build_event.sequence_number,
            ) for r in self.requests]

        def test_ack_loss_duplication_reorder_and_wrong_identity_fail(self):
            bes._verify_acks(self.acks, self.requests)
            for acks in (self.acks[:-1], self.acks + self.acks[-1:], self.acks[::-1]):
                with self.subTest(acks=acks), self.assertRaises(AssertionError):
                    bes._verify_acks(acks, self.requests)
            self.acks[0].stream_id.invocation_id = 'wrong-invocation'
            with self.assertRaisesRegex(AssertionError, 'wrong stream ID'):
                bes._verify_acks(self.acks, self.requests)

        def test_exact_live_bytes_and_cursor_not_just_a_marker(self):
            response = bes.eventlog_pb2.GetEventLogChunkResponse(
                live=True, buffer=b'marker\n', next_chunk_id='0000')
            with patch.object(bes, '_log_chunk', return_value=response):
                bes._verify_log(self.ctx, 'id', b'marker\n', live=True)
                for field, value in [('buffer', b'marker\nmarker\n'), ('live', False),
                                     ('previous_chunk_id', 'ffff'), ('next_chunk_id', '0001')]:
                    old = getattr(response, field)
                    setattr(response, field, value)
                    with self.subTest(field=field), self.assertRaises(AssertionError):
                        bes._verify_log(self.ctx, 'id', b'marker\n', live=True)
                    setattr(response, field, old)

        def test_durable_log_requires_terminal_cursor(self):
            chunk = bes.eventlog_pb2.GetEventLogChunkResponse(buffer=b'x\n', next_chunk_id='0001')
            end = bes.eventlog_pb2.GetEventLogChunkResponse(previous_chunk_id='0000')
            with patch.object(bes, '_log_chunk', side_effect=[chunk, chunk, end]):
                bes._verify_log(self.ctx, 'id', b'x\n', live=False)
            end.buffer = b'duplicate\n'
            with patch.object(bes, '_log_chunk', side_effect=[chunk, chunk, end]):
                with self.assertRaisesRegex(AssertionError, 'did not terminate'):
                    bes._verify_log(self.ctx, 'id', b'x\n', live=False)

        def test_poll_does_not_retry_failed_rpc_or_assertion(self):
            for error in (RuntimeError('rpc failure'), AssertionError('broken invariant')):
                with patch.object(bes, '_log_chunk', side_effect=error) as read:
                    with self.assertRaises(type(error)):
                        bes._poll(self.ctx, 'exact value', lambda: read(), lambda v: v == 1)
                    self.assertEqual(read.call_count, 1)
            with self.assertRaisesRegex(AssertionError, 'last observation: 2'):
                bes._poll(self.ctx, 'exact value', lambda: 2, lambda v: v == 1)

        def test_only_initial_not_found_is_pollable(self):
            class Error(grpc.RpcError):
                def __init__(self, code):
                    self.status = code
                def code(self):
                    return self.status
            for code in [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED,
                         grpc.StatusCode.INTERNAL, grpc.StatusCode.DEADLINE_EXCEEDED]:
                with patch.object(bes, '_rpc', side_effect=Error(code)):
                    if code == grpc.StatusCode.NOT_FOUND:
                        self.assertIsNone(bes._get_invocation(self.ctx, 'id', allow_missing=True))
                    else:
                        with self.assertRaises(grpc.RpcError):
                            bes._get_invocation(self.ctx, 'id', allow_missing=True)
                    with self.assertRaises(grpc.RpcError):
                        bes._get_invocation(self.ctx, 'id')

        def test_partial_status_requires_exact_attempt_and_metadata(self):
            inv = bes.invocation_pb2.Invocation(
                invocation_id=self.fixture['invocation_id'], attempt=2,
                invocation_status=bes.invocation_status_pb2.PARTIAL_INVOCATION_STATUS,
                command='build', user='smoke-user', host='smoke-host',
                repo_url=self.fixture['metadata']['REPO_URL'],
                commit_sha=self.fixture['metadata']['COMMIT_SHA'],
                branch_name=self.fixture['metadata']['BRANCH_NAME'], role='CI',
                has_chunked_event_logs=True, created_at_usec=1, updated_at_usec=2,
            )
            with patch.object(bes, '_get_invocation', return_value=inv):
                bes._wait_status(self.ctx, self.fixture, inv.invocation_status, 2)
                for field, value in [('attempt', 1), ('command', 'test'),
                                     ('invocation_status', bes.invocation_status_pb2.COMPLETE_INVOCATION_STATUS),
                                     ('has_chunked_event_logs', False)]:
                    old = getattr(inv, field)
                    setattr(inv, field, value)
                    with self.subTest(field=field), self.assertRaisesRegex(AssertionError, 'timed out'):
                        bes._wait_status(self.ctx, self.fixture,
                                         bes.invocation_status_pb2.PARTIAL_INVOCATION_STATUS, 2)
                    setattr(inv, field, old)

        def test_real_queue_stream_waits_for_eof_and_cleans_up_disconnect(self):
            received = threading.Event()
            class Service(bes.publish_build_event_pb2_grpc.PublishBuildEventServicer):
                def PublishBuildToolEventStream(self, requests, context):
                    pending = []
                    for request in requests:
                        pending.append(request)
                        received.set()
                    for request in pending:
                        yield bes.publish.PublishBuildToolEventStreamResponse(
                            stream_id=request.ordered_build_event.stream_id,
                            sequence_number=request.ordered_build_event.sequence_number,
                        )
            pool = futures.ThreadPoolExecutor(max_workers=2)
            server = grpc.server(pool)
            bes.publish_build_event_pb2_grpc.add_PublishBuildEventServicer_to_server(Service(), server)
            port = server.add_insecure_port('127.0.0.1:0')
            server.start()
            ctx = SimpleNamespace(timeout=2, grpc_target=f'127.0.0.1:{port}')
            try:
                with bes._OpenStream(ctx) as stream:
                    stream.send(self.requests[:5])
                    self.assertTrue(received.wait(2))
                    stream.assert_open()
                    stream.finish(self.requests[5:], self.requests)
                self.assertTrue(stream.iterator_done.is_set())
                self.assertFalse(stream.reader.is_alive())
                received.clear()
                with bes._OpenStream(ctx) as stream:
                    stream.send(self.requests[:5])
                    self.assertTrue(received.wait(2))
                    stream.disconnect()
                self.assertTrue(stream.iterator_done.is_set())
                self.assertFalse(stream.reader.is_alive())
            finally:
                server.stop(0).wait(2)
                pool.shutdown(wait=True)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    return int(not result.wasSuccessful())


if __name__ == '__main__':
    if '--contracts' in sys.argv:
        raise SystemExit(contracts())
    unittest.main()
