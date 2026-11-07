"""Build and persist authority manifests from wiki snapshots.

Reads a snapshot's ``current.json`` pointer, resolves the generation directory,
compiles the authority manifest, and writes it alongside the snapshot.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .authority import AuthorityManifest, compile_authority
from .paths import read_bytes_within, resolve_within, write_bytes_atomic
from .snapshot import _CURRENT_FILENAME

__all__ = [
    "build_authority_manifest",
    "load_authority_manifest",
    "load_verified_generation",
]

_AUTHORITY_FILENAME = "authority.json"
_MANIFEST_FILENAME = "manifest.json"

# Keep verified views alive for the lifetime of the CLI process.  Returning a
# private view instead of the shared generation directory prevents a
# concurrent state writer from changing bytes between verification and
# authority compilation/index/search/readback.
_VERIFIED_VIEWS: list[tempfile.TemporaryDirectory] = []


def _materialize_verified_view(
    manifest_bytes: bytes, payloads: dict[str, bytes],
) -> Path:
    tempdir = tempfile.TemporaryDirectory(prefix="librarian-verified-")
    try:
        view = Path(tempdir.name) / "snapshot"
        view.mkdir()
        for relative, payload in payloads.items():
            target = resolve_within(view, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        resolve_within(view, _MANIFEST_FILENAME).write_bytes(manifest_bytes)
    except Exception:
        tempdir.cleanup()
        raise
    _VERIFIED_VIEWS.append(tempdir)
    return view


def load_verified_generation(state_root: Path) -> tuple[dict, Path, dict]:
    """Load a current generation after verifying pointer, manifest, and files."""
    state_root = resolve_within(state_root, ".")
    current = json.loads(read_bytes_within(state_root, _CURRENT_FILENAME))
    if current.get("schema_version") != 1:
        raise RuntimeError("unsupported current snapshot schema")
    generation = current.get("generation")
    if not isinstance(generation, str) or not generation:
        raise RuntimeError("invalid snapshot generation")
    gen_dir = resolve_within(resolve_within(state_root, "snapshots"), generation)
    manifest_bytes = read_bytes_within(state_root, Path("snapshots") / generation / "manifest.json")
    if current.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise RuntimeError("current snapshot manifest integrity check failed")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 1 or manifest.get("generation") != generation:
        raise RuntimeError("generation manifest identity mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RuntimeError("generation manifest files are malformed")

    seen_paths: set[str] = set()
    usable_paths: set[str] = set()
    payloads: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("generation manifest entry is malformed")
        relative = entry.get("relative_path")
        state = entry.get("state")
        if not isinstance(relative, str) or state not in {
            "copied", "stale", "quarantined", "deleted"
        }:
            raise RuntimeError("generation manifest entry is malformed")
        if relative in seen_paths:
            raise RuntimeError("generation manifest contains duplicate paths")
        seen_paths.add(relative)
        path = resolve_within(gen_dir, relative)
        if state in {"copied", "stale"}:
            payload = read_bytes_within(state_root, Path("snapshots") / generation / relative)
            if entry.get("size") != len(payload):
                raise RuntimeError(f"snapshot file integrity mismatch (size): {relative}")
            if hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
                raise RuntimeError(f"snapshot file integrity mismatch: {relative}")
            usable_paths.add(relative)
            payloads[relative] = payload
        elif path.exists():
            raise RuntimeError("non-usable manifest entry unexpectedly has snapshot bytes")

    actual_paths = {
        path.relative_to(gen_dir).as_posix()
        for path in gen_dir.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != usable_paths:
        raise RuntimeError("snapshot Markdown inventory differs from generation manifest")

    for relative, pointer_key, label in (
        ("authority.json", "authority_sha256", "authority manifest"),
        (".wikimap/index.db", "wikimap_index_sha256", "wikimap index"),
    ):
        expected = current.get(pointer_key)
        if expected is None:
            continue
        payload = read_bytes_within(state_root, Path("snapshots") / generation / relative)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise RuntimeError(f"{label} integrity check failed")
        payloads[relative] = payload
    return current, _materialize_verified_view(manifest_bytes, payloads), manifest


def build_authority_manifest(state_root: Path) -> AuthorityManifest:
    """Build an authority manifest from the current snapshot generation.

    Args:
        state_root: External state directory containing ``current.json``.

    Returns:
        AuthorityManifest compiled from the current snapshot generation.

    Raises:
        FileNotFoundError: If no snapshot exists.
    """
    state_root = resolve_within(state_root, ".")
    current, gen_dir, _ = load_verified_generation(state_root)
    generation = current["generation"]
    manifest = compile_authority(gen_dir, generation)

    # Write authority manifest to state root
    write_bytes_atomic(
        state_root, _AUTHORITY_FILENAME,
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )

    return manifest


def load_authority_manifest(state_root: Path) -> dict | None:
    """Load generation-local authority first, then the legacy projection."""
    state_root = resolve_within(state_root, ".")
    try:
        current_payload = read_bytes_within(state_root, _CURRENT_FILENAME)
    except FileNotFoundError:
        current_payload = None
    if current_payload is not None:
        current = json.loads(current_payload)
        generation = current.get("generation")
        expected = current.get("authority_sha256")
        if isinstance(generation, str) and expected:
            resolve_within(resolve_within(state_root, "snapshots"), generation)
            payload = read_bytes_within(state_root, Path("snapshots") / generation / _AUTHORITY_FILENAME)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise RuntimeError("authority manifest integrity check failed")
            return json.loads(payload)
    try:
        return json.loads(read_bytes_within(state_root, _AUTHORITY_FILENAME))
    except FileNotFoundError:
        return None
