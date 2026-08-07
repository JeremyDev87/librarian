from __future__ import annotations

from pathlib import Path

import pytest

from local_wiki_librarian.connectome import ConnectomeError, extract_connectome, expand_connectome


def test_only_exact_connection_section_produces_typed_edges(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("# Target", encoding="utf-8")
    (tmp_path / "source.md").write_text(
        "# Source\n\n- **supports**: [[ignored]]\n\n"
        "## 연결\n\n- **supports**: [[target]]\n",
        encoding="utf-8",
    )
    graph = extract_connectome(tmp_path)
    assert [(e.relation, e.target) for e in graph.edges] == [("supports", "target.md")]


def test_broken_target_and_forbidden_relation_are_lint_failures(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text(
        "## 연결\n\n"
        "- **supports**: [[missing]]\n"
        "- **confers_authority**: [[missing]]\n",
        encoding="utf-8",
    )
    graph = extract_connectome(tmp_path)
    codes = {finding.code for finding in graph.findings}
    assert "BROKEN_TYPED_EDGE" in codes
    assert "FORBIDDEN_RELATION" in codes


def test_expand_depth_has_hard_cap_two(tmp_path: Path) -> None:
    for name, target in [("a", "b"), ("b", "c"), ("c", "d")]:
        (tmp_path / f"{name}.md").write_text(
            f"## 연결\n\n- **supports**: [[{target}]]\n", encoding="utf-8"
        )
    (tmp_path / "d.md").write_text("# D", encoding="utf-8")
    graph = extract_connectome(tmp_path)
    assert expand_connectome("a.md", graph, max_hops=2) == ["b.md", "c.md"]
    with pytest.raises(ConnectomeError):
        expand_connectome("a.md", graph, max_hops=3)
