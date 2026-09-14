# BuildBuddy binary smoke tests

**61 black-box checks in about 10 seconds** against the
`buildbuddy-enterprise-linux-amd64` **v2.303.0** release, including real Bazel
cache reuse, Chromium, authentication, and process restart. The smaller core
profile runs 59 checks in about 5 seconds. These are measurements, not a promise
for every machine; both profiles have a **120-second hard wall-clock limit**.

This answers: *“Someone handed me an app binary. Is its main data path wired up
and working?”* It is not a unit-test suite, benchmark, conformance certification,
or proof that every enterprise deployment feature works.

## Run

Linux x86_64, Python 3.10+ with venv/pip, curl, sha256sum, `/bin/bash`, `/bin/cat`.
No Docker, Go build, system Java, Redis, executor, cloud credentials, or external
OAuth provider required.

```sh
# One-time setup: downloads are NOT inside the smoke runtime budget.
scripts/setup.sh --with-deps

# Default: protocols + browser + auth + actual Bazel cold/hit builds.
python3 run.py

# Fastest broad coverage: omit just the real Bazel builds.
python3 run.py --profile core

# Test a binary someone handed you (no BuildBuddy binary download needed).
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
python3 -m unittest discover -s tests -v  # harness deadline/failure regression tests
```

The output directory must not already exist. `--budget` accepts 5–120 seconds.
Missing dependencies are failures, not silently skipped coverage. Protocol
bindings and expected contracts are pinned to v2.303.0: a different binary may
expose intentional API changes that require reviewing/updating expectations.

## What “works” means here

| Surface | Assertions, not just successful connections |
|---|---|
| Process / storage | Fresh SQLite migration; real readiness/liveness; clean SIGTERM; restart on existing database, blob store and disk cache; exact CAS bytes, action results, invocation metadata and logs survive |
| Web application | HTML serves its actual JS bundle, not a SPA fallback; HTTP JSON RPC; malformed JSON rejected; Prometheus metrics; Chromium renders successful/failed builds and exact console markers; Details/Logs navigation; no uncaught JS exceptions |
| Downloads | CAS artifact download/view produces exact bytes and attachment/inline headers; successful/failed build-log downloads and raw-event JSON exports |
| BES | Valid synthetic successful and failed BEP graphs; ordered stream ACKs; complete invocation status; user/host/command/repo/commit/branch/duration/exit status; stored events; exact durable logs and terminal cursor |
| CAS | Mixed hits/misses, empty blob and empty batches, per-entry partial failures, invalid hashes/sizes/encoding, SHA-256 plus actual SHA-1/SHA-512 round trips, nested directory traversal, missing referenced directories |
| ByteStream | Multi-request writes and multi-response reads, exact bytes, offsets/limits/EOF, duplicate writes, malformed resource names, offset gaps and changed resources; rejected writes do not become readable |
| Compression | Zstd batch + streaming writes/reads, compressed/uncompressed interoperability, incompressible multi-chunk upload, ranged decompression, duplicate-write acknowledgement, checksum rejection |
| Action cache | Miss → update → exact read, overwrite, inline output files without mutating stored data, output-tree validation, missing referenced CAS data, instance isolation |
| Remote Asset | Controlled local HTTP origin → checksum-verified FetchBlob → exact CAS bytes; URI fallback, forwarded header, cached fetch without contacting origin, bad checksum/404 rejection, non-allowlisted loopback blocking |
| Authentication | Strict anonymous/invalid-key denial on CAS, ByteStream, AC, BES and user/group/key APIs; local OAuth cookie flow → CreateUser → CreateApiKey → authenticated CAS/AC; anonymous reads still denied for known existing entries |
| Real Bazel (full) | One real shell action executed/uploaded, then rebuilt using a **different fresh local output root**; exactly one remote cache hit and no local action; identical output bytes; both real invocations fully ingested |

There are hundreds of assertions grouped into 61 named cases. The count includes
lifecycle checks and explicit unsupported-method contracts, **not 61 independent
product features**. Supported capabilities are asserted rather than used to
silently skip tests. Known unsupported contracts are called out below.

## Isolation and timing

- A run starts its own binary with fresh local SQLite/blob/cache directories and
  dynamically selected **loopback-only** HTTP/gRPC/monitoring ports. No attaching
  to, modifying, or cleaning up an existing production installation.
- One anonymous fixture is restarted for persistence checks, then restarted with
  a local self-auth provider to test authenticated operation and denial paths.
- Never enable this self-auth fixture in production: it intentionally lets anyone
  reaching it sign in as a local test administrator. Its signing key and all data
  are throwaway. No host service credentials are needed.
- HTTP-origin fixtures and OAuth are local. Browser external requests are
  blocked. Bazel ignores user rc files, has zero external repositories and
  rejects repository downloads; its action needs no compiler. Telemetry is off.
- Startup, Python imports, binary SHA256, browser/JVM launches, RPCs, all three
  app starts, persistence, shutdown and cleanup are inside the budget. Download,
  package installation and protobuf generation are **setup**, outside it.
- RPCs normally have 5s deadlines; each Bazel build has a 30s ceiling. The outer
  supervisor terminates the entire worker/app/browser/Bazel process group before
  the budget expires. The budget reserves 3s for cleanup/reporting. Exit 124 means
  budget exceeded, exit 1 means failed assertions, exit 0 means all selected
  checks passed.
- Test data is retained in the ignored output directory for debugging. Delete
  only specific old result directories when you no longer need them.

Tests intentionally run serially: protocol cases are already sub-millisecond to
milliseconds; avoiding concurrency makes failures easier to diagnose. Closing
finished client channels before SIGTERM avoids wasting the HTTP/2 drain window.

## Reports and CI

Each run writes `report.json`, JUnit `junit.xml`, per-generation app logs, browser
screenshots/errors, fixture IDs, a generated config and local storage. Full runs
also retain Bazel commands, logs, BEP JSON, profiles, output bytes, invocation
JSON and hit summaries. Binary SHA256 and platform details are in the report.

The GitHub Actions workflow runs setup separately, checks the supervisor's
failure/deadline behavior, runs the full profile, and uploads selected reports,
logs and screenshots. The CI job has a longer provisioning timeout; **only the
smoke command is promised a ≤120-second budget**.

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
