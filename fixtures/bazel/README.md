# Offline real-Bazel fixture

`smoke/bazel.py` copies this workspace to the run artifacts and changes only
`nonce.txt` once per pair of builds. `SmokeCopy` is one real cacheable shell
action whose declared nonce input guarantees a cold first action-cache lookup.
The output is always the exact bytes of `payload.txt`.

Bazel 8.4.2 must use the harness's flags: disabling Bzlmod and autoload alone is
insufficient because Bazel's default WORKSPACE suffix loads language rules.
`--experimental_resolved_file_instead_of_workspace=resolved.bzl` replaces that
initialization with **zero repositories**, while `//:local` replaces the default
host and target platforms. `--repository_disable_download` fails closed on any
accidental repository download. No compiler, system Java, external toolchain,
registry, or internet access is needed during builds; `/bin/bash` and `/bin/cat`
are the only action tools. Bazel's bundled JDK is extracted during setup.

Both builds use `--batch` and distinct output-user-root directories. Sharing the
read-only installation is not sharing a local action cache. The first build
must log a local action; the second must log exactly one remote cache hit and
no local actions. Exact output bytes and fully ingested successful BES
invocations are checked for both. Nonempty server cache statistics are checked
as additional evidence; the standalone server may expose an empty stats message
when it has no collector, so those counters are explicitly reported unavailable.

Artifacts include commands, combined output logs, JSON build-event files,
Bazel JSON profiles, fetched invocation JSON, and an aggregate summary.
