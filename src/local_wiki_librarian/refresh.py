"""Atomic maintenance operations for Librarian's derived Wiki state.

Supported writers are cooperative: every stage, promotion, and rollback must
use this module's shared writer lock. Permission hardening and digest readback
defend against accidental pathname replacement and detect corruption; they are
not an immutability boundary against an arbitrary same-UID process, which can
chmod or directly rewrite both generation and pointer state.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .authority import compile_authority
from .lexical import WikimapAdapter
from .paths import resolve_within
from .snapshot import (
    SnapshotError,
    SnapshotResult,
    _canonical_json_sha256,
    _inventory_rows,
    _inventory_sha256,
    build_snapshot,
    discover_markdown,
)
from .vendor import bundled_vendor_root, bundled_wikimap_path, verify_vendor

_LOCK_FILENAME = ".librarian-snapshot.lock"


@dataclass(frozen=True)
class RefreshResult:
    generation: str
    summary: dict
    authority_entries: int
    unresolved_redirects: int
    authority_sha256: str
    wikimap_index_sha256: str
    manifest_sha256: str
    candidate_sha256: str | None = None
    promoted: bool = True
    migration_receipt_sha256: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "status": "ok",
            "generation": self.generation,
            "summary": self.summary,
            "authority_entries": self.authority_entries,
            "unresolved_redirects": self.unresolved_redirects,
            "authority_sha256": self.authority_sha256,
            "wikimap_index_sha256": self.wikimap_index_sha256,
            "manifest_sha256": self.manifest_sha256,
            **({"candidate_sha256": self.candidate_sha256} if self.candidate_sha256 else {}),
            "promoted": self.promoted,
            **(
                {"migration_receipt_sha256": self.migration_receipt_sha256}
                if self.migration_receipt_sha256 is not None else {}
            ),
            "warnings": list(self.warnings),
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _find_wikimap_path() -> Path | None:
    """Locate vendored Wikimap in a source tree or installed wheel."""
    configured = os.environ.get("LIBRARIAN_WIKIMAP_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    try:
        vendor_root = bundled_vendor_root()
        verify_vendor(vendor_root)
        return bundled_wikimap_path()
    except (FileNotFoundError, OSError, RuntimeError):
        return None


def _write_fsync(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        _write_fsync(tmp, payload)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
        if path.read_bytes() != payload:
            raise RuntimeError(f"atomic write readback failed: {path.name}")
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _writer_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / _LOCK_FILENAME
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError("another snapshot writer is active (lock contention)") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _current_bytes(state_root: Path) -> bytes | None:
    path = state_root / "current.json"
    return path.read_bytes() if path.is_file() else None


def current_pointer_sha256(state_root: Path) -> str | None:
    """Return the digest used by promotion and rollback CAS checks."""
    payload = _current_bytes(Path(state_root))
    return _sha256(payload) if payload is not None else None


def _quick_check_index(index_path: Path) -> None:
    if not index_path.is_file():
        raise RuntimeError("wikimap index was not created")
    try:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("wikimap index integrity check failed") from exc
    if not result or result[0] != "ok":
        raise RuntimeError("wikimap index integrity check failed")


def _semantic_index_sha256(index_path: Path) -> str:
    """Hash query-relevant Wikimap rows while ignoring volatile SQLite bytes."""
    queries = (
        "SELECT path, sha, title, words FROM files ORDER BY path",
        "SELECT path, line, level, heading, content FROM sections ORDER BY path, line, level, heading, content",
        "SELECT rowid, path, line, heading, content FROM sections_fts ORDER BY rowid",
        "SELECT src, dst, kind FROM links ORDER BY src, dst, kind",
        "SELECT path, tag FROM tags ORDER BY path, tag",
        "SELECT path, alias FROM aliases ORDER BY path, alias",
        "SELECT src, dst, alt FROM img_alts ORDER BY src, dst, alt",
        "SELECT src, dst, relation, rationale, origin, created, src_sha, dst_sha FROM edges "
        "ORDER BY src, dst, relation, rationale, origin, created, src_sha, dst_sha",
        "SELECT path, sha, vec FROM embeds ORDER BY path",
        "SELECT question, insight, created, sources FROM notes ORDER BY question, insight, created, sources",
    )
    digest = hashlib.sha256()
    try:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            for query in queries:
                digest.update(query.encode("utf-8"))
                for row in connection.execute(query):
                    digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    digest.update(b"\n")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("wikimap semantic index verification failed") from exc
    return digest.hexdigest()


def _authority_bytes(snapshot_dir: Path, generation: str) -> tuple[bytes, int, int]:
    authority = compile_authority(snapshot_dir, generation)
    if authority.unresolved_redirects:
        raise RuntimeError("authority compilation has unresolved redirects")
    payload = json.dumps(
        authority.to_dict(), indent=2, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return payload, len(authority.entries), len(authority.unresolved_redirects)


def build_root_migration_receipt(state_root: Path, new_root: Path) -> dict:
    """Build an exact, reviewable receipt without mutating either root or state."""
    state_root = Path(state_root)
    new_root = Path(new_root).resolve()
    current_payload = resolve_within(state_root, "current.json").read_bytes()
    current = json.loads(current_payload)
    old_root = Path(str(current.get("canonical_root", ""))).resolve()
    if old_root == new_root:
        raise ValueError("root migration requires distinct canonical roots")
    if not new_root.is_dir():
        raise FileNotFoundError("new canonical root does not exist")

    new_entries: list[dict] = []
    for entry in discover_markdown(new_root):
        try:
            payload = entry.absolute_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("new canonical root is not fully readable") from exc
        new_entries.append({
            "relative_path": entry.relative_path,
            "size": len(payload),
            "sha256": _sha256(payload),
            "state": "copied",
        })
    old_entries = current.get("files")
    if not isinstance(old_entries, list):
        raise RuntimeError("current snapshot inventory is malformed")
    old_paths = {row[0] for row in _inventory_rows(old_entries)}
    new_paths = {row[0] for row in _inventory_rows(new_entries)}
    old_only_paths = sorted(old_paths - new_paths)
    return {
        "schema_version": 1,
        "expected_current_generation": current.get("generation"),
        "expected_current_sha256": _sha256(current_payload),
        "from_root": str(old_root),
        "to_root": str(new_root),
        "old_inventory_sha256": _inventory_sha256(old_entries),
        "new_inventory_sha256": _inventory_sha256(new_entries),
        "old_only_paths": old_only_paths,
        "old_only_paths_sha256": _canonical_json_sha256(old_only_paths),
    }


def stage_refresh(
    canonical_root: Path,
    state_root: Path,
    wikimap_path: Path,
    *,
    migration_receipt: dict | None = None,
) -> RefreshResult:
    """Materialize and verify a candidate generation without changing current.json."""
    canonical_root = Path(canonical_root)
    state_root = Path(state_root)
    wikimap_path = Path(wikimap_path)
    if not wikimap_path.is_file():
        raise FileNotFoundError("wikimap runtime not found")

    prepared: dict[str, Any] = {}

    def prepare(snapshot_dir: Path, manifest: dict) -> dict:
        generation = str(manifest["generation"])
        summary = manifest.get("summary", {})
        stale = int(summary.get("stale", 0) or 0)
        quarantined = int(summary.get("quarantined", 0) or 0)
        deleted = int(summary.get("deleted", 0) or 0)
        if stale or quarantined or deleted:
            raise RuntimeError(
                "refresh requires a fully fresh canonical read: "
                f"stale={stale}, quarantined={quarantined}, deleted={deleted}"
            )
        authority_payload, entry_count, unresolved = _authority_bytes(snapshot_dir, generation)
        authority_path = snapshot_dir / "authority.json"
        _write_fsync(authority_path, authority_payload)
        WikimapAdapter(wikimap_path, snapshot_dir).index()
        index_path = snapshot_dir / ".wikimap" / "index.db"
        _quick_check_index(index_path)
        index_payload = index_path.read_bytes()
        prepared.update({
            "authority_entries": entry_count,
            "unresolved_redirects": unresolved,
            "authority_sha256": _sha256(authority_payload),
            "wikimap_index_sha256": _sha256(index_payload),
        })
        descriptor = {
            "schema_version": 1,
            "generation": generation,
            "manifest_sha256": _sha256((snapshot_dir / "manifest.json").read_bytes()),
            "authority_sha256": prepared["authority_sha256"],
            "wikimap_index_sha256": prepared["wikimap_index_sha256"],
        }
        descriptor_payload = json.dumps(
            descriptor, indent=2, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        _write_fsync(snapshot_dir / "candidate.json", descriptor_payload)
        prepared["candidate_sha256"] = _sha256(descriptor_payload)
        return {
            "authority_sha256": prepared["authority_sha256"],
            "wikimap_index_sha256": prepared["wikimap_index_sha256"],
            "candidate_sha256": prepared["candidate_sha256"],
        }

    snapshot: SnapshotResult = build_snapshot(
        canonical_root,
        state_root,
        prepare_generation=prepare,
        promote=False,
        migration_receipt=migration_receipt,
    )
    manifest_path = state_root / "snapshots" / snapshot.generation / "manifest.json"
    return RefreshResult(
        generation=snapshot.generation,
        summary=snapshot.summary,
        authority_entries=int(prepared["authority_entries"]),
        unresolved_redirects=int(prepared["unresolved_redirects"]),
        authority_sha256=str(prepared["authority_sha256"]),
        wikimap_index_sha256=str(prepared["wikimap_index_sha256"]),
        manifest_sha256=_sha256(manifest_path.read_bytes()),
        candidate_sha256=str(prepared["candidate_sha256"]),
        promoted=False,
        migration_receipt_sha256=snapshot.manifest.get("migration_receipt_sha256"),
    )


_MANIFEST_STATES = ("copied", "stale", "quarantined", "deleted")


def _validated_manifest_entries(
    manifest: dict, snapshot_dir: Path, *, require_fresh: bool,
) -> list[dict]:
    base_keys = {
        "schema_version", "generation", "canonical_root", "created_at",
        "file_count", "files", "summary",
    }
    receipt_keys = {"migration_receipt", "migration_receipt_sha256"}
    keys = frozenset(manifest)
    if keys not in {frozenset(base_keys), frozenset(base_keys | receipt_keys)}:
        raise RuntimeError("generation manifest schema is malformed")
    generation = manifest.get("generation")
    canonical_root = manifest.get("canonical_root")
    created_at = manifest.get("created_at")
    file_count = manifest.get("file_count")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(generation, str) or not generation
        or not isinstance(canonical_root, str) or not Path(canonical_root).is_absolute()
        or not isinstance(created_at, str) or not created_at
        or not isinstance(file_count, int) or isinstance(file_count, bool) or file_count < 0
    ):
        raise RuntimeError("generation manifest metadata is malformed")
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or set(summary) != set(_MANIFEST_STATES):
        raise RuntimeError("generation summary is malformed")
    counts: dict[str, int] = {}
    for state in _MANIFEST_STATES:
        value = summary.get(state)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("generation summary count is malformed")
        counts[state] = value

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise RuntimeError("generation manifest files are malformed")
    if file_count != len(entries):
        raise RuntimeError("generation file count differs from manifest entries")
    actual_counts = {state: 0 for state in _MANIFEST_STATES}
    seen_paths: set[str] = set()
    validated: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("generation manifest entry is malformed")
        relative = entry.get("relative_path")
        state = entry.get("state")
        expected_entry_keys = {"relative_path", "sha256", "size", "state"}
        if state in {"stale", "quarantined"}:
            expected_entry_keys.add("error")
        if set(entry) != expected_entry_keys:
            raise RuntimeError("generation manifest entry schema is malformed")
        size = entry.get("size")
        sha = entry.get("sha256")
        if not isinstance(relative, str) or state not in actual_counts:
            raise RuntimeError("generation manifest entry is malformed")
        resolve_within(snapshot_dir, relative)
        if relative in seen_paths:
            raise RuntimeError("generation manifest contains duplicate paths")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not isinstance(sha, str):
            raise RuntimeError("generation manifest entry is malformed")
        if state in {"copied", "stale"}:
            if not _is_sha256(sha):
                raise RuntimeError("generation manifest file digest is malformed")
        elif size != 0 or sha:
            raise RuntimeError("generation non-file entry is malformed")
        if "error" in entry and (
            not isinstance(entry["error"], str) or not entry["error"]
        ):
            raise RuntimeError("generation manifest entry error is malformed")
        seen_paths.add(relative)
        actual_counts[state] += 1
        validated.append(entry)
    if [entry["relative_path"] for entry in validated] != sorted(seen_paths):
        raise RuntimeError("generation manifest entries are not sorted")
    if counts != actual_counts:
        raise RuntimeError("generation summary differs from manifest entries")
    if require_fresh and any(actual_counts[state] for state in _MANIFEST_STATES[1:]):
        raise RuntimeError("candidate generation is not fully fresh")
    return validated


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_migration_receipt(
    receipt: dict,
    manifest: dict,
    current: dict | None,
    expected_current_generation: str | None,
    expected_current_sha256: str | None,
) -> None:
    _validate_migration_receipt_shape(receipt, manifest)
    if current is None:
        raise RuntimeError("migration receipt requires a current generation")
    if receipt.get("expected_current_generation") != expected_current_generation:
        raise RuntimeError("migration receipt expected current generation mismatch")
    if receipt.get("expected_current_sha256") != expected_current_sha256:
        raise RuntimeError("migration receipt expected current pointer mismatch")
    if receipt.get("from_root") != current.get("canonical_root"):
        raise RuntimeError("migration receipt source root mismatch")
    current_files = current.get("files")
    if not isinstance(current_files, list):
        raise RuntimeError("migration receipt inventory is malformed")
    if receipt["old_inventory_sha256"] != _inventory_sha256(current_files):
        raise RuntimeError("migration receipt old inventory mismatch")
    old_paths = {row[0] for row in _inventory_rows(current_files)}
    new_paths = {row[0] for row in _inventory_rows(manifest["files"])}
    if receipt["old_only_paths"] != sorted(old_paths - new_paths):
        raise RuntimeError("migration receipt old-only path set mismatch")


def _validate_migration_receipt_shape(receipt: dict, manifest: dict) -> None:
    expected_keys = {
        "schema_version", "expected_current_generation", "expected_current_sha256",
        "from_root", "to_root", "old_inventory_sha256", "new_inventory_sha256",
        "old_only_paths", "old_only_paths_sha256",
    }
    if set(receipt) != expected_keys or receipt.get("schema_version") != 1:
        raise RuntimeError("migration receipt schema is malformed")
    if (
        not isinstance(receipt.get("expected_current_generation"), str)
        or not isinstance(receipt.get("from_root"), str)
        or not isinstance(receipt.get("to_root"), str)
        or receipt.get("to_root") != manifest.get("canonical_root")
    ):
        raise RuntimeError("migration receipt target root mismatch")
    for key in (
        "expected_current_sha256", "old_inventory_sha256", "new_inventory_sha256",
        "old_only_paths_sha256",
    ):
        if not _is_sha256(receipt.get(key)):
            raise RuntimeError(f"migration receipt {key} is malformed")
    old_only = receipt.get("old_only_paths")
    if (
        not isinstance(old_only, list)
        or not all(isinstance(path, str) for path in old_only)
        or old_only != sorted(set(old_only))
    ):
        raise RuntimeError("migration receipt old_only_paths is malformed")
    if receipt["old_only_paths_sha256"] != _canonical_json_sha256(old_only):
        raise RuntimeError("migration receipt old_only_paths digest mismatch")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise RuntimeError("migration receipt inventory is malformed")
    if receipt["new_inventory_sha256"] != _inventory_sha256(manifest_files):
        raise RuntimeError("migration receipt new inventory mismatch")


def _verify_candidate(
    state_root: Path,
    generation: str,
    expected_manifest_sha256: str,
    expected_candidate_sha256: str,
    wikimap_path: Path | None = None,
) -> tuple[dict, bytes, bytes]:
    snapshot_dir = resolve_within(resolve_within(state_root, "snapshots"), generation)
    manifest_payload = resolve_within(snapshot_dir, "manifest.json").read_bytes()
    if _sha256(manifest_payload) != expected_manifest_sha256:
        raise RuntimeError("candidate manifest differs from expected digest")
    descriptor_payload = resolve_within(snapshot_dir, "candidate.json").read_bytes()
    if _sha256(descriptor_payload) != expected_candidate_sha256:
        raise RuntimeError("candidate descriptor differs from expected digest")
    descriptor = json.loads(descriptor_payload)
    if not isinstance(descriptor, dict):
        raise RuntimeError("candidate descriptor is malformed")
    if descriptor.get("schema_version") != 1 or descriptor.get("generation") != generation:
        raise RuntimeError("candidate descriptor identity mismatch")
    if descriptor.get("manifest_sha256") != expected_manifest_sha256:
        raise RuntimeError("candidate descriptor manifest digest mismatch")

    manifest = json.loads(manifest_payload)
    if not isinstance(manifest, dict):
        raise RuntimeError("candidate manifest is malformed")
    if manifest.get("schema_version") != 1 or manifest.get("generation") != generation:
        raise RuntimeError("generation manifest identity mismatch")
    entries = _validated_manifest_entries(manifest, snapshot_dir, require_fresh=True)
    usable: set[str] = set()
    payloads: dict[str, bytes] = {}
    for entry in entries:
        relative = entry.get("relative_path")
        assert isinstance(relative, str)
        payload = resolve_within(snapshot_dir, relative).read_bytes()
        if len(payload) != entry.get("size") or _sha256(payload) != entry.get("sha256"):
            raise RuntimeError("candidate snapshot file integrity mismatch")
        usable.add(relative)
        payloads[relative] = payload
    actual = {
        path.relative_to(snapshot_dir).as_posix()
        for path in snapshot_dir.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    if actual != usable:
        raise RuntimeError("candidate Markdown inventory differs from manifest")

    receipt = manifest.get("migration_receipt")
    receipt_sha = manifest.get("migration_receipt_sha256")
    if receipt is not None and not isinstance(receipt, dict):
        raise RuntimeError("candidate migration receipt is malformed")
    if receipt is not None and receipt_sha != _canonical_json_sha256(receipt):
        raise RuntimeError("candidate migration receipt integrity check failed")

    authority_payload = resolve_within(snapshot_dir, "authority.json").read_bytes()
    if descriptor.get("authority_sha256") != _sha256(authority_payload):
        raise RuntimeError("candidate authority differs from staged digest")
    authority = json.loads(authority_payload)
    if not isinstance(authority, dict):
        raise RuntimeError("candidate authority is malformed")
    if authority.get("generation") != generation or authority.get("unresolved_redirects"):
        raise RuntimeError("candidate authority is not promotion-safe")
    index_path = resolve_within(snapshot_dir, ".wikimap/index.db")
    index_payload = index_path.read_bytes()
    if descriptor.get("wikimap_index_sha256") != _sha256(index_payload):
        raise RuntimeError("candidate Wikimap index differs from staged digest")
    _quick_check_index(index_path)

    runtime = Path(wikimap_path) if wikimap_path is not None else _find_wikimap_path()
    if runtime is None or not runtime.is_file():
        raise FileNotFoundError("packaged wikimap runtime not found")
    with tempfile.TemporaryDirectory(prefix="librarian-promote-verify-") as temp:
        verified_view = Path(temp) / "snapshot"
        verified_view.mkdir()
        for relative, payload in payloads.items():
            target = resolve_within(verified_view, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        rebuilt_authority, _, _ = _authority_bytes(verified_view, generation)
        if rebuilt_authority != authority_payload:
            raise RuntimeError("candidate authority differs from verified snapshot compilation")
        WikimapAdapter(runtime, verified_view).index()
        rebuilt_index = resolve_within(verified_view, ".wikimap/index.db")
        _quick_check_index(rebuilt_index)
        if _semantic_index_sha256(rebuilt_index) != _semantic_index_sha256(index_path):
            raise RuntimeError("candidate Wikimap index differs from verified snapshot rebuild")
    return manifest, authority_payload, index_payload


def _pointer_expectation(
    current_payload: bytes | None,
    expected_current_generation: str | None,
    expected_current_sha256: str | None,
) -> dict | None:
    actual_sha = _sha256(current_payload) if current_payload is not None else None
    if actual_sha != expected_current_sha256:
        raise RuntimeError("expected current pointer digest mismatch")
    current = json.loads(current_payload) if current_payload is not None else None
    if current is not None and not isinstance(current, dict):
        raise RuntimeError("current pointer is malformed")
    actual_generation = current.get("generation") if current is not None else None
    if actual_generation != expected_current_generation:
        raise RuntimeError("expected current generation mismatch")
    if current is not None:
        _validate_current_integrity(current)
    return current


def _validate_current_integrity(current: dict) -> None:
    generation = current.get("generation")
    canonical_root = current.get("canonical_root")
    if (
        current.get("schema_version") != 1
        or not isinstance(generation, str) or not generation
        or not isinstance(canonical_root, str) or not Path(canonical_root).is_absolute()
    ):
        raise RuntimeError("current pointer metadata is malformed")
    for key in ("manifest_sha256", "authority_sha256", "wikimap_index_sha256"):
        if not _is_sha256(current.get(key)):
            raise RuntimeError(f"current pointer {key} is malformed")
    if "candidate_sha256" in current and not _is_sha256(current["candidate_sha256"]):
        raise RuntimeError("current pointer candidate_sha256 is malformed")
    receipt_present = "migration_receipt" in current
    receipt_digest_present = "migration_receipt_sha256" in current
    if receipt_present != receipt_digest_present:
        raise RuntimeError("current pointer migration receipt binding is malformed")
    if receipt_present:
        receipt = current["migration_receipt"]
        receipt_digest = current["migration_receipt_sha256"]
        if not isinstance(receipt, dict) or not _is_sha256(receipt_digest):
            raise RuntimeError("current pointer migration receipt is malformed")
        if receipt_digest != _canonical_json_sha256(receipt):
            raise RuntimeError("current pointer migration receipt digest mismatch")


def _harden_generation_permissions(generation_dir: Path) -> None:
    """Make the verified tree read-only as cooperative-writer defense in depth.

    This does not claim protection from an arbitrary same-UID process: an
    owner can restore permissions. Integrity hashes still make such external
    mutation fail closed at verification or runtime readback.
    """
    directories = [generation_dir]
    for path in generation_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            directories.append(path)
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True,
    ):
        directory.chmod(0o555)


def promote_generation(
    state_root: Path,
    generation: str,
    expected_manifest_sha256: str,
    expected_candidate_sha256: str,
    expected_current_generation: str | None,
    expected_current_sha256: str | None,
    wikimap_path: Path | None = None,
) -> RefreshResult:
    """CAS-promote an already verified generation under the shared writer lock."""
    state_root = Path(state_root)
    with _writer_lock(state_root):
        current = _pointer_expectation(
            _current_bytes(state_root), expected_current_generation, expected_current_sha256,
        )
        manifest, authority_payload, index_payload = _verify_candidate(
            state_root,
            generation,
            expected_manifest_sha256,
            expected_candidate_sha256,
            wikimap_path,
        )
        receipt = manifest.get("migration_receipt")
        root_changed = (
            current is not None
            and current.get("canonical_root") != manifest.get("canonical_root")
        )
        if root_changed and receipt is None:
            raise RuntimeError("root migration candidate requires a migration receipt")
        if not root_changed and receipt is not None:
            raise RuntimeError("migration receipt is invalid when canonical root is unchanged")
        if receipt is not None:
            _validate_migration_receipt(
                receipt, manifest, current,
                expected_current_generation, expected_current_sha256,
            )
        generation_dir = resolve_within(
            resolve_within(state_root, "snapshots"), generation,
        )
        _harden_generation_permissions(generation_dir)
        sealed_manifest, sealed_authority, sealed_index = _verify_candidate(
            state_root,
            generation,
            expected_manifest_sha256,
            expected_candidate_sha256,
            wikimap_path,
        )
        if (
            sealed_manifest != manifest
            or sealed_authority != authority_payload
            or sealed_index != index_payload
        ):
            raise RuntimeError("candidate bytes changed before promotion")
        pointer = {
            **manifest,
            "manifest_sha256": expected_manifest_sha256,
            "authority_sha256": _sha256(authority_payload),
            "wikimap_index_sha256": _sha256(index_payload),
            "candidate_sha256": expected_candidate_sha256,
        }
        if current is not None:
            previous_generation = current.get("generation")
            if not isinstance(previous_generation, str) or not previous_generation:
                raise RuntimeError("current generation is malformed")
            pointer["previous_generation"] = previous_generation
            for key in ("manifest_sha256", "authority_sha256", "wikimap_index_sha256"):
                value = current.get(key)
                if not _is_sha256(value):
                    raise RuntimeError(f"current generation has malformed {key}")
                pointer[f"previous_{key}"] = value
            pointer["previous_migration_receipt_sha256"] = current.get(
                "migration_receipt_sha256",
            )
        pointer_payload = json.dumps(
            pointer, indent=2, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        _write_atomic(state_root / "current.json", pointer_payload)

    authority = json.loads(authority_payload)
    return RefreshResult(
        generation=generation,
        summary=dict(manifest.get("summary", {})),
        authority_entries=len(authority.get("entries", [])),
        unresolved_redirects=len(authority.get("unresolved_redirects", [])),
        authority_sha256=_sha256(authority_payload),
        wikimap_index_sha256=_sha256(index_payload),
        manifest_sha256=expected_manifest_sha256,
        candidate_sha256=expected_candidate_sha256,
        promoted=True,
        migration_receipt_sha256=manifest.get("migration_receipt_sha256"),
    )


def refresh_wiki(
    canonical_root: Path,
    state_root: Path,
    wikimap_path: Path,
    *,
    migration_receipt: dict | None = None,
) -> RefreshResult:
    """Compatibility wrapper: stage, verify, then CAS-promote one generation."""
    state_root = Path(state_root)
    current_payload = _current_bytes(state_root)
    expected_sha = _sha256(current_payload) if current_payload is not None else None
    expected_generation = (
        json.loads(current_payload).get("generation") if current_payload is not None else None
    )
    staged = stage_refresh(
        canonical_root, state_root, wikimap_path, migration_receipt=migration_receipt,
    )
    return promote_generation(
        state_root,
        staged.generation,
        staged.manifest_sha256,
        str(staged.candidate_sha256),
        expected_generation,
        expected_sha,
        wikimap_path,
    )


def _verified_pointer_for_generation(
    state_root: Path,
    current: dict,
    generation: str,
    wikimap_path: Path | None = None,
) -> tuple[dict, bytes]:
    if current.get("previous_generation") != generation:
        raise RuntimeError("requested rollback generation is not the trusted previous generation")
    snapshots = resolve_within(state_root, "snapshots")
    snapshot_dir = resolve_within(snapshots, generation)
    manifest_payload = resolve_within(snapshot_dir, "manifest.json").read_bytes()
    manifest = json.loads(manifest_payload)
    if not isinstance(manifest, dict):
        raise RuntimeError("generation manifest is malformed")
    if manifest.get("schema_version") != 1 or manifest.get("generation") != generation:
        raise RuntimeError("generation manifest identity mismatch")
    if current.get("previous_manifest_sha256") != _sha256(manifest_payload):
        raise RuntimeError("previous generation manifest is not integrity-bound")
    entries = _validated_manifest_entries(manifest, snapshot_dir, require_fresh=False)
    receipt = manifest.get("migration_receipt")
    bound_receipt_sha = current.get("previous_migration_receipt_sha256")
    if bound_receipt_sha is None:
        if receipt is not None:
            raise RuntimeError("rollback receipt-bearing generation is not integrity-bound")
    elif not _is_sha256(bound_receipt_sha):
        raise RuntimeError("previous migration receipt binding is malformed")
    elif receipt is None or manifest.get("migration_receipt_sha256") != bound_receipt_sha:
        raise RuntimeError("previous migration receipt binding mismatch")
    if receipt is not None:
        if not isinstance(receipt, dict):
            raise RuntimeError("generation migration receipt is malformed")
        if manifest.get("migration_receipt_sha256") != _canonical_json_sha256(receipt):
            raise RuntimeError("generation migration receipt integrity check failed")
        _validate_migration_receipt_shape(receipt, manifest)
    usable: set[str] = set()
    payloads: dict[str, bytes] = {}
    for entry in entries:
        relative = entry.get("relative_path")
        assert isinstance(relative, str)
        path = resolve_within(snapshot_dir, relative)
        if entry.get("state") in {"copied", "stale"}:
            payload = path.read_bytes()
            if len(payload) != entry.get("size") or _sha256(payload) != entry.get("sha256"):
                raise RuntimeError("snapshot file integrity mismatch")
            usable.add(relative)
            payloads[relative] = payload
    actual = {
        path.relative_to(snapshot_dir).as_posix()
        for path in snapshot_dir.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    if actual != usable:
        raise RuntimeError("snapshot Markdown inventory differs from generation manifest")
    authority_payload = resolve_within(snapshot_dir, "authority.json").read_bytes()
    if current.get("previous_authority_sha256") != _sha256(authority_payload):
        raise RuntimeError("previous generation authority is not integrity-bound")
    authority = json.loads(authority_payload)
    if not isinstance(authority, dict):
        raise RuntimeError("generation authority is malformed")
    if authority.get("generation") != generation or authority.get("unresolved_redirects"):
        raise RuntimeError("generation authority is not rollback-safe")
    index_path = resolve_within(snapshot_dir, ".wikimap/index.db")
    index_payload = index_path.read_bytes()
    if current.get("previous_wikimap_index_sha256") != _sha256(index_payload):
        raise RuntimeError("previous generation Wikimap index is not integrity-bound")
    _quick_check_index(index_path)

    runtime = Path(wikimap_path) if wikimap_path is not None else _find_wikimap_path()
    if runtime is None or not runtime.is_file():
        raise FileNotFoundError("packaged wikimap runtime not found")
    with tempfile.TemporaryDirectory(prefix="librarian-rollback-verify-") as temp:
        verified_view = Path(temp) / "snapshot"
        verified_view.mkdir()
        for relative, payload in payloads.items():
            target = resolve_within(verified_view, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        rebuilt_authority, _, _ = _authority_bytes(verified_view, generation)
        if rebuilt_authority != authority_payload:
            raise RuntimeError("generation authority differs from verified snapshot compilation")
        WikimapAdapter(runtime, verified_view).index()
        rebuilt_index = resolve_within(verified_view, ".wikimap/index.db")
        _quick_check_index(rebuilt_index)
        if _semantic_index_sha256(rebuilt_index) != _semantic_index_sha256(index_path):
            raise RuntimeError("generation Wikimap index differs from verified snapshot rebuild")

    pointer = {
        **manifest,
        "manifest_sha256": _sha256(manifest_payload),
        "authority_sha256": _sha256(authority_payload),
        "wikimap_index_sha256": _sha256(index_payload),
    }
    return pointer, authority_payload


def rollback_generation(
    state_root: Path,
    generation: str,
    *,
    expected_current_sha256: str | None = None,
) -> RefreshResult:
    """Atomically repoint to a verified prior generation under CAS serialization."""
    state_root = Path(state_root)
    with _writer_lock(state_root):
        current_payload = resolve_within(state_root, "current.json").read_bytes()
        if expected_current_sha256 is not None and _sha256(current_payload) != expected_current_sha256:
            raise RuntimeError("expected current pointer digest mismatch")
        current = json.loads(current_payload)
        if not isinstance(current, dict):
            raise RuntimeError("current pointer is malformed")
        _validate_current_integrity(current)
        previous = str(current["generation"])
        pointer, authority_payload = _verified_pointer_for_generation(
            state_root, current, generation,
        )
        generation_dir = resolve_within(
            resolve_within(state_root, "snapshots"), generation,
        )
        _harden_generation_permissions(generation_dir)
        sealed_pointer, sealed_authority = _verified_pointer_for_generation(
            state_root, current, generation,
        )
        if sealed_pointer != pointer or sealed_authority != authority_payload:
            raise RuntimeError("rollback generation bytes changed before promotion")
        if previous:
            pointer["previous_generation"] = previous
            for key in ("manifest_sha256", "authority_sha256", "wikimap_index_sha256"):
                value = current.get(key)
                if not _is_sha256(value):
                    raise RuntimeError(f"current generation has malformed {key}")
                pointer[f"previous_{key}"] = value
            pointer["previous_migration_receipt_sha256"] = current.get(
                "migration_receipt_sha256",
            )
        pointer_payload = json.dumps(
            pointer, indent=2, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        _write_atomic(state_root / "current.json", pointer_payload)

    authority = json.loads(authority_payload)
    return RefreshResult(
        generation=generation,
        summary=dict(pointer.get("summary", {})),
        authority_entries=len(authority.get("entries", [])),
        unresolved_redirects=len(authority.get("unresolved_redirects", [])),
        authority_sha256=str(pointer["authority_sha256"]),
        wikimap_index_sha256=str(pointer["wikimap_index_sha256"]),
        manifest_sha256=str(pointer["manifest_sha256"]),
        candidate_sha256=pointer.get("candidate_sha256"),
        promoted=True,
        migration_receipt_sha256=pointer.get("migration_receipt_sha256"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain derived Wiki Librarian state")
    parser.add_argument("--wiki-root", type=Path)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--wikimap-path", type=Path)
    parser.add_argument("--migration-receipt", type=Path)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--stage-only", action="store_true")
    operation.add_argument("--promote-generation")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-candidate-sha256")
    parser.add_argument("--expected-current-generation")
    parser.add_argument("--expected-current-sha256")
    operation.add_argument("--rollback-generation")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.rollback_generation:
            if any((args.wiki_root, args.wikimap_path, args.migration_receipt,
                    args.expected_manifest_sha256, args.expected_candidate_sha256,
                    args.expected_current_generation)):
                raise ValueError("rollback received mode-inapplicable options")
            result = rollback_generation(
                args.state_root,
                args.rollback_generation,
                expected_current_sha256=args.expected_current_sha256,
            )
        elif args.promote_generation:
            if args.wiki_root or args.migration_receipt:
                raise ValueError("promotion received mode-inapplicable options")
            if not args.expected_manifest_sha256 or not args.expected_candidate_sha256:
                raise ValueError("promotion requires expected manifest and candidate digests")
            result = promote_generation(
                args.state_root,
                args.promote_generation,
                args.expected_manifest_sha256,
                args.expected_candidate_sha256,
                args.expected_current_generation,
                args.expected_current_sha256,
                args.wikimap_path,
            )
        else:
            if any((args.expected_manifest_sha256, args.expected_candidate_sha256,
                    args.expected_current_generation, args.expected_current_sha256)):
                raise ValueError("refresh received mode-inapplicable expected values")
            if args.wiki_root is None:
                raise ValueError("--wiki-root is required for refresh")
            wikimap_path = args.wikimap_path or _find_wikimap_path()
            if wikimap_path is None:
                raise FileNotFoundError("packaged wikimap runtime not found")
            receipt = None
            if args.migration_receipt is not None:
                receipt = json.loads(args.migration_receipt.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict):
                    raise ValueError("migration receipt must be a JSON object")
            if args.stage_only:
                result = stage_refresh(
                    args.wiki_root,
                    args.state_root,
                    wikimap_path,
                    migration_receipt=receipt,
                )
            else:
                result = refresh_wiki(
                    args.wiki_root,
                    args.state_root,
                    wikimap_path,
                    migration_receipt=receipt,
                )
        data = result.to_dict()
    except (AttributeError, FileNotFoundError, KeyError, OSError, RuntimeError,
            TypeError, ValueError) as exc:
        data = {
            "schema_version": 1,
            "status": "error",
            "error": "wiki maintenance failed",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if data["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
