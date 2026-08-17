"""Read-only atomic wiki snapshot with retry, stale fallback, and quarantine.

This module copies ``*.md`` files from a canonical Markdown knowledge base root into an
external state directory. It never writes to the canonical root and never
creates ``MAP.md`` or ``.wikimap`` artifacts.

Key safety properties:
- Each snapshot generation is staged in a unique subdirectory, then atomically
  promoted via ``current.json`` pointer swap.
- Per-file bounded retry on read errors; last-known-good bytes are preserved as
  ``stale`` rather than overwritten with empty content.
- Unreadable files with no prior copy are ``quarantined``; empty files are never
  created to substitute for read failures.
- A single ``fcntl.flock`` writer lock prevents concurrent snapshot corruption.
- Promotion is refused when zero usable Markdown files exist (fail-closed).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .paths import UnsafePathError, validate_disjoint_roots

__all__ = [
    "FileState",
    "FileEntry",
    "SnapshotResult",
    "SnapshotError",
    "build_snapshot",
]

_LOCK_FILENAME = ".librarian-snapshot.lock"
_CURRENT_FILENAME = "current.json"
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 0.0
_CHUNK_SIZE = 1024 * 1024


class SnapshotError(RuntimeError):
    """Raised when a snapshot operation fails."""


@dataclass(frozen=True)
class FileState:
    """Metadata for one file within a snapshot generation."""

    relative_path: str
    sha256: str
    size: int
    state: str  # copied | stale | quarantined | deleted
    error: str | None = None


@dataclass(frozen=True)
class FileEntry:
    """A resolved Markdown file discovered in the canonical root."""

    relative_path: str
    absolute_path: Path


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of a single snapshot build."""

    generation: str
    manifest: dict
    summary: dict


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_fsync(path: Path, payload: bytes) -> None:
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


