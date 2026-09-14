# BuildBuddy enterprise binary smoke tests

Fast black-box checks against a supplied **enterprise Linux x86_64 binary**:
protocols, real Chromium, authentication, restart persistence, real Bazel tests,
compressed remote caching and minimal downloads. Both profiles have a
**120-second hard wall-clock limit**; provisioning is separate.

**Current v2.303.0 status:** core passes; the full profile now correctly **fails**
on a shutdown artifact-persistence defect. An artifact acknowledged just before
SIGTERM cannot be retrieved from blob storage after restart with an empty CAS,
and the app logs a persistence error. This is not allowlisted or converted to an
expected pass. See [the reproducible issue](docs/KNOWN_ISSUES.md).
Older green results predate that regression and do **not** establish clean shutdown.

| Latest local validation | Result | Wall time |
|---|---|---:|
| Core | 62 passed | 4.649s |
| Full | 72 passed; 2 shutdown-related failures | 17.678s |
| Harness regression tests | 17 top-level tests passed | 12.339s (separate from smoke) |

This answers: *“Someone handed me an enterprise app binary. Is its main data path
wired up and working?”* It is not a unit-test suite, benchmark, conformance
certification, or proof that every enterprise deployment feature works. The OSS
`buildbuddy-linux-amd64` binary is not supported by this launch configuration.

## Run

Linux x86_64, Python 3.10+ with venv/pip, curl, sha256sum, `/bin/bash`, `/bin/cat`.
No Docker, Go build, system Java, Redis, executor, cloud credentials, or external
OAuth provider required.

```sh
# One-time setup: downloads are NOT inside the smoke runtime budget.
scripts/setup.sh --with-deps

# Default: protocols, browser, auth, real Bazel tests/cache and strict shutdown.
python3 run.py

# Faster: omit real Bazel and the shutdown-artifact probe (not its log scan).
python3 run.py --profile core

# Test a supplied ENTERPRISE binary (no BuildBuddy binary download needed).
scripts/setup.sh --binary /absolute/path/buildbuddy-enterprise-linux-amd64 --with-deps
python3 run.py --binary /absolute/path/buildbuddy-enterprise-linux-amd64
```

`--with-deps` installs browser OS libraries and may require sudo. Normal runs
are unprivileged. For core-only provisioning, use `scripts/setup.sh --skip-bazel`.
For existing browser libraries, omit `--with-deps`. Chromium remains required
by both runtime profiles. All binary/source archive digests and Python package
versions are pinned. Bazel's embedded JDK is unpacked during setup.

```sh
python3 run.py --budget 30 --output results/my-run
python3 -m unittest discover -s tests -v  # includes an active-Chromium timeout test
```

The output directory must not already exist. `--budget` accepts 5–120 seconds.
Missing dependencies are failures, not silently skipped coverage. Protocol
bindings and expected contracts are pinned to v2.303.0: a different binary may
expose intentional API changes that require reviewing/updating expectations.

## What “works” means here

| Surface | Assertions, not just successful connections |
|---|---|
| Process / storage | Fresh SQLite migration; readiness/liveness; bounded SIGTERM with exit 0; restart on existing storage; exact CAS bytes, action results, invocation metadata and logs survive. Exit 0 alone is NOT called clean shutdown |
| Server logs | Supervisor scans every app generation AFTER process cleanup, including late errors; unexpected ERR/error/fatal/panic/goroutine dumps fail the run, even if the worker exited 0 |
| Shutdown artifacts (full) | Early control plus artifact acknowledged immediately before SIGTERM; restart with original database/blobstore but a genuinely empty CAS; assert both downloads return exact bytes through blobstore fallback. **Fails on v2.303.0** |
| Web application | HTML serves its actual JS bundle, not a SPA fallback; HTTP JSON RPC; malformed JSON rejected; Prometheus metrics; Chromium renders successful/failed builds and exact console markers; Details/Logs navigation; no uncaught JS exceptions |
| Downloads | CAS artifact download/view produces exact bytes and attachment/inline headers; successful/failed build-log downloads and raw-event JSON exports |
| BES | Successful/failed closed BEP graphs, exact ACKs, metadata, durable logs and EOF; PARTIAL invocation and exact live log bytes while stream remains open with no ACKs; transport disconnect → DISCONNECTED attempt 1 → resend same stream/sequence/bytes → COMPLETE attempt 2 without duplicated events/logs; all four fixtures survive restart |
| CAS | Mixed hits/misses, empty blob and empty batches, per-entry partial failures, invalid hashes/sizes/encoding, SHA-256 plus actual SHA-1/SHA-512 round trips, nested directory traversal, missing referenced directories |
| ByteStream | Multi-request writes and multi-response reads, exact bytes, offsets/limits/EOF, duplicate writes, malformed resource names, offset gaps and changed resources; rejected writes do not become readable |
| Compression | Zstd batch + streaming writes/reads, compressed/uncompressed interoperability, incompressible multi-chunk upload, ranged decompression, duplicate-write acknowledgement, checksum rejection |
| Action cache | Miss → update → exact read, overwrite, inline output files without mutating stored data, output-tree validation, missing referenced CAS data, instance isolation |
| Remote Asset | Controlled local HTTP origin → checksum-verified FetchBlob → exact CAS bytes; URI fallback, forwarded header, cached fetch without contacting origin, bad checksum/404 rejection, non-allowlisted loopback blocking |
| Authentication | Strict anonymous/invalid-key denial on CAS, ByteStream, AC, BES and user/group/key APIs; local OAuth cookie flow → CreateUser → CreateApiKey → authenticated CAS/AC; anonymous reads still denied for known existing entries |
| Real Bazel cache (full) | Three cold local actions; two fresh-root rebuilds require three remote hits apiece; exact **4 MiB** output and SHA256 receipt; inspect Bazel's binary gRPC log for successful zstd uploads/downloads with **>1 MiB compressed bytes and multiple requests/responses**, not merely flags; minimal build requires correct AC digests, absent local outputs, no inline file contents and no large-output reads |
| Real Bazel tests (full) | One passing and one failing test; require expected Bazel TESTS_FAILED exit rather than treating any failure as success; validate per-target TestResult/TestSummary events and GetTarget responses; Chromium Targets tab must show the correct passing/failing labels and counts |

