# Offline real-Bazel fixtures

`smoke/bazel.py` copies this workspace into the run artifacts. A UUID in
`nonce.txt` changes action keys once per run, but remains identical across the
three cache builds. It also writes `large-input.bin` locally: 2,048 independent
SHAKE-256-derived 1 KiB blocks interleaved with 1 KiB zero blocks (4 MiB total).
This is reproducible, partly compressible, and still larger than 1 MiB on the
compressed wire. The harness also substitutes that UUID in the test scripts
and the pre-extracted Bazel installation path in `resolved.bzl`.

## No downloaded rules or toolchains

Bazel 8.4.2 uses `--enable_bzlmod=false`, empty external autoloads,
`--repository_disable_download`, and
`--experimental_resolved_file_instead_of_workspace=resolved.bzl`. Replacing the
normal WORKSPACE initialization prevents eager loading of rules_cc/rules_java.
The only registered repositories point to **already extracted files in the
pinned Bazel installation**: `embedded_tools` (Bazel's native test launcher/XML
scripts) and `platforms` (constraints required by the launcher's Windows select).
They do not download anything. `//:local` replaces the host and target platforms.

The real `smoke_test` is a custom Starlark `rule(test = True)` that writes an
executable `/bin/bash` script with an explicit exit code. It does not use
`sh_test`, rules_shell, a language toolchain, or external runfiles. Bazel's own
bundled test launcher remains in use, so these are actual test actions producing
real testResult and testSummary events, not synthetic BES messages. Shell builds
use `/bin/bash`, `/bin/cat`, and `/usr/bin/sha256sum`; Bazel's bundled launcher also
uses standard Linux utilities. No system Java/compiler, registry, or internet
access is needed. Setup extracts the bundled JDK and runtime outside the budget.

## Four fresh-root executions

Every invocation uses `--batch` and a distinct, initially nonexistent
`output_user_root`. The installation is shared read-only, never a local action
cache. All commands enable real Bazel `--remote_cache_compression`.

1. **Cold build:** `//:artifact` copies the original exact 50-byte payload;
   `//:large` copies the deterministic 4 MiB `large-input.bin`; `//:receipt`
   reads that intermediate and writes its SHA256. Require three actual AC
   NOT_FOUND RPCs, three local actions, zero remote hits, exact output bytes, and
   a successful zstd ByteStream upload for the large output's precise digest.
2. **Full download hit:** same targets, new local root. Require three successful
   AC RPCs, three remote hits, zero local actions, exact bytes for all outputs,
   and a successful zstd ByteStream read of that same large digest. Compressed
   bytes sent/read must each exceed 1 MiB and remain smaller than the original
   output. Require actual `WriteDetails.num_writes > 1` and
   `ReadDetails.num_reads > 1` in the pinned Bazel gRPC log (both field 2, int64),
   proving multiple stream requests/responses rather than just large plaintext.
3. **Minimal download hit:** same targets, another new root, this time with
   `--remote_download_minimal`. Require three AC hits and zero local actions.
   All three output files must be **absent**, while their exact digest/size pairs
   remain present in the returned ActionResults. The gRPC log must contain no
   ByteStream read of the large intermediate. This proves behavior, not just
   that a flag appeared in the command.
4. **Real tests:** `bazel test //:passing_test //:failing_test`, a fourth fresh
   root, disabled test-result caching. Exit **3 / TESTS_FAILED is expected**.
   Validate both labels' emitted result AND summary events, log-output metadata,
   and exactly one run each. Fetch the completed invocation through app RPC and
   require the expected failed-test exit status. Fetch each label via GetTarget,
   require its PASSED/FAILED target status, and compare the ingested testResult
   payload/ID and testSummary using exact protobuf equality with Bazel's
   emitted BEP. GetInvocation deliberately paginates these events into target
   groups, so testing only `inv.event` would incorrectly report missing tests.

Server cache statistics, when populated, must agree with cold/hit behavior.
The standalone server can expose an empty cache-stats message; it is recorded as
unavailable, never substituted for mandatory client-side RPC and action evidence.

## Browser integration and artifacts

After `bazel.cases(ctx)`, call **`bazel.browser_targets_check(ctx, page)`** inside
the existing browser session. It reuses the page's offline routing/error capture
and does not close the page or browser. It opens the real test invocation's
Targets tab and requires the failing card to contain exactly `//:failing_test`
and “1 test failed”, and the passing-test card to contain exactly
`//:passing_test` and “1 test passed”. Merely seeing a failed invocation is not
enough. DOM and screenshot are saved even if a browser assertion fails.

`bazel/summary.json` records execution times, UUIDs, roots, hits, transferred byte
counts and actual stream message counts, the large digest/size, minimal absent outputs/read resources, and test
statuses. Each execution retains its command, combined Bazel log, local BEP,
profile, binary remote gRPC log, and fetched invocation. Cache builds additionally
save a decoded gRPC JSON projection; full-download builds save all three output
files. `bazel/test/` holds both GetTarget responses and
`targets-browser.{txt,png}`. The projection follows the pinned Bazel
`src/main/protobuf/remote_execution_log.proto` field numbers, referenced directly
in the parser; it uses existing protobuf descriptors, not runtime protoc.

Harness regressions: `python3 -m unittest discover -s tests -p test_bazel.py`.
The stdlib-only wrapper runs seven evidence checks in the pinned venv, so CI
discovery needs no system grpc/protobuf packages.
These include negative checks for missing/swapped/cached test events, missing
compressed/multi-message transfer evidence, malformed gRPC framing, and minimal-mode regressions
(downloaded/materialized outputs or incorrect AC metadata).

Observed full runner validation: `results/bazel-chunked-wire`, 18.001 seconds
with a 30-second supervisor budget. The 4,194,304-byte output uploaded as
2,161,316 compressed bytes in 132 requests and downloaded as 2,104,273 compressed
bytes in 9 responses. All 14 system-Python-discovered harness tests also passed;
all four Bazel cases and the reused Chromium Targets check passed. The full
profile correctly remained red for the independent pending-artifact persistence
404 and its strict server ERR. No sleeps or artifact-persistence disabling were
introduced to hide those failures.
