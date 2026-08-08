#!/usr/bin/env python3
"""Build a deterministic release metadata manifest for local-wiki-librarian."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import tarfile
from typing import Any, Iterable
import zipfile


VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
TAG_RE = re.compile(r"^v([0-9]+\.[0-9]+\.[0-9]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def validate_member(name: str) -> str:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe artifact member: {name!r}")
    return name


def artifact_members(path: Path) -> list[str]:
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            return sorted(validate_member(name) for name in archive.namelist())
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return sorted(validate_member(member.name) for member in archive.getmembers())
    raise ValueError(f"unsupported release artifact: {path.name}")


def project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise ValueError("pyproject.toml has no literal project version")
    return match.group(1)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_manifest(
    *,
    source_root: Path,
    artifacts: Iterable[Path],
    tag: str,
    source_sha: str | None = None,
    verifier_results: Iterable[Path] = (),
) -> dict[str, Any]:
    root = source_root.resolve()
    version = project_version(root)
    tag_match = TAG_RE.fullmatch(tag)
    if not tag_match or tag_match.group(1) != version:
        raise ValueError(f"tag {tag!r} must be v{version}")
    commit = source_sha or git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", f"{commit}^{{tree}}")
    if not SHA_RE.fullmatch(commit) or not re.fullmatch(r"^[0-9a-f]{40}", tree):
        raise ValueError("source commit/tree must be full Git object IDs")

    artifact_rows = []
    seen_names: set[str] = set()
    for artifact in sorted((Path(item).resolve() for item in artifacts), key=lambda item: item.name):
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
        if artifact.name in seen_names:
            raise ValueError(f"duplicate artifact name: {artifact.name}")
        seen_names.add(artifact.name)
        artifact_rows.append(
            {
                "name": artifact.name,
                "path": relative_path(root, artifact),
                "sha256": sha256(artifact),
                "size": artifact.stat().st_size,
                "members": artifact_members(artifact),
            }
        )
    if {row["name"].endswith(".whl") for row in artifact_rows} != {True, False}:
        raise ValueError("manifest requires at least one wheel and one sdist")
    required_vendor_names = {"LICENSE", "UPSTREAM.json"}
    for row in artifact_rows:
        members = set(row["members"])
        if not all(
            any(member.endswith(f"/_vendor/wikimap/{name}") for member in members)
            for name in required_vendor_names
        ):
            raise ValueError(f"{row['name']} omits vendored LICENSE or UPSTREAM.json")

    vendor_root = root / "src/local_wiki_librarian/_vendor/wikimap"
    upstream_path = vendor_root / "UPSTREAM.json"
    upstream = load_json(upstream_path)
    if not isinstance(upstream, dict) or not isinstance(upstream.get("files"), dict):
        raise ValueError("vendored UPSTREAM.json has no files object")
    vendor_files = {}
    for relative, expected in sorted(upstream["files"].items()):
        path = vendor_root / relative
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f"vendored provenance mismatch: {relative}")
        vendor_files[relative] = {
            "path": f"{relative_path(root, path)}",
            "sha256": sha256(path),
        }

    producer_paths = ["scripts/build-release-manifest.py", "scripts/verify-installed-package.py"]
    producer_scripts = {}
    for relative in producer_paths:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(path)
        producer_scripts[relative] = sha256(path)

    verifiers = []
    for result_path in sorted((Path(item).resolve() for item in verifier_results), key=lambda item: item.name):
        payload = load_json(result_path)
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError(f"verifier result is not successful: {result_path}")
        verifiers.append({"path": relative_path(root, result_path), "result": payload})

    notice_members = {member for row in artifact_rows for member in row["members"] if Path(member).name == "NOTICE"}
    return {
        "schema_version": 1,
        "distribution": {"name": "local-wiki-librarian", "version": version},
        "source": {
            "repository": "https://github.com/JeremyDev87/librarian",
            "commit": commit,
            "tree": tree,
            "version": version,
            "tag": tag,
        },
        "artifacts": artifact_rows,
        "provenance": {
            "upstream_json": {
                "path": relative_path(root, upstream_path),
                "sha256": sha256(upstream_path),
                "content": upstream,
            },
            "vendor_files": vendor_files,
        },
        "producer_scripts": producer_scripts,
        "verifiers": verifiers,
        "policy": {
            "notice": {
                "path": "NOTICE",
                "included_in_artifacts": bool(notice_members),
                "decision": "Root NOTICE remains source-only and non-blocking; vendored LICENSE and UPSTREAM.json remain packaged and hash-verified.",
            }
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-sha")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--artifacts", nargs="+", type=Path, required=True)
    parser.add_argument("--verifier-result", action="append", nargs="+", default=[], type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(
            source_root=args.source_root,
            artifacts=args.artifacts,
            tag=args.tag,
            source_sha=args.source_sha,
            verifier_results=[item for group in args.verifier_result for item in group],
        )
        args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        print(f"release manifest failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "output": str(args.output), "schema_version": 1}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