Named cases group many assertions. Counts include lifecycle checks, inexpensive
auth denials and explicit unsupported-method contracts; they are **not independent
product features**. Supported capabilities are asserted rather than used to
silently skip tests. Known unsupported contracts are called out below.

## Isolation and timing

- A run starts its own binary with fresh local SQLite/blob/cache directories and
  dynamically selected **loopback-only** HTTP/gRPC/monitoring ports. No attaching
  to, modifying, or cleaning up an existing production installation.
- One anonymous fixture is restarted for persistence checks, then restarted with
  a local self-auth provider to test authenticated operation and denial paths.
  Full also adds an anonymous restart with a fresh cache and existing blobstore;
  the original cache is preserved, not deleted, for diagnosis.
- Never enable this self-auth fixture in production: it intentionally lets anyone
  reaching it sign in as a local test administrator. Its signing key and all data
  are throwaway. No host service credentials are needed.
- HTTP-origin fixtures and OAuth are local. Browser external requests are
  blocked. Bazel ignores user rc files and rejects repository downloads. Tests
  use only Bazel's pre-extracted local runtime/platform repositories; no external
  rule downloads, language toolchain or compiler is needed. Telemetry is off.
- Startup, Python imports, binary SHA256, browser/JVM launches, RPCs, every
  app start, persistence, shutdown and cleanup are inside the budget. Download,
  package installation and protobuf generation are **setup**, outside it.
- RPCs normally have 5s deadlines; each Bazel build has a 30s ceiling. The outer
  supervisor uses Linux child-subreaper adoption and bounded TERM/KILL/reaping
  of the whole descendant tree, including detached Chromium processes. The budget
  reserves 3s for cleanup/reporting. Exit 124 means
  budget exceeded, exit 1 means failed assertions, exit 0 means all selected
  checks passed.
- Test data is retained in the ignored output directory for debugging. Delete
  only specific old result directories when you no longer need them.

Tests intentionally run serially: protocol cases are already sub-millisecond to
milliseconds; avoiding concurrency makes failures easier to diagnose. Closing
finished client channels before SIGTERM avoids wasting the HTTP/2 drain window.

## Reports and CI

Each run writes `report.json`, JUnit `junit.xml`, per-generation app logs, browser
screenshots/errors, `log-hygiene.json`, fixture IDs, a generated config and local
storage. Full runs also retain Bazel commands, logs, BEP JSON, binary/decoded gRPC
logs, profiles, output bytes, invocation/target JSON and cache-transfer summaries.
Binary SHA256 and platform details are in the report. Log inspection runs in a
separately timed subprocess (at most 1s and within the remaining budget), with
bounded file/line/total sizes and finding samples. Incomplete inspection **fails**;
original logs are retained. Worker reports over 4 MiB also fail closed.

The GitHub Actions workflow runs setup separately, checks the supervisor's
failure/deadline behavior, runs the full profile, and uploads selected reports,
logs and screenshots. The CI job has a longer provisioning timeout; **only the
smoke command is promised a ≤120-second budget**. With the pinned v2.303.0,
strict full-profile CI is intentionally red until the product defect is fixed;
the workflow does not suppress the failure.

See [measured runs](docs/measurements.json) and [scope and version notes](docs/SCOPE.md).

## What this does not prove

Remote **execution** (scheduling, execution, cancellation) needs an executor and
Redis and is explicitly disabled; real Bazel actions execute **locally** and
use the app for remote caching and BES. This is not an RBE test.

Also out of scope: distributed/replicated caches, Redis/Pebble/cloud backends,
cache eviction and TTL, upgrade migrations, crash/power-loss durability,
load/soak/concurrency, TLS/mTLS, external SSO, multiple tenants/role boundaries,
key revocation/expiry, GitHub/workflows/webhooks, OLAP analytics, billing, or full
UI/accessibility/security coverage. Add separately budgeted deployment profiles
for those instead of claiming that a green single-process smoke test covers them.
