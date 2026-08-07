"""Tests for frontmatter parsing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_frontmatter_bytes,
)


class TestParseFrontmatterBasic:
    def test_no_frontmatter(self) -> None:
        fm = parse_frontmatter_bytes(b"# Just a title\n\ntext", "page.md")
        assert fm.has_frontmatter is False
        assert fm.has_authority_schema is False

    def test_empty_frontmatter(self) -> None:
        fm = parse_frontmatter_bytes(b"---\n---\n# Page", "page.md")
        assert fm.has_frontmatter is True
        assert fm.has_authority_schema is False

    def test_authority_schema_frontmatter(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
layer: policy
domain: wiki
authority: high
status: active
do_not_answer_as_current: false
last_verified: "2026-06-03"
---

# Page title
"""
        fm = parse_frontmatter_bytes(content, "metadata/page.md")
        assert fm.has_frontmatter is True
        assert fm.has_authority_schema is True
        assert fm.wiki_schema == "librarian-v1"
        assert fm.layer == "policy"
        assert fm.domain == "wiki"
        assert fm.authority == "high"
        assert fm.status == "active"
        assert fm.do_not_answer_as_current is False
        assert fm.last_verified == "2026-06-03"

    def test_redirect_frontmatter(self) -> None:
        content = b"""---
wiki_schema: "librarian-v1"
status: "redirected"
do_not_answer_as_current: true
canonical_path: "knowledge/core/assets/natural-gate.md"
redirect_reason: "Renamed"
---

# Redirect stub
"""
        fm = parse_frontmatter_bytes(content, "metadata/legacy.md")
        assert fm.is_redirect is True
        assert fm.canonical_path == "knowledge/core/assets/natural-gate.md"

    def test_evidence_frontmatter(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
source_role: source_map
authority: evidence
status: active
---

# Source map
"""
        fm = parse_frontmatter_bytes(content, "raw/source-map.md")
        assert fm.is_raw_or_evidence is True
        assert fm.is_authoritative is False

    def test_raw_prefix_is_evidence(self) -> None:
        content = b"""---
title: Some raw note
---

# Raw note
"""
        fm = parse_frontmatter_bytes(content, "raw/2026-01-01-note.md")
        assert fm.is_raw_or_evidence is True

    def test_malformed_frontmatter_raises(self) -> None:
        content = b"---\n: : : invalid yaml\n---\n# Page"
        with pytest.raises(FrontmatterError, match="malformed"):
            parse_frontmatter_bytes(content, "bad.md")

    def test_tags_as_list(self) -> None:
        content = b'---\ntags: [python, testing, wiki]\n---\n# Page'
        fm = parse_frontmatter_bytes(content, "page.md")
        assert fm.tags == ["python", "testing", "wiki"]

    def test_tags_as_string(self) -> None:
        content = b'---\ntags: "python, testing"\n---\n# Page'
        fm = parse_frontmatter_bytes(content, "page.md")
        assert fm.tags == ["python", "testing"]

    def test_non_mapping_frontmatter_raises(self) -> None:
        content = b"---\n- item1\n- item2\n---\n# Page"
        with pytest.raises(FrontmatterError, match="must be a mapping"):
            parse_frontmatter_bytes(content, "bad.md")


class TestAuthorityClassification:
    def test_current_truth(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: high
status: active
do_not_answer_as_current: false
---

# Active high-authority page
"""
        fm = parse_frontmatter_bytes(content, "knowledge/policies/policy.md")
        assert fm.is_current_truth is True
        assert fm.is_authoritative is True

    def test_superseded_is_not_current(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: high
status: superseded
---

# Old page
"""
        fm = parse_frontmatter_bytes(content, "metadata/old.md")
        assert fm.is_current_truth is False
        assert fm.is_authoritative is False

    def test_do_not_answer_as_current(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: high
status: active
do_not_answer_as_current: true
---

# Locked page
"""
        fm = parse_frontmatter_bytes(content, "metadata/locked.md")
        assert fm.is_current_truth is False
        assert fm.is_authoritative is False
        assert fm.is_redirect is True

    def test_redirect_alias_authority(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: redirect_alias
status: redirected
canonical_path: "real.md"
---

# Redirect
"""
        fm = parse_frontmatter_bytes(content, "metadata/alias.md")
        assert fm.is_redirect is True
        assert fm.is_authoritative is False

    def test_index_file_classification(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: high
status: active
---

# Index
"""
        fm = parse_frontmatter_bytes(content, "metadata/index.md")
        assert fm.is_index_or_log is True

    def test_claude_md_is_index(self) -> None:
        content = b"""---
wiki_schema: librarian-v1
authority: high
status: active
---

# CLAUDE
"""
        fm = parse_frontmatter_bytes(content, "knowledge/policies/CLAUDE.md")
        assert fm.is_index_or_log is True
