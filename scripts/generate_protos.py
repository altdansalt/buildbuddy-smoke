#!/usr/bin/env python3
"""Generate the smoke runner's protobuf import closure from verified archives.

Run with .venv/bin/python scripts/generate_protos.py. No Bazel, Go, git checkout,
or system protoc is needed. Sources stay in ignored .tools/; generated/ is the
Python import root. Dependencies match the pinned BuildBuddy source's Bazel/Go
configuration. Only selected .proto files are copied out of source archives.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent
BUILDBUDDY_COMMIT = "6fc01488a60d69832f86eff154ac985e1170653e"
# name, GitHub repository, immutable commit, archive SHA256, source prefix
ARCHIVES = (
    ("buildbuddy", "buildbuddy-io/buildbuddy", BUILDBUDDY_COMMIT,
     "6374b31dfd08c1b384c295a428539ead7b010a82a502310cec1888f973525c42", "proto/"),
    ("googleapis", "googleapis/googleapis", "20ac242a6b3a723cb10c1a0201209261addaf7d8",
     "2e54dd6e7829afa9f382b67b3eea6730963df65381255f012ac4bd23b3654fb5", "google/"),
    ("kythe", "buildbuddy-io/kythe", "533f25354661f90e29c8755160502e02eb3cfd0e",
     "2295ffa35e4f4ed4ad4c9775331d7721a7cb61e7f7ef0e2a3cab62195bc1e738", "kythe/proto/"),
    ("vtprotobuf", "planetscale/vtprotobuf", "79df5c4772f27b4e08f9612d045e1e1b21ac963a",
     "a57234ab7636c0e67cbc4d961b79aab4dfff1f9b51e40888ba2bc44f7c068592", "include/"),
)
TARGETS = (
    "proto/remote_execution.proto",
    "google/bytestream/bytestream.proto",
    "proto/remote_asset.proto",
    "proto/publish_build_event.proto",
    "proto/build_event_stream.proto",
    "proto/invocation.proto",
    "proto/eventlog.proto",
    "proto/buildbuddy_service.proto",
    "google/rpc/code.proto",
    "google/rpc/error_details.proto",
)
IMPORT_RE = re.compile(r'^\s*import\s+(?:(?:public|weak)\s+)?"([^"]+)"\s*;', re.MULTILINE)
VT_IMPORT = "github.com/planetscale/vtprotobuf/vtproto/ext.proto"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if sys.version_info >= (3, 11) else _sha256_310(stream)


def _sha256_310(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path, expected: str) -> None:
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".partial")
        print(f"Downloading {url}", flush=True)
        subprocess.run(["curl", "--fail", "--location", "--retry", "3",
                        "--connect-timeout", "30", "--output", str(temporary), url], check=True)
        if sha256(temporary) != expected:
            raise RuntimeError(f"SHA256 mismatch: {temporary}; refusing unverified archive")
        temporary.replace(path)
    if sha256(path) != expected:
        raise RuntimeError(f"SHA256 mismatch: {path}; remove this cache file and retry")


def load_sources(downloads: Path) -> dict[str, str]:
    sources = {}
    for name, repo, commit, expected, prefix in ARCHIVES:
        archive_path = downloads / f"{name}-{commit}.tar.gz"
        download(f"https://codeload.github.com/{repo}/tar.gz/{commit}", archive_path, expected)
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith(".proto"):
                    continue
                relative = member.name.partition("/")[2]
                if not relative.startswith(prefix):
                    continue
                if name == "vtprotobuf":
                    relative = relative.removeprefix("include/")
                # protoc's Python imports cannot represent the literal directory
                # 'github.com'. Normalize this Go-only option's filename (not its
                # protobuf package/extension names or any wire fields).
                relative = relative.replace(VT_IMPORT, "vtproto/ext.proto")
                if ".." in PurePosixPath(relative).parts or relative.startswith("/"):
                    raise RuntimeError(f"Unsafe archive member: {member.name}")
                stream = archive.extractfile(member)
                assert stream is not None
                sources[relative] = stream.read().decode("utf-8").replace(VT_IMPORT, "vtproto/ext.proto")
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "generated")
    args = parser.parse_args()
    import grpc_tools
    well_known = Path(grpc_tools.__file__).parent / "_proto"
    downloads = ROOT / ".tools" / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    sources = load_sources(downloads)
    selected: set[str] = set()

    def visit(name: str) -> None:
        if name in selected:
            return
        if name.startswith("google/protobuf/") and (well_known / name).is_file():
            return  # Runtime already provides these; do not shadow protobuf.
        if name not in sources:
            raise RuntimeError(f"Unresolved proto import: {name}")
        selected.add(name)
        for dependency in IMPORT_RE.findall(sources[name]):
            visit(dependency)

    for target in TARGETS:
        visit(target)
    staging = ROOT / ".tools" / "proto-src"
    for name in sorted(selected):
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(sources[name])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "grpc_tools.protoc", f"-I{staging}", f"-I{well_known}",
        f"--python_out={output}", f"--grpc_python_out={output}",
        *sorted(selected),
    ], check=True)
    # A regular proto package avoids collisions with installed 'proto' packages.
    # google must remain a namespace package so google.protobuf stays importable.
    (output / "proto" / "__init__.py").write_text('"""Generated BuildBuddy protocol bindings."""\n')
    modules = [name[:-6].replace("/", ".") + suffix
               for name in sorted(selected) for suffix in ("_pb2", "_pb2_grpc")]
    # Use a clean subprocess so repeated runs never reuse stale imported modules.
    subprocess.run([sys.executable, "-c",
                    "import importlib,json,sys; sys.path.insert(0,sys.argv[1]); "
                    "[importlib.import_module(m) for m in json.loads(sys.argv[2])]",
                    str(output), json.dumps(modules)], check=True)
    manifest = {
        "buildbuddy_version": "v2.303.0", "buildbuddy_commit": BUILDBUDDY_COMMIT,
        "archives": [{"name": n, "repo": r, "commit": c, "sha256": s}
                     for n, r, c, s, _ in ARCHIVES],
        "targets": list(TARGETS), "protos": sorted(selected),
        "python": sys.version,
        "packages": {name: importlib.metadata.version(name)
                     for name in ("grpcio", "grpcio-tools", "protobuf")},
        "normalized_import": {VT_IMPORT: "vtproto/ext.proto"},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Generated {len(selected)} protos; verified all {len(modules)} module imports in {output}")


if __name__ == "__main__":
    main()
