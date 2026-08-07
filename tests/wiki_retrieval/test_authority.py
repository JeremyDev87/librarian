"""Tests for authority compilation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.authority import compile_authority  # noqa: E402


def _make_gen(gen_dir: Path, files: dict[str, str]) -> None:
    """Create a snapshot generation fixture."""
    (gen_dir / "manifest.json").write_text('{"generation": "test"}', encoding="utf-8")
    for rel, content in files.items():
        path = gen_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


CURRENT_PAGE = """---
wiki_schema: librarian-v1
layer: policy
domain: wiki
authority: high
status: active
do_not_answer_as_current: false
last_verified: "2026-06-03"
---

# Current authority page
"""

REDIRECT_PAGE = """---
wiki_schema: librarian-v1
status: redirected
do_not_answer_as_current: true
canonical_path: knowledge/policies/real.md
redirect_reason: "Renamed"
---

# Redirect stub
"""

EVIDENCE_PAGE = """---
wiki_schema: librarian-v1
source_role: source_map
authority: evidence
status: active
---

# Source map
"""

RAW_PAGE = """---
title: Raw note
---

# Raw note
"""

INDEX_PAGE = """---
wiki_schema: librarian-v1
authority: high
status: active
---

# Index
"""

SUPERSEDED_PAGE = """---
wiki_schema: librarian-v1
authority: high
status: superseded
---

# Old page
"""


class TestCompileAuthorityClassification:
    def test_current_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"knowledge/policies/policy.md": CURRENT_PAGE})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "current"

    def test_redirect_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "knowledge/policies/alias.md": REDIRECT_PAGE,
            "knowledge/policies/real.md": CURRENT_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        tiers = {e.relative_path: e.tier for e in manifest.entries}
        assert tiers["knowledge/policies/alias.md"] == "redirect"
        assert tiers["knowledge/policies/real.md"] == "current"

    def test_raw_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"raw/source-map.md": EVIDENCE_PAGE})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "raw"

    def test_raw_prefix_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"raw/note.md": RAW_PAGE})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "raw"

    def test_index_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"metadata/index.md": INDEX_PAGE})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "index"

    def test_history_tier(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"metadata/old.md": SUPERSEDED_PAGE})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "history"

    def test_no_frontmatter_unknown(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"page.md": "# No frontmatter"})
        manifest = compile_authority(gen, "test-gen")
        entry = manifest.entries[0]
        assert entry.tier == "unknown"

    @pytest.mark.parametrize(
        "frontmatter",
        [
            "authority: low\nstatus: active",
            "title: Ordinary note",
            "authority: high",
            "status: active",
        ],
    )
    def test_current_truth_requires_explicit_high_and_active(
        self, tmp_path: Path, frontmatter: str,
    ) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"page.md": f"---\n{frontmatter}\n---\n# Page\n"})

        assert compile_authority(gen, "test-gen").entries[0].tier != "current"

    @pytest.mark.parametrize("value", ["'true'", "1", "[true]", "{}", "null", ""])
    def test_invalid_do_not_answer_value_fails_closed(
        self, tmp_path: Path, value: str,
    ) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "page.md": (
                "---\nauthority: high\nstatus: active\n"
                f"do_not_answer_as_current: {value}\n---\n# Page\n"
            )
        })

        entry = compile_authority(gen, "test-gen").entries[0]
        assert entry.tier != "current"
        assert entry.do_not_answer_as_current is True

    def test_icloud_conflict_copy_cannot_be_current_when_original_exists(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "knowledge/policies/policy.md": CURRENT_PAGE,
            "knowledge/policies/policy 2.md": CURRENT_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        tiers = {entry.relative_path: entry.tier for entry in manifest.entries}
        assert tiers["knowledge/policies/policy.md"] == "current"
        assert tiers["knowledge/policies/policy 2.md"] == "history"

    def test_numbered_filename_without_original_is_not_assumed_conflict(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"knowledge/policies/policy 2.md": CURRENT_PAGE})
        manifest = compile_authority(gen, "test-gen")
        assert manifest.entries[0].tier == "current"


class TestRedirectResolution:
    def test_redirect_map_built(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "metadata/alias.md": REDIRECT_PAGE,
            "knowledge/policies/real.md": CURRENT_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        assert "metadata/alias.md" in manifest.redirect_map
        assert manifest.redirect_map["metadata/alias.md"] == "knowledge/policies/real.md"

    def test_unresolved_redirect_detected(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {"metadata/alias.md": REDIRECT_PAGE})
        # target knowledge/policies/real.md does NOT exist
        manifest = compile_authority(gen, "test-gen")
        assert len(manifest.unresolved_redirects) == 1
        assert manifest.unresolved_redirects[0]["from"] == "metadata/alias.md"

    def test_resolved_redirect_no_unresolved(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "metadata/alias.md": REDIRECT_PAGE,
            "knowledge/policies/real.md": CURRENT_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        assert len(manifest.unresolved_redirects) == 0


class TestTierCounts:
    def test_tier_counts(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "knowledge/policies/current.md": CURRENT_PAGE,
            "metadata/index.md": INDEX_PAGE,
            "raw/source.md": EVIDENCE_PAGE,
            "metadata/old.md": SUPERSEDED_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        assert manifest.tier_counts["current"] == 1
        assert manifest.tier_counts["index"] == 1
        assert manifest.tier_counts["raw"] == 1
        assert manifest.tier_counts["history"] == 1


class TestSerialization:
    def test_to_dict_roundtrip(self, tmp_path: Path) -> None:
        gen = tmp_path / "gen"
        gen.mkdir()
        _make_gen(gen, {
            "knowledge/policies/policy.md": CURRENT_PAGE,
            "metadata/alias.md": REDIRECT_PAGE,
        })
        manifest = compile_authority(gen, "test-gen")
        d = manifest.to_dict()
        assert d["schema_version"] == 1
        assert d["generation"] == "test-gen"
        assert len(d["entries"]) == 2
        assert d["redirect_map"]["metadata/alias.md"] == "knowledge/policies/real.md"


class TestMissingGeneration:
    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            compile_authority(tmp_path / "nonexistent", "gen")
