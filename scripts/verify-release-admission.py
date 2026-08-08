#!/usr/bin/env python3
"""Fail-closed admission check for the manual PyPI publication gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
API_VERSION = "2022-11-28"


def api_request(url: str, token: str, accept: str = "application/vnd.github+json") -> Any:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "local-wiki-librarian-release-admission",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"GitHub API request failed for {url}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API returned non-JSON for {url}") from exc


def download_asset(url: str, token: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "local-wiki-librarian-release-admission",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"release asset download failed for {url}: {exc}") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def resolve_tag(repo_url: str, tag: str, token: str) -> str:
    encoded = quote(tag, safe="")
    ref = api_request(f"{repo_url}/git/ref/tags/{encoded}", token)
    obj = ref.get("object", {})
    for _ in range(3):
        if obj.get("type") == "commit":
            sha = obj.get("sha", "")
            if SHA_RE.fullmatch(sha):
                return sha
            break
        if obj.get("type") != "tag" or not SHA_RE.fullmatch(obj.get("sha", "")):
            break
        tag_object = api_request(f"{repo_url}/git/tags/{obj['sha']}", token)
        obj = tag_object.get("object", {})
    raise RuntimeError("tag does not resolve to a full commit SHA")


def load_manifest(payload: bytes) -> Dict[str, Any]:
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release-manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("release manifest schema_version must be 1")
    return manifest


def check_local_assets(local_dir: Path, manifest: Dict[str, Any]) -> None:
    local_manifest = local_dir / "release-manifest.json"
    if not local_manifest.is_file():
        raise RuntimeError("local release assets do not contain release-manifest.json")
    local = load_manifest(local_manifest.read_bytes())
    if local != manifest:
        raise RuntimeError("local release manifest differs from the admitted release manifest")
    for artifact in manifest.get("artifacts", []):
        name = artifact.get("name")
        path = local_dir / str(name)
        if not path.is_file():
            raise RuntimeError(f"local release asset missing: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != artifact.get("sha256"):
            raise RuntimeError(f"local release asset digest mismatch: {name}")


def admit(repo: str, tag: str, expected_sha: str, token: str, local_dir: Optional[Path] = None) -> Dict[str, Any]:
    if not TAG_RE.fullmatch(tag):
        raise RuntimeError("tag must match vMAJOR.MINOR.PATCH")
    if not SHA_RE.fullmatch(expected_sha):
        raise RuntimeError("expected source SHA must be a full commit SHA")
    repo_url = f"https://api.github.com/repos/{repo}"
    resolved_sha = resolve_tag(repo_url, tag, token)
    if resolved_sha != expected_sha:
        raise RuntimeError(f"tag {tag} resolves to {resolved_sha}, expected {expected_sha}")

    release = api_request(f"{repo_url}/releases/tags/{quote(tag, safe='')}", token)
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("release must be published and non-prerelease")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("release assets are unavailable")
    by_name = {asset.get("name"): asset for asset in assets if isinstance(asset, dict)}
    manifest_asset = by_name.get("release-manifest.json")
    if not manifest_asset:
        raise RuntimeError("release is missing release-manifest.json")
    wheel_names = sorted(name for name in by_name if isinstance(name, str) and name.endswith(".whl"))
    sdist_names = sorted(name for name in by_name if isinstance(name, str) and name.endswith(".tar.gz"))
    if len(wheel_names) != 1 or len(sdist_names) != 1:
        raise RuntimeError("release must contain exactly one wheel and one sdist")

    manifest = load_manifest(download_asset(manifest_asset["url"], token))
    source = manifest.get("source", {})
    if source.get("commit") != expected_sha or source.get("tag") != tag:
        raise RuntimeError("release manifest source does not match the admitted tag")
    artifact_rows = {row.get("name"): row for row in manifest.get("artifacts", []) if isinstance(row, dict)}
    if set(artifact_rows) != {wheel_names[0], sdist_names[0]}:
        raise RuntimeError("release manifest artifact names do not match the release assets")

    remote_digests = {}
    for name in (wheel_names[0], sdist_names[0]):
        asset = by_name[name]
        digest = sha256_bytes(download_asset(asset["url"], token))
        if digest != artifact_rows[name].get("sha256"):
            raise RuntimeError(f"release asset digest mismatch: {name}")
        remote_digests[name] = digest
    if local_dir is not None:
        check_local_assets(local_dir, manifest)
    return {
        "status": "ok",
        "repository": repo,
        "tag": tag,
        "source_sha": expected_sha,
        "release_id": release.get("id"),
        "manifest_asset": "release-manifest.json",
        "artifact_digests": remote_digests,
        "local_assets_checked": local_dir is not None,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG"))
    parser.add_argument("--expected-sha", default=os.environ.get("EXPECTED_SHA"))
    parser.add_argument("--local-dir", type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not args.repo or not args.tag or not args.expected_sha or not token:
        parser.error("repo, tag, expected-sha, and GH_TOKEN are required")
    try:
        receipt = admit(args.repo, args.tag, args.expected_sha, token, args.local_dir)
    except RuntimeError as exc:
        print(f"release admission failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
