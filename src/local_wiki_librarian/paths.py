from __future__ import annotations

import errno
import os
import stat
import tempfile
from contextlib import contextmanager
import posixpath
import re
from pathlib import Path
from typing import Iterator, Union


PathLike = Union[str, Path]


def parse_retrieval_path(value: str) -> tuple[str | None, str]:
    """Return a normalized relative retrieval path and reject unsafe inputs."""
    raw = value.strip().replace("\\", "/")
    if "\x00" in raw or "://" in raw:
        return None, ""
    collection: str | None = None
    raw = raw.split("?", 1)[0]
    if ".." in raw.strip().strip("/").split("/"):
        return collection, ""
    path = posixpath.normpath("/" + raw.strip().strip("/")).lstrip("/")
    if path in {"", "."} or path.startswith("../"):
        return collection, ""
    return collection, path


def retrieval_alias_key(value: str) -> str:
    """Build a slug-tolerant identity key; callers must reject collisions."""
    _, path = parse_retrieval_path(value)
    return "/".join(re.sub(r"[-_]+", "-", part).casefold() for part in path.split("/"))


def unique_retrieval_aliases(paths: list[str]) -> dict[str, str]:
    """Map alias keys only when exactly one canonical path owns the key."""
    grouped: dict[str, set[str]] = {}
    for value in paths:
        _, path = parse_retrieval_path(value)
        if path:
            grouped.setdefault(retrieval_alias_key(path), set()).add(path)
    return {key: next(iter(values)) for key, values in grouped.items() if len(values) == 1}


class UnsafePathError(ValueError):
    """Raised when a retrieval path crosses an owned root boundary."""


def _resolved_root(path: PathLike) -> Path:
    root = Path(path).expanduser()
    if root.is_symlink():
        raise UnsafePathError(f"root must not be a symlink: {root}")
    return root.resolve(strict=False)


def resolve_within(root: PathLike, relative: PathLike) -> Path:
    """Resolve a relative path while rejecting absolute, traversal, and symlink escapes."""
    root_path = _resolved_root(root)
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise UnsafePathError(f"absolute paths are not allowed: {relative_path}")
    if ".." in relative_path.parts:
        raise UnsafePathError(f"parent traversal is not allowed: {relative_path}")

    current = root_path
    for part in relative_path.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"symlink path components are not allowed: {current}")

    candidate = root_path / relative_path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes owned root: {relative_path}") from exc
    return candidate


def validate_disjoint_roots(first: PathLike, second: PathLike) -> tuple[Path, Path]:
    """Return normalized roots after proving neither contains the other."""
    first_root = _resolved_root(first)
    second_root = _resolved_root(second)
    if (
        first_root == second_root
        or first_root in second_root.parents
        or second_root in first_root.parents
    ):
        raise UnsafePathError(
            f"retrieval roots must not overlap: {first_root} <-> {second_root}"
        )
    # ``resolve()`` can preserve caller-provided casing on case-insensitive
    # filesystems.  Compare every existing candidate ancestor by stable
    # filesystem identity so an alternate-case spelling cannot place state
    # below the canonical source while evading the lexical check above.
    def physically_contains(existing_root: Path, candidate: Path) -> bool:
        if not existing_root.exists():
            return False
        current = candidate
        while True:
            if current.exists():
                try:
                    if existing_root.samefile(current):
                        return True
                except OSError:
                    pass
            if current == current.parent:
                return False
            current = current.parent

    if physically_contains(first_root, second_root) or physically_contains(
        second_root, first_root
    ):
        raise UnsafePathError(
            f"retrieval roots must not overlap physically: {first_root} <-> {second_root}"
        )
    return first_root, second_root


@contextmanager
def _directory_descriptor(root: PathLike, relative: PathLike = ".") -> Iterator[int]:
    """Walk directory components without following replacements or links."""
    resolve_within(root, relative)
    descriptor = None
    try:
        root_path = Path(root).expanduser().absolute()
        descriptor = os.open(root_path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in root_path.parts[1:] + Path(relative).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafePathError(f"unsafe directory component: {relative}") from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def open_regular_within(root: PathLike, relative: PathLike, flags: int = os.O_RDONLY) -> int:
    """Return an owned nonblocking regular-file descriptor; caller closes it."""
    resolve_within(root, relative)
    path = Path(relative)
    with _directory_descriptor(root, path.parent) as parent:
        try:
            descriptor = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=parent)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise UnsafePathError(f"unsafe file: {relative}") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnsafePathError(f"not a regular file: {relative}")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


def read_bytes_within(root: PathLike, relative: PathLike) -> bytes:
    """Read only the regular file opened beneath the supplied directory root."""
    with os.fdopen(open_regular_within(root, relative), "rb") as handle:
        return handle.read()


def write_bytes_atomic(root: PathLike, relative: PathLike, payload: bytes) -> None:
    """Durably replace one owned file using a unique descriptor-backed temporary."""
    target = resolve_within(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _directory_descriptor(root, Path(relative).parent) as parent:
        try:
            existing = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafePathError(f"not a regular destination: {relative}")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(Path(temporary).name, target.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            if read_bytes_within(root, relative) != payload:
                raise RuntimeError(f"atomic write readback failed: {relative}")
        finally:
            try:
                os.unlink(Path(temporary).name, dir_fd=parent)
            except FileNotFoundError:
                pass
