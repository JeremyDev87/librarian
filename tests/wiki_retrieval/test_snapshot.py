"""Tests for read-only atomic wiki snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.snapshot import (  # noqa: E402
    SnapshotError,
    build_snapshot,
    discover_markdown,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_wiki(root: Path, files: dict[str, str]) -> None:
    """Create a wiki fixture with {relative_path: content}."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _read_current(state_root: Path) -> dict:
    current = state_root / "current.json"
    assert current.is_file(), "current.json should exist after successful snapshot"
    return json.loads(current.read_text(encoding="utf-8"))


def _source_state(root):
    return [
        (str(path.relative_to(root)), path.lstat().st_mode, path.lstat().st_mtime_ns,
         os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
    ]


# ---------------------------------------------------------------------------
# discover_markdown
# ---------------------------------------------------------------------------

class TestDiscoverMarkdown:
    def test_finds_markdown_files(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _make_wiki(wiki, {
            "page1.md": "# Page 1",
            "sub/page2.md": "# Page 2",
            "not_md.txt": "ignore me",
        })
        entries = discover_markdown(wiki)
        paths = sorted(e.relative_path for e in entries)
        assert paths == ["page1.md", "sub/page2.md"]

    def test_includes_hidden_markdown(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _make_wiki(wiki, {
            ".hidden.md": "# Hidden",
            "visible.md": "# Visible",
        })
        entries = discover_markdown(wiki)
        paths = sorted(e.relative_path for e in entries)
        assert ".hidden.md" in paths
        assert "visible.md" in paths

    def test_excludes_non_markdown(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _make_wiki(wiki, {
            "page.md": "# Page",
            "image.png": "binary",
            "data.json": "{}",
            "MAP.md": "should be included as .md",
        })
        entries = discover_markdown(wiki)
        paths = sorted(e.relative_path for e in entries)
        assert "image.png" not in paths
        assert "data.json" not in paths
        assert "MAP.md" in paths  # .md extension, so included

    def test_skips_symlinks(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _make_wiki(wiki, {"real.md": "# Real"})
        # Create a symlink to a file outside wiki
        symlink_target = tmp_path / "outside.md"
        symlink_target.write_text("# Outside", encoding="utf-8")
        link_path = wiki / "link.md"
        os.symlink(symlink_target, link_path)
        entries = discover_markdown(wiki)
        paths = [e.relative_path for e in entries]
        assert "real.md" in paths
        assert "link.md" not in paths


# ---------------------------------------------------------------------------
# build_snapshot — basic
# ---------------------------------------------------------------------------

class TestBuildSnapshotBasic:
    def test_rejects_state_root_inside_canonical_before_writing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _make_wiki(wiki, {"page.md": "# Page"})
        nested_state = wiki / ".librarian-state"

        with pytest.raises(SnapshotError, match="must not overlap"):
            build_snapshot(wiki, nested_state)

        assert not nested_state.exists()

    def test_copies_all_markdown(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {
            "page1.md": "# Page 1\ncontent",
            "sub/page2.md": "# Page 2\nmore content",
        })
        result = build_snapshot(wiki, state)
        assert result.summary["copied"] == 2
        assert result.summary["stale"] == 0
        assert result.summary["quarantined"] == 0
        assert result.summary["deleted"] == 0

    def test_creates_current_json(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Page"})
        build_snapshot(wiki, state)
        current = _read_current(state)
        assert current["schema_version"] == 1
        assert current["file_count"] == 1
        assert current["files"][0]["state"] == "copied"
        generation_manifest = state / "snapshots" / current["generation"] / "manifest.json"
        assert current["manifest_sha256"] == _sha256(generation_manifest.read_bytes())

    def test_sha256_matches_source(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        content = "# Hash Test\n한글 내용"
        _make_wiki(wiki, {"page.md": content})
        result = build_snapshot(wiki, state)
        expected_sha = _sha256(content.encode("utf-8"))
        file_entry = result.manifest["files"][0]
        assert file_entry["sha256"] == expected_sha

    def test_canonical_bytes_unchanged(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        original_content = "# Immutable\nsource"
        _make_wiki(wiki, {"page.md": original_content})
        build_snapshot(wiki, state)
        # Verify canonical wasn't modified
        assert (wiki / "page.md").read_text(encoding="utf-8") == original_content

    def test_no_map_md_created(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Page"})
        build_snapshot(wiki, state)
        assert not (wiki / "MAP.md").exists()
        assert not (wiki / ".wikimap").exists()
        assert not (state / "MAP.md").exists()

    def test_nested_directories(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {
            "a/b/c/deep.md": "# Deep",
            "x/y.md": "# Y",
        })
        result = build_snapshot(wiki, state)
        assert result.summary["copied"] == 2


# ---------------------------------------------------------------------------
# build_snapshot — retry and stale
# ---------------------------------------------------------------------------

class TestRetryAndStale:
    def test_stale_uses_last_known_good(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Original"})
        build_snapshot(wiki, state)

        # Simulate unreadable file by removing read permission
        page = wiki / "page.md"
        os.chmod(page, 0o000)
        try:
            result = build_snapshot(wiki, state)
            assert result.summary["stale"] == 1
            assert result.summary["copied"] == 0
        finally:
            os.chmod(page, 0o644)

    def test_quarantine_when_no_prior_copy(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        # One readable + one unreadable: snapshot succeeds but unreadable is quarantined
        _make_wiki(wiki, {"good.md": "# Good", "bad.md": "# Bad"})
        bad = wiki / "bad.md"
        os.chmod(bad, 0o000)
        try:
            result = build_snapshot(wiki, state)
            assert result.summary["quarantined"] == 1
            assert result.summary["copied"] == 1
        finally:
            os.chmod(bad, 0o644)


# ---------------------------------------------------------------------------
# build_snapshot — deletion detection
# ---------------------------------------------------------------------------

class TestDeletionDetection:
    def test_deleted_file_detected(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"keep.md": "# Keep", "delete.md": "# Delete"})
        build_snapshot(wiki, state)

        (wiki / "delete.md").unlink()
        result = build_snapshot(wiki, state)
        assert result.summary["deleted"] == 1
        assert result.summary["copied"] == 1

    def test_existing_file_not_marked_deleted(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"a.md": "# A", "b.md": "# B"})
        result = build_snapshot(wiki, state)
        assert result.summary["deleted"] == 0


# ---------------------------------------------------------------------------
# build_snapshot — fail-closed
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_refuses_zero_usable_promotion(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Original"})
        os.chmod(wiki / "page.md", 0o000)
        os.chmod(wiki, 0o000)  # Also block directory traversal
        try:
            with pytest.raises(SnapshotError, match="fail-closed"):
                build_snapshot(wiki, state)
        finally:
            os.chmod(wiki, 0o755)
            os.chmod(wiki / "page.md", 0o644)

    def test_all_quarantine_does_not_promote(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Original"})
        os.chmod(wiki / "page.md", 0o000)
        try:
            with pytest.raises(SnapshotError, match="fail-closed"):
                build_snapshot(wiki, state)
        finally:
            os.chmod(wiki / "page.md", 0o644)


# ---------------------------------------------------------------------------
# build_snapshot — atomic promotion
# ---------------------------------------------------------------------------

class TestAtomicPromotion:
    def test_current_json_survives_second_snapshot(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# V1"})
        r1 = build_snapshot(wiki, state)

        (wiki / "page.md").write_text("# V2", encoding="utf-8")
        r2 = build_snapshot(wiki, state)

        assert r1.generation != r2.generation
        current = _read_current(state)
        assert current["generation"] == r2.generation

    def test_generation_directories_exist(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Page"})
        result = build_snapshot(wiki, state)
        gen_dir = state / "snapshots" / result.generation
        assert gen_dir.is_dir()
        assert (gen_dir / "page.md").is_file()
        assert (gen_dir / "manifest.json").is_file()

    def test_failed_snapshot_preserves_current(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Original"})
        r1 = build_snapshot(wiki, state)
        _read_current(state)

        # Make all files unreadable to trigger fail-closed
        os.chmod(wiki / "page.md", 0o000)
        os.chmod(wiki, 0o000)
        try:
            with pytest.raises(SnapshotError):
                build_snapshot(wiki, state)
        finally:
            os.chmod(wiki, 0o755)
            os.chmod(wiki / "page.md", 0o644)

        # current.json should still point to the first generation
        preserved_current = _read_current(state)
        assert preserved_current["generation"] == r1.generation


# ---------------------------------------------------------------------------
# build_snapshot — concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_writer_fails_fast(self, tmp_path: Path) -> None:
        import fcntl
        wiki = tmp_path / "wiki"
        state = tmp_path / "state"
        _make_wiki(wiki, {"page.md": "# Page"})
        state.mkdir(parents=True, exist_ok=True)

        # Acquire lock manually
        lock_path = state / ".librarian-snapshot.lock"
        lock_path.touch()
        fd = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with pytest.raises(SnapshotError, match="lock contention"):
                build_snapshot(wiki, state)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@pytest.mark.parametrize("existing", [False, True])
def test_boundary_discovery_error_refuses_partial_snapshot(tmp_path, monkeypatch, existing):
    import local_wiki_librarian.snapshot as snapshot
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _make_wiki(wiki, {"good.md": "# Good", "blocked/page.md": "# Hidden"})
    if existing:
        build_snapshot(wiki, state)
    before = (state / "current.json").read_bytes() if existing else None
    real_walk = os.walk

    def incomplete_walk(root, **kwargs):
        yield str(root), [], ["good.md"]
        callback = kwargs.get("onerror")
        if callback:
            callback(PermissionError("injected unreadable subtree"))

    monkeypatch.setattr(snapshot.os, "walk", incomplete_walk)
    with pytest.raises(SnapshotError):
        build_snapshot(wiki, state)
    monkeypatch.setattr(snapshot.os, "walk", real_walk)
    assert ((state / "current.json").read_bytes() if existing else None) == before
    assert existing or not (state / "current.json").exists()


@pytest.mark.parametrize("replacement", ["leaf", "parent"])
def test_boundary_source_replaced_after_discovery(tmp_path, monkeypatch, replacement):
    import local_wiki_librarian.snapshot as snapshot
    wiki, state, outside = tmp_path / "wiki", tmp_path / "state", tmp_path / "outside"
    _make_wiki(wiki, {"dir/page.md": "# Original", "good.md": "# Good"})
    _make_wiki(outside, {"page.md": "EXTERNAL SENTINEL"})
    build_snapshot(wiki, state)
    before = (state / "current.json").read_bytes()
    original = snapshot.discover_markdown
    source_after_injection = []
    outside_before = _source_state(outside)

    def swap(root):
        entries = original(root)
        if replacement == "leaf":
            (wiki / "dir/page.md").unlink()
            (wiki / "dir/page.md").symlink_to(outside / "page.md")
        else:
            (wiki / "dir").rename(wiki / "saved")
            (wiki / "dir").symlink_to(outside, target_is_directory=True)
        source_after_injection.extend(_source_state(wiki))
        return entries

    monkeypatch.setattr(snapshot, "discover_markdown", swap)
    with pytest.raises(SnapshotError):
        build_snapshot(wiki, state)
    assert (state / "current.json").read_bytes() == before
    assert _source_state(wiki) == source_after_injection
    assert _source_state(outside) == outside_before
    assert (outside / "page.md").read_bytes() == b"EXTERNAL SENTINEL"
    assert all(b"EXTERNAL SENTINEL" not in p.read_bytes() for p in (state / "snapshots").rglob("*.md"))


@pytest.mark.parametrize("name", ["snapshots", "current.json", ".librarian-snapshot.lock"])
def test_boundary_state_symlink_preserves_external_target(tmp_path, name):
    wiki, state, outside = tmp_path / "wiki", tmp_path / "state", tmp_path / "outside"
    _make_wiki(wiki, {"page.md": "# Page"})
    state.mkdir()
    if name == "snapshots":
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"SENTINEL")
    else:
        outside.write_bytes(b"SENTINEL")
    before = outside.stat()
    (state / name).symlink_to(outside)
    with pytest.raises(SnapshotError):
        build_snapshot(wiki, state)
    after = outside.stat()
    assert (before.st_mode, before.st_mtime_ns) == (after.st_mode, after.st_mtime_ns)
    assert (outside / "sentinel" if name == "snapshots" else outside).read_bytes() == b"SENTINEL"


@pytest.mark.parametrize("failure", ["exists", "permission"])
def test_boundary_omitted_previous_file_is_not_deletion(tmp_path, monkeypatch, failure):
    import local_wiki_librarian.snapshot as snapshot
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _make_wiki(wiki, {"a.md": "# A", "b.md": "# B"})
    build_snapshot(wiki, state)
    before = (state / "current.json").read_bytes()
    original = snapshot.discover_markdown
    monkeypatch.setattr(snapshot, "discover_markdown", lambda root: [e for e in original(root) if e.relative_path == "a.md"])
    if failure == "permission":
        original_lstat = Path.lstat

        def denied(path, *args, **kwargs):
            if path == wiki / "b.md":
                raise PermissionError("cannot confirm deletion")
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(SnapshotError):
        build_snapshot(wiki, state)
    assert (state / "current.json").read_bytes() == before


def test_boundary_fifo_never_blocks(tmp_path):
    import subprocess
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _make_wiki(wiki, {"good.md": "# Good"})
    os.mkfifo(wiki / "pipe.md")
    code = "from pathlib import Path; from local_wiki_librarian.snapshot import build_snapshot; build_snapshot(Path(__import__('sys').argv[1]), Path(__import__('sys').argv[2]))"
    result = subprocess.run([sys.executable, "-c", code, str(wiki), str(state)], timeout=5, capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
    assert "SnapshotError" in result.stderr
    assert not (state / "current.json").exists()


@pytest.mark.parametrize("component", ["generation", "file", "traversal"])
def test_boundary_previous_fallback_is_confined(tmp_path, monkeypatch, component):
    import local_wiki_librarian.paths as paths
    wiki, state, outside = tmp_path / "wiki", tmp_path / "state", tmp_path / "outside"
    _make_wiki(wiki, {"page.md": "# Safe", "good.md": "# Good"})
    _make_wiki(outside, {"page.md": "EXTERNAL"})
    first = build_snapshot(wiki, state)
    generation = state / "snapshots" / first.generation
    if component == "generation":
        generation.rename(generation.with_name("saved"))
        generation.symlink_to(outside)
    elif component == "file":
        (generation / "page.md").unlink()
        (generation / "page.md").symlink_to(outside / "page.md")
    else:
        pointer = _read_current(state)
        pointer["generation"] = "../outside"
        (state / "current.json").write_text(json.dumps(pointer))
    before = (state / "current.json").read_bytes()
    original = paths.os.open

    def fail_source(path, flags, mode=0o777, *, dir_fd=None):
        if path == "page.md" and dir_fd is not None:
            directory = os.fstat(dir_fd)
            if (directory.st_dev, directory.st_ino) == (wiki.stat().st_dev, wiki.stat().st_ino):
                raise PermissionError("ordinary source read failure")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(paths.os, "open", fail_source)
    with pytest.raises(SnapshotError):
        build_snapshot(wiki, state)
    assert (state / "current.json").read_bytes() == before
    assert (outside / "page.md").read_bytes() == b"EXTERNAL"


@pytest.mark.parametrize("phase", ["temp", "file_fsync", "replace", "directory_fsync", "readback", "interrupt"])
def test_boundary_pointer_write_failure_keeps_referenced_generation(tmp_path, monkeypatch, phase):
    import local_wiki_librarian.paths as paths
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _make_wiki(wiki, {"page.md": "# First"})
    first = build_snapshot(wiki, state)
    before = (state / "current.json").read_bytes()
    (wiki / "page.md").write_text("# Second")
    replaced = False
    writing_pointer = False
    original_replace, original_read = paths.os.replace, paths.read_bytes_within
    original_fsync, original_temp = paths.os.fsync, paths.tempfile.mkstemp

    def replace(source, destination, **kwargs):
        nonlocal replaced
        if destination == "current.json":
            if phase == "replace":
                raise OSError("injected before replace")
            if phase == "interrupt":
                raise KeyboardInterrupt("injected cancellation")
            original_replace(source, destination, **kwargs)
            replaced = True
            return
        return original_replace(source, destination, **kwargs)

    def fsync(descriptor):
        if phase == "file_fsync" and writing_pointer:
            raise OSError("injected temporary-file fsync")
        if phase == "directory_fsync" and replaced:
            raise OSError("injected after replace fsync")
        return original_fsync(descriptor)

    def read(root, relative):
        if phase == "readback" and replaced and str(relative) == "current.json":
            raise OSError("injected after replace readback")
        return original_read(root, relative)

    def temporary(*args, **kwargs):
        nonlocal writing_pointer
        writing_pointer = kwargs.get("prefix") == ".current.json."
        if phase == "temp" and kwargs.get("prefix") == ".current.json.":
            raise OSError("injected before temporary write")
        return original_temp(*args, **kwargs)

    monkeypatch.setattr(paths.os, "replace", replace)
    monkeypatch.setattr(paths.os, "fsync", fsync)
    monkeypatch.setattr(paths, "read_bytes_within", read)
    monkeypatch.setattr(paths.tempfile, "mkstemp", temporary)
    with pytest.raises(KeyboardInterrupt if phase == "interrupt" else SnapshotError):
        build_snapshot(wiki, state)
    after = (state / "current.json").read_bytes()
    pointer = json.loads(after)
    referenced = state / "snapshots" / pointer["generation"]
    assert referenced.is_dir(), f"dangling pointer after {phase}"
    assert (referenced / "manifest.json").is_file()
    assert (state / "snapshots" / first.generation / "manifest.json").is_file()
    assert not list(state.glob(".current.json.*.tmp"))
    if phase in {"temp", "file_fsync", "replace", "interrupt"}:
        assert after == before, f"current changed before replacement: {phase}"
    else:
        assert after != before and (referenced / "page.md").read_bytes() == b"# Second"
    if phase == "interrupt":
        monkeypatch.undo()
        resumed = build_snapshot(wiki, state)
        assert _read_current(state)["generation"] == resumed.generation


def test_boundary_predictable_snapshot_temp_is_not_touched(tmp_path, monkeypatch):
    import local_wiki_librarian.snapshot as snapshot
    wiki, state, sentinel = tmp_path / "wiki", tmp_path / "state", tmp_path / "sentinel"
    _make_wiki(wiki, {"page.md": "# Safe"})
    state.mkdir()
    sentinel.write_bytes(b"SENTINEL")
    before = sentinel.stat()
    link = state / ".current.fixed-generation.tmp"
    link.symlink_to(sentinel)
    monkeypatch.setattr(snapshot, "_generation_name", lambda: "fixed-generation")
    result = build_snapshot(wiki, state)
    assert result.generation == _read_current(state)["generation"]
    assert sentinel.read_bytes() == b"SENTINEL"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns
    assert link.is_symlink()


@pytest.mark.parametrize("prior", [False, True])
def test_boundary_ordinary_read_failure_retries_then_quarantines_or_stales(tmp_path, monkeypatch, prior):
    import local_wiki_librarian.paths as paths
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _make_wiki(wiki, {"page.md": "# ORIGINAL", "good.md": "# Good"})
    if prior:
        build_snapshot(wiki, state)
    original = paths.os.open
    attempts = 0
    source_before = _source_state(wiki)

    def fail(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal attempts
        if path == "page.md" and dir_fd is not None and os.fstat(dir_fd).st_ino == wiki.stat().st_ino:
            attempts += 1
            raise PermissionError("ordinary failure")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(paths.os, "open", fail)
    result = build_snapshot(wiki, state)
    assert attempts == 3
    assert result.summary["stale" if prior else "quarantined"] == 1
    copied = state / "snapshots" / result.generation / "page.md"
    assert copied.read_bytes() == b"# ORIGINAL" if prior else not copied.exists()
    assert _source_state(wiki) == source_before


@pytest.mark.parametrize("scenario", ["initial", "existing", "discovery", "open", "receipt"])
def test_boundary_nonregular_socket_is_not_quarantined_or_stale(monkeypatch, scenario):
    import socket
    import tempfile
    import local_wiki_librarian.paths as paths
    import local_wiki_librarian.snapshot as snapshot
    from local_wiki_librarian.refresh import build_root_migration_receipt

    # Short fixture paths stay below macOS's AF_UNIX sockaddr pathname limit.
    with tempfile.TemporaryDirectory(prefix="s5-") as temporary, socket.socket(socket.AF_UNIX) as endpoint:
        base = Path(temporary).resolve()
        wiki, state = base / "wiki", base / "state"
        _make_wiki(wiki, {"good.md": "# Good", "socket.md": "# Regular before replacement"})
        if scenario != "initial":
            build_snapshot(wiki, state)
        before = (state / "current.json").read_bytes() if scenario != "initial" else None
        source_after_injection = []

        def replace_with_socket():
            (wiki / "socket.md").unlink()
            endpoint.bind(str(wiki / "socket.md"))
            source_after_injection.extend(_source_state(wiki))

        if scenario == "discovery":
            original_discover = snapshot.discover_markdown

            def discover(root):
                entries = original_discover(root)
                replace_with_socket()
                return entries

            monkeypatch.setattr(snapshot, "discover_markdown", discover)
        elif scenario == "open":
            original_open = paths.os.open

            def open_after_replacement(path, flags, mode=0o777, *, dir_fd=None):
                if path == "socket.md" and dir_fd is not None and not source_after_injection:
                    replace_with_socket()
                return original_open(path, flags, mode, dir_fd=dir_fd)

            monkeypatch.setattr(paths.os, "open", open_after_replacement)
        else:
            replace_with_socket()

        if scenario == "receipt":
            old = base / "old"
            _make_wiki(old, {"old.md": "# Old"})
            old_state = base / "old-state"
            build_snapshot(old, old_state)
            old_current = (old_state / "current.json").read_bytes()
            with pytest.raises(paths.UnsafePathError, match="not a regular file"):
                build_root_migration_receipt(old_state, wiki)
            assert (old_state / "current.json").read_bytes() == old_current
        else:
            with pytest.raises(SnapshotError, match="not a regular file"):
                build_snapshot(wiki, state)
        assert source_after_injection and _source_state(wiki) == source_after_injection
        if before is None:
            assert not (state / "current.json").exists()
        else:
            assert (state / "current.json").read_bytes() == before
