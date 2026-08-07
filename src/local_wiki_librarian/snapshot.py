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

        # Load previous manifest for stale/deleted detection
        prev_manifest = _load_previous_manifest(state_root)
        prev_files = _previous_file_map(prev_manifest)
        prev_generation = prev_manifest.get("generation") if prev_manifest else None

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
                dest.write_bytes(raw)
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
                    dest.write_bytes(stale_bytes)
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
