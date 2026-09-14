# Scope, measurements, and v2.303.0 contracts

## Measured results

On September 14, 2026, with the exact release SHA256 recorded in
[measurements.json](measurements.json):

| Environment / profile | Runs | Cases per run | Min / median / max |
|---|---:|---:|---:|
| exe.dev VM, core | 3 | 59 | 4.766 / 4.904 / 5.093 seconds |
| exe.dev VM, full | 5 | 61 | 9.917 / 10.014 / 10.639 seconds |
| GitHub Actions Ubuntu 24.04, full | 1 | 61 | 12.185 seconds |

All 482 cases in the eight serial local repetitions passed. The independent
GitHub Actions run also passed all 61 cases and the three supervisor regression
tests present at that commit. The VM exposed 8 CPUs, with the app limited to
GOMAXPROCS=4; the CI runner exposed 2 CPUs. App storage and Bazel output roots
were fresh for every run; the OS page cache, dependencies and Bazel installation
were warm. These are **not cold-machine boot** measurements.

The complete GitHub Actions job includes downloading/installing dependencies;
its provisioning time is not represented by the 12.185-second smoke time.
CI run: https://github.com/altdansalt/buildbuddy-smoke/actions/runs/34887414605

A deliberately shortened **7-second** full-profile budget interrupted the first
Bazel build at **4.008 seconds** (the supervisor reserves 3 seconds for cleanup).
It returned exit 124, produced JSON/JUnit failure reports, and inspection found
no escaped live app/browser/Bazel processes for that run. A crashing fake binary
and a hanging fake binary are also tested by `tests/test_supervisor.py`; those
must produce failure, never a false green.

## Compatibility decisions worth knowing

### HTTP readiness is not every internal client being ready

On a fresh fast start, `/readyz` was already OK while Remote Asset's local gRPC
cache client was still reconnecting after its pre-listen connection attempt.
The FetchBlob case permits **only** that precise `Unavailable` +
`connection refused` condition for at most 3 seconds. It does not retry checksum,
status, byte-comparison, auth, or other assertion failures. Attempts are recorded
as `asset_startup_attempts` in `fixtures.json`; the observed warm-up was ~0.8s.

This tests service usability after bounded startup rather than treating an HTTP
ready flag as a proof of functional integration. If readiness semantics themselves
are a deployment contract, add a strict-ready profile and remove this allowance.

### BES logs and completion

Synthetic BES requests form a closed BEP event graph, include Started options,
and finish the stream. The server sends ACKs after request EOF/finalization, so
a client that waits for an ACK before sending each next event would deadlock.
Every sequence number and stream ID must be acknowledged.

The invocation row can retain the reserved `ffff` last-chunk sentinel for a small
completed log. Passing it as an explicit log cursor returns RESOURCE_EXHAUSTED.
The documented empty cursor asks for the actual latest chunk; tests use it,
assert exact durable bytes, and follow the returned cursor to assert an empty
terminal page. No errors are suppressed or retried to make BES pass.

### Auth must actually be enabled before testing denial

`auth.enable_anonymous_usage=false` **alone** does not disable anonymous behavior
when no OAuth providers/self-auth are configured in this version. The strict
auth phase enables the app's own local self-auth provider and an ephemeral JWT
key as well. This fixture grants local administrator access without credentials;
it is **only safe on a disposable loopback-only test instance**.

The positive path uses actual HTTP OAuth redirects/cookies and public onboarding
RPCs to create the test user/key. It does not insert users or API keys into SQLite.
Negative gRPC cases accept only UNAUTHENTICATED/PERMISSION_DENIED, not generic
failures. The HTTP protolet represents service auth errors as HTTP 500, so the
test also checks the embedded gRPC authorization code, not just HTTP failure.

### Explicitly unsupported does not mean exercised successfully

The pinned server returns UNIMPLEMENTED for:

- Remote Asset FetchDirectory, PushBlob, and PushDirectory.
- ByteStream QueryWriteStatus (no resumable-upload promise).

Those responses are asserted as compatibility contracts and **are not claimed
as working features**. A future release implementing one should gain positive
coverage rather than preserving an obsolete expectation.

Action-cache output existence is validated on GetActionResult, not necessarily
UpdateActionResult. Tests exercise that behavior and verify missing output files
and tree references cause cache misses. CAS sharing across instance names is
backend-dependent; action-cache instance isolation is asserted, not mistaken
for a tenant-isolation test.

### Cache hits must be real, but optional statistics are not proof

The real Bazel fixture has one declared shell action, a unique nonce input per
run, no remote executor, no repositories, and separate fresh local output roots.
Cold build: one local action and no remote hits. Second build: exactly one remote
hit and no local action. Both must produce the exact output and complete BES
invocations. Turning off remote-cache acceptance makes the second check fail.

This standalone configuration returns an empty cache-stat message. The harness
records server statistics as unavailable, rather than passing an assertion on
zero-valued absent counters. If counters are populated, they must agree with the
cold/hit outcome. The client-side remote-hit assertion is **always mandatory**.

## Sensible next profiles (not implemented or timed here)

1. **Configured deployment smoke:** use real selected storage/cache backends,
   TLS endpoints, and identity provider test accounts; verify two tenants cannot
   read each other's invocations/cache and read-only keys cannot mutate data.
2. **Remote execution:** start Redis plus one executor; execute one tiny action,
   verify output/stdout/stderr/exit status, repeat for AC hit, then cancel a blocked
   action. An app binary alone is not an executor.
3. **Upgrade smoke:** seed data with the previous supported release, start the
   candidate on the same fixture, verify migrations and reads, exercise rollback
   only where supported. Empty-database migration is not an upgrade test.
4. **Reliability/performance suites:** concurrent writers, interrupted uploads,
   process crashes, eviction, TTL, replication/failover, large logs/trees, queue
   pressure and slow storage. Keep long-running tests outside the fast gate.

Prefer a new explicit profile over silently skipping a service based on a
capability response or an unavailable credential. A report should say precisely
what configuration and data paths were tested.
