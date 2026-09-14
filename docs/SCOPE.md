# Scope, measurements, and v2.303.0 contracts

## Current status after the completeness review

Core now includes live/reconnected BES and late server-log inspection (62 cases).
Full adds real passing/failing Bazel tests, Targets UI, compressed large-blob
transfers, minimal downloads and an immediate-shutdown artifact regression.

**The pinned v2.303.0 no longer receives a green full report.** The early control
artifact survives in blob storage, but the one acknowledged immediately before
SIGTERM does not. Its missing download and the app's late error both fail.
See [reproduction and scope](KNOWN_ISSUES.md). There are no sleeps, error
allowlists, expected-pass conversions or CI failure suppression for this issue.

Updated measurements are recorded separately from the historical baseline below.

## Historical baseline timings (before those review fixes)

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

A subsequent review identified Playwright's detached Chromium process group as
a cleanup gap. The supervisor now registers as a Linux child subreaper and
cleans up/reaps the entire descendant tree across process groups. Two additional
regressions force timeout with **a detached child ignoring SIGTERM** and with
**Chromium actively running**; both require those PIDs to disappear. All six
harness regressions pass. The hardened full suite passed again in **9.908s**.
Raw-event downloads now validate decoded Started, Finished and BuildMetadata
payloads, not merely the presence of an invocation UUID in arbitrary JSON.

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
The documented empty cursor asks for the actual latest durable chunk. The tests
check both that lookup and explicit cursor `0000`, assert exact bytes and follow
`0001` to an empty terminal page. For the live chunk, `0000` returns `live=true`
and repeats `0000` as the next cursor while the transport/request iterator is
still open and has received no ACKs.

The disconnect case closes its HTTP/2 channel before Finished/EOF, observes
DISCONNECTED attempt 1 and the durable log prefix, then resends identical request
objects (same StreamId, timestamps, sequence numbers and payloads) from sequence
1. It requires PARTIAL attempt 2, then COMPLETE attempt 2 with exactly one copy
of each stored event/log byte. Both live/reconnected fixtures are rechecked after
restart. Polling observes explicit asynchronous predicates under a deadline;
failed assertions and unexpected RPC statuses are never broadly retried.

These log APIs use the app's in-memory key-value store in this standalone config;
Redis is not required. The UI streaming flag alone is not the evidence: the
live response, cursor and exact buffer are asserted through the RPC.

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

The real Bazel fixture has three shell actions, unique nonce inputs per run,
no remote executor and separate fresh local output roots. It registers only the
pre-extracted local Bazel runtime/platform repositories. Cold build: three local
actions, three AC NOT_FOUND RPCs and no remote hits. Second build: three successful
AC RPCs/remote hits, no local actions and exact output bytes. Bazel's binary gRPC
log must show actual compressed ByteStream transfers for the large output digest.
A third fresh-root build enables `--remote_download_minimal`; correct AC output
digests, absent output files and no large-output read are all mandatory.

A fourth `bazel test` invocation runs one passing and one failing real test. It
must exit with TESTS_FAILED, not an arbitrary nonzero error. The app paginates
those events into target groups, so the checks compare GetTarget's TestResult
and TestSummary payloads/IDs with the real emitted BEP as well as requiring the
expected per-label statuses. Chromium checks the actual Targets tab's two labels,
counts and pass/fail groups. Invocation status alone is not sufficient evidence.

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
