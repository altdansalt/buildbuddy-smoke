# Known product failure: acknowledged artifact lost from durable blob storage at shutdown

**Pinned enterprise v2.303.0 fails the strict full profile.** This is a product
failure detected by the harness, not a skipped check, expected-pass exception,
or claim that the existing core cache/BES data paths fail.

Binary SHA256: `1ea34ea814bd4a21021f4b0698cad6d726cce7e1843c88bda921c16a6ab73fd3`.
Source revision: `6fc01488a60d69832f86eff154ac985e1170653e`.

## Reproduce

After one-time setup:

```sh
python3 run.py --profile full --output results/shutdown-repro
```

Expected for this release: exit **1**, with these failures:

- `persistence.shutdown_artifact_without_cas`: HTTP 404 for the artifact whose
  BES stream completed immediately before SIGTERM.
- `app.log_hygiene`: `ERR ... Failed to stream to blobstore ...` during shutdown.

Other unexpected failures still fail normally; this is not an assertion that
"any two failures" are acceptable. CI retains a failing conclusion, plus reports,
logs, fixture IDs and browser screenshots. There is no error allowlist.

## What the regression actually proves

1. Upload a uniquely identified small file through CAS, then publish a valid
   synthetic BES invocation announcing that file in `BuildToolLogs`.
2. Do this early for a **control**, and again immediately before SIGTERM for the
   **pending** artifact. Require every sequence number and stream ID to be ACKed.
3. For the pending artifact, **do not sleep or poll for background persistence**.
   Record ACK-to-SIGTERM latency in `fixtures.json`.
4. Restart on the original database/blobstore/cache and require both original
   CAS payloads to remain readable byte-for-byte.
5. Restart on the same database/blobstore with a **new empty cache directory**.
   Preserve the original cache directory untouched for debugging.
6. Prove both digests are absent from this fresh CAS, then use the actual HTTP
   artifact-download route with invocation ID + bytestream URL. Check exact bytes,
   and prove the CAS remains empty afterward, so a warm-cache hit cannot hide
   failed blobstore persistence.

The control downloads successfully; the pending artifact returns 404. Thus this
is a failed **durable artifact copy**, not a claim of permanent loss from the
original CAS. A normal restart retaining the hot cache would hide the defect.

## Why the error occurs

The pinned implementation queues stats/artifact work after finalizing the
invocation. `statsRecorder.handleTask` waits the default 500ms finalization delay,
then `persistArtifact` reads cache bytes through the app's ByteStream client to
copy them into blob storage. Shutdown drains this work, but the gRPC listener can
already be stopped. Logs show connection refused, followed by the persistence
error, followed by a successful process exit.

Relevant source: `server/build_event_protocol/build_event_handler/build_event_handler.go`
(`handleTask`, `Stop`, `persistArtifact`). The HTTP fallback is in
`server/buildbuddy_server/buildbuddy_server.go` (`ServeHTTP`, `serveArtifact`).

The old harness checked only exit code 0 and selected durable data; it did not
inspect late application errors or remove hot-cache masking. **Its earlier
“clean SIGTERM” wording was too strong.**

## Harness policy

The supervisor now inspects every app generation **after child-process cleanup**,
so errors emitted after the worker finishes still turn an otherwise successful
run into failure. ANSI-colored text errors, structured JSON error/fatal/panic
records and Go crash/goroutine-dump markers are covered by regression tests.
Warnings from deliberately malformed requests or the deliberate BES transport
cancellation remain available in logs but are not treated as fatal errors.

The inspection subprocess is capped by both a 1-second allowance and the
remaining overall budget. It streams bounded lines, limits total/file sizes and
retains bounded finding samples. Any inspection timeout, unreadable/oversized
log or overlong line fails explicitly as **incomplete inspection**, never green.
Original logs remain available. Unit tests include a blocked scanner that must
be killed/reaped, oversized logs and errors beyond the retained sample count.

Do not make this green by adding a sleep, disabling artifact persistence,
allowlisting its errors or turning this assertion into a skip. Fix the product's
shutdown ordering so pending copies can finish while their dependencies are
available, then rerun the same test against that supplied enterprise binary.

`--profile core` omits the real Bazel and shutdown-artifact scenarios, but retains
strict log inspection and live/reconnected BES coverage. A passing core run is
not evidence that this full-profile durability defect is fixed.
