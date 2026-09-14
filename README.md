# BuildBuddy binary smoke tests

Black-box, local-only integration tests for an already-built BuildBuddy enterprise
Linux amd64 binary. Default reference: **v2.303.0**. No BuildBuddy source build,
cloud account, Docker, Redis, or executor required for the core profile.

## Design

- Two-minute hard wall-clock budget for startup, checks, restart and cleanup.
- Setup (downloading the binary, Python dependencies, protobuf compilation,
  Chromium) is separate and explicitly not included in that budget.
- Fresh SQLite database, disk cache and blob storage per run; loopback listeners.
- Exercise real HTTP, gRPC and a real Chromium renderer; assert results, not just
  successful connection/status codes.
- Successful and failed synthetic builds join BES ingestion to invocation APIs,
  stored logs and rendered UI. Cache tests include positive and negative cases.
- Controlled HTTP origin tests remote asset download and CAS integration without
  depending on the public internet.
- Restart the process and verify durable state survives.
- Fail closed: unavailable services and budget overruns fail, never silently skip.

Implementation and measured results are in progress.
