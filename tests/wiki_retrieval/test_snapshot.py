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