def _fsync_generation(generation_dir: Path, snapshots_dir: Path) -> None:
    """Make every candidate byte and directory entry durable before exposure."""
    for path in sorted(generation_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    directories = [path for path in generation_dir.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(generation_dir)
    _fsync_directory(snapshots_dir)


def _is_symlink(path: Path) -> bool:
    """Check symlink without following."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def discover_markdown(canonical_root: Path) -> list[FileEntry]:
    """Discover all ``*.md`` files (including hidden) under ``canonical_root``.

    Symlinks are skipped and reported as quarantine candidates by the caller.
    """
    entries: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(canonical_root, followlinks=False):
        # Skip symlinked directories
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_symlink(Path(dirpath) / d)
        )
        for filename in sorted(filenames):
            if not filename.endswith(".md"):
                continue
            abs_path = Path(dirpath) / filename
            if _is_symlink(abs_path):
                continue
            rel = abs_path.relative_to(canonical_root)
            # Reject any path containing .. (should not happen with os.walk but guard)
            if ".." in rel.parts:
                continue
            entries.append(
                FileEntry(
                    relative_path=str(rel),
                    absolute_path=abs_path,
                )
            )
    return entries


def _read_with_retry(
    path: Path, max_retries: int = _MAX_RETRIES, delay: float = _RETRY_DELAY_SECONDS
) -> bytes | None:
    """Read file bytes with bounded retry. Returns ``None`` on persistent failure."""
    for attempt in range(max_retries):
        try:
            return path.read_bytes()
        except OSError:
            if delay > 0 and attempt < max_retries - 1:
                time.sleep(delay)
    return None


def _load_previous_manifest(state_root: Path) -> dict | None:
    """Load the current.json manifest if it exists."""
    current_path = state_root / _CURRENT_FILENAME
    if not current_path.is_file():
        return None
    try:
        data = json.loads(current_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "files" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _previous_file_map(manifest: dict | None) -> dict[str, dict]:
    """Build {relative_path: file_state_dict} from previous manifest."""
    if not manifest or not isinstance(manifest.get("files"), list):
        return {}
    result: dict[str, dict] = {}
    for entry in manifest["files"]:
        if isinstance(entry, dict) and "relative_path" in entry:
            result[entry["relative_path"]] = entry
    return result


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _inventory_rows(entries: list[dict] | list[FileState]) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for entry in entries:
        if isinstance(entry, FileState):
            relative, size, sha, state = (
                entry.relative_path, entry.size, entry.sha256, entry.state,
            )
        else:
            relative = entry.get("relative_path")
            size = entry.get("size")
            sha = entry.get("sha256")
            state = entry.get("state")
        if state not in {"copied", "stale"}:
            continue
        if not isinstance(relative, str) or not isinstance(size, int) or not isinstance(sha, str):
            raise SnapshotError("snapshot inventory entry is malformed")
        rows.append((relative, size, sha))
    return sorted(rows)


def _inventory_sha256(entries: list[dict] | list[FileState]) -> str:
    return _canonical_json_sha256(_inventory_rows(entries))


def _previous_file_bytes(state_root: Path, generation: str, relative_path: str) -> bytes | None:
    """Read bytes of a file from a previous generation's snapshot."""
    prev_path = state_root / "snapshots" / generation / relative_path
    if not prev_path.is_file():
        return None
    try:
        return prev_path.read_bytes()
    except OSError:
        return None


def _generation_name() -> str:
    """Generate a unique generation identifier."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}Z-{os.getpid()}-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}"


def build_snapshot(
    canonical_root: Path,
    state_root: Path,
    max_retries: int = _MAX_RETRIES,
    retry_delay: float = _RETRY_DELAY_SECONDS,
    prepare_generation: Callable[[Path, dict], dict] | None = None,
    *,
    promote: bool = True,
    migration_receipt: dict | None = None,
) -> SnapshotResult:
    """Build an atomic read-only snapshot of all Markdown files.

    Args:
        canonical_root: The Markdown knowledge base root (read-only source).
        state_root: External state directory for snapshot storage.
        max_retries: Per-file read retry attempts.
        retry_delay: Seconds to wait between retries.
        prepare_generation: Optional callback executed under the writer lock
            after immutable Markdown/manifest creation and before pointer
            promotion. It may create verified generation-local sidecars and
            return integrity fields to add to ``current.json``.

    Returns:
        SnapshotResult with generation name, manifest, and summary.

    Raises:
        SnapshotError: On lock contention, root traversal failure, or
            fail-closed (zero usable Markdown).
    """
    try:
        canonical_root, state_root = validate_disjoint_roots(
            canonical_root, state_root
        )
    except UnsafePathError as exc:
        raise SnapshotError(f"unsafe snapshot roots: {exc}") from exc

    if not canonical_root.is_dir():
        raise SnapshotError(f"canonical root does not exist: {canonical_root}")

    # Ensure state root exists (but not under canonical)
    state_root.mkdir(parents=True, exist_ok=True)

    snapshots_dir = state_root / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)

    # Acquire single-writer lock
    lock_path = state_root / _LOCK_FILENAME
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise SnapshotError(f"cannot create lock file: {exc}") from exc

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotError(
                "another snapshot writer is active (lock contention)"
            ) from exc

        # Discover files
        entries = discover_markdown(canonical_root)

        # Load previous manifest for stale/deleted detection. A root migration
        # is a new baseline: previous bytes must never silently cross roots.
        prev_manifest = _load_previous_manifest(state_root)
        prev_files = _previous_file_map(prev_manifest)
        prev_generation = prev_manifest.get("generation") if prev_manifest else None
        migration_receipt_sha256: str | None = None
        if prev_manifest is not None:
            previous_root = Path(str(prev_manifest.get("canonical_root", ""))).resolve()
            root_changed = previous_root != canonical_root
            if root_changed:
                if not isinstance(migration_receipt, dict):
                    raise SnapshotError("root migration receipt is required before staging")
                expected_receipt_keys = {
                    "schema_version",
                    "expected_current_generation",
                    "expected_current_sha256",
                    "from_root",
                    "to_root",
                    "old_inventory_sha256",
                    "new_inventory_sha256",
                    "old_only_paths",
                    "old_only_paths_sha256",
                }
                if set(migration_receipt) != expected_receipt_keys:
                    raise SnapshotError("root migration receipt schema mismatch")
                current_payload = (state_root / _CURRENT_FILENAME).read_bytes()
                expected = {
                    "schema_version": 1,
                    "expected_current_generation": prev_generation,
                    "expected_current_sha256": _sha256_bytes(current_payload),
                    "from_root": str(previous_root),
                    "to_root": str(canonical_root),
                    "old_inventory_sha256": _inventory_sha256(list(prev_files.values())),
                }
                for key, value in expected.items():
                    if migration_receipt.get(key) != value:
                        raise SnapshotError(f"root migration receipt {key} mismatch")
                old_only = migration_receipt.get("old_only_paths")
                if not isinstance(old_only, list) or not all(isinstance(path, str) for path in old_only):
                    raise SnapshotError("root migration receipt old_only_paths is malformed")
                if old_only != sorted(set(old_only)):
                    raise SnapshotError("root migration receipt old_only_paths must be exact and sorted")
                if migration_receipt.get("old_only_paths_sha256") != _canonical_json_sha256(old_only):
                    raise SnapshotError("root migration receipt old_only_paths digest mismatch")
                migration_receipt_sha256 = _canonical_json_sha256(migration_receipt)
                prev_manifest = None
                prev_files = {}
                prev_generation = None
            elif migration_receipt is not None:
                raise SnapshotError("root migration receipt is invalid when canonical root is unchanged")

        generation = _generation_name()
        gen_dir = snapshots_dir / generation
        gen_dir.mkdir(parents=True, exist_ok=False)

        file_states: list[FileState] = []
        counts = {"copied": 0, "stale": 0, "quarantined": 0, "deleted": 0}

        seen_paths: set[str] = set()

        for entry in entries:
            seen_paths.add(entry.relative_path)
            dest = gen_dir / entry.relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)

            raw = _read_with_retry(entry.absolute_path, max_retries, retry_delay)
            if raw is not None:
                _write_bytes_fsync(dest, raw)
                sha = _sha256_bytes(raw)
                file_states.append(FileState(
                    relative_path=entry.relative_path,
                    sha256=sha,
                    size=len(raw),
                    state="copied",
                ))
                counts["copied"] += 1
            else:
                # Read failed — try last-known-good from previous generation
                stale_bytes = None
                if prev_generation:
                    stale_bytes = _previous_file_bytes(state_root, prev_generation, entry.relative_path)
                if stale_bytes is not None:
                    _write_bytes_fsync(dest, stale_bytes)
                    sha = _sha256_bytes(stale_bytes)
                    file_states.append(FileState(
                        relative_path=entry.relative_path,
                        sha256=sha,
                        size=len(stale_bytes),
                        state="stale",
                        error="read_failed_using_last_known_good",
                    ))
                    counts["stale"] += 1
                else:
                    # No prior copy — quarantine (do NOT create empty file)
                    file_states.append(FileState(
                        relative_path=entry.relative_path,
                        sha256="",
                        size=0,
                        state="quarantined",
                        error="read_failed_no_prior_copy",
                    ))
                    counts["quarantined"] += 1

        if migration_receipt is not None:
            try:
                new_inventory_sha256 = _inventory_sha256(file_states)
                if migration_receipt.get("new_inventory_sha256") != new_inventory_sha256:
                    raise SnapshotError("root migration receipt new inventory mismatch")
                old_paths = {
                    row[0] for row in _inventory_rows(
                        list(_previous_file_map(_load_previous_manifest(state_root)).values())
                    )
                }
                new_paths = {row[0] for row in _inventory_rows(file_states)}
                actual_old_only = sorted(old_paths - new_paths)
                if migration_receipt.get("old_only_paths") != actual_old_only:
                    raise SnapshotError("root migration receipt old-only path set mismatch")
            except Exception:
                import shutil
                shutil.rmtree(gen_dir, ignore_errors=True)
                raise

        # Detect deletions: files in previous manifest but not seen now
        for prev_rel, prev_info in prev_files.items():
            if prev_rel not in seen_paths:
                # Verify with lstat that it's genuinely gone
                prev_abs = canonical_root / prev_rel
                try:
                    prev_abs.lstat()
                    # File still exists (maybe race?) — skip, don't mark deleted
                    continue
                except FileNotFoundError:
                    file_states.append(FileState(
                        relative_path=prev_rel,
                        sha256="",
                        size=0,
                        state="deleted",
                    ))
                    counts["deleted"] += 1
                except OSError:
                    # Permission denied or other error — can't confirm deletion,
                    # preserve old copy rather than risk false deletion.
                    continue

        # Fail-closed: refuse promotion if zero usable Markdown
        usable = counts["copied"] + counts["stale"]
        if usable == 0:
            # Clean up the unusable generation
            import shutil
            shutil.rmtree(gen_dir, ignore_errors=True)
            raise SnapshotError(
                "fail-closed: zero usable Markdown files in this generation "
                f"(quarantined={counts['quarantined']})"
            )

        # Build manifest
        manifest = {
            "schema_version": 1,
            "generation": generation,
            "canonical_root": str(canonical_root),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "file_count": len(file_states),
            "files": [
                {
                    "relative_path": fs.relative_path,
                    "sha256": fs.sha256,
                    "size": fs.size,
                    "state": fs.state,
                    **({"error": fs.error} if fs.error else {}),
                }
                for fs in sorted(file_states, key=lambda f: f.relative_path)
            ],
            "summary": dict(counts),
            **({
                "migration_receipt": migration_receipt,
                "migration_receipt_sha256": migration_receipt_sha256,
            } if migration_receipt is not None else {}),
        }

        # Write and fsync the immutable generation manifest before promotion.
        manifest_path = gen_dir / "manifest.json"
        manifest_bytes = json.dumps(
            manifest, indent=2, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        with manifest_path.open("wb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        pointer_metadata: dict = {}
        if prepare_generation is not None:
            try:
                pointer_metadata = prepare_generation(gen_dir, manifest) or {}
                if not isinstance(pointer_metadata, dict):
                    raise TypeError("prepare_generation must return a mapping")
                collisions = set(pointer_metadata).intersection(manifest)
                if collisions:
                    raise ValueError(
                        "prepare_generation cannot replace manifest fields: "
                        + ", ".join(sorted(collisions))
                    )
            except Exception as exc:
                import shutil
                shutil.rmtree(gen_dir, ignore_errors=True)
                raise SnapshotError(
                    f"generation preparation failed: {type(exc).__name__}"
                ) from exc

        # A pointer may only reference a generation whose complete file tree
        # and parent directory entry have reached durable storage.
        _fsync_generation(gen_dir, snapshots_dir)

        if not promote:
            return SnapshotResult(
                generation=generation,
                manifest=manifest,
                summary=dict(counts),
            )

        # Atomic promotion via os.replace.  Keep the historical full-manifest
        # current.json shape for compatibility, and add an integrity pointer to
        # the immutable generation manifest.
        previous_integrity = {}
        if prev_generation and prev_manifest is not None:
            for key in ("manifest_sha256", "authority_sha256", "wikimap_index_sha256"):
                value = prev_manifest.get(key)
                if value is not None:
                    previous_integrity[f"previous_{key}"] = value
        current_payload = {
            **manifest,
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            **({"previous_generation": prev_generation} if prev_generation else {}),
            **previous_integrity,
            **pointer_metadata,
        }
        current_path = state_root / _CURRENT_FILENAME
        tmp_current = state_root / f".current.{generation}.tmp"
        current_bytes = json.dumps(
            current_payload, indent=2, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        with tmp_current.open("wb") as handle:
            handle.write(current_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_current, current_path)
        _fsync_directory(state_root)
        if current_path.read_bytes() != current_bytes:
            raise SnapshotError("current pointer readback failed")

        return SnapshotResult(
            generation=generation,
            manifest=manifest,
            summary=dict(counts),
        )

    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
