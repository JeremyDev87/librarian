"""Tests for scoring metrics."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from local_wiki_librarian.scorer import (  # noqa: E402

    score_query,
    aggregate_reports,
    format_report,
)


class TestRecallAtK:
    def test_perfect_recall(self) -> None:
        qr = score_query(
            query="test",
            expected_paths=["a.md", "b.md"],
            retrieved_paths=["a.md", "b.md", "c.md"],
        )
        assert qr.recall_at_k == 1.0

    def test_partial_recall(self) -> None:
        qr = score_query(
            query="test",
            expected_paths=["a.md", "b.md"],
            retrieved_paths=["a.md", "c.md"],
        )
        assert qr.recall_at_k == 0.5

    def test_zero_recall(self) -> None:
        qr = score_query(
            query="test",
            expected_paths=["a.md"],
            retrieved_paths=["b.md", "c.md"],
        )
        assert qr.recall_at_k == 0.0

    def test_recall_respects_k(self) -> None:
        qr = score_query(
            query="test",
            expected_paths=["a.md"],
            retrieved_paths=["x.md", "y.md", "z.md", "w.md", "v.md", "a.md"],
            k=5,
        )
        # a.md is at position 6, outside top-5
        assert qr.recall_at_k == 0.0


class TestMRR:
    def test_rank1_mrr(self) -> None:
        qr = score_query("t", ["a.md"], ["a.md", "b.md"])
        assert qr.mrr == 1.0

    def test_rank2_mrr(self) -> None:
        qr = score_query("t", ["a.md"], ["b.md", "a.md"])
        assert qr.mrr == 0.5

    def test_no_hit_mrr(self) -> None:
        qr = score_query("t", ["a.md"], ["b.md", "c.md"])
        assert qr.mrr == 0.0


class TestTop1Accuracy:
    def test_top1_hit(self) -> None:
        qr = score_query("t", ["a.md"], ["a.md", "b.md"])
        assert qr.top1_hit is True

    def test_top1_miss(self) -> None:
        qr = score_query("t", ["a.md"], ["b.md", "a.md"])
        assert qr.top1_hit is False


class TestAuthorityFalsePositive:
    def test_fp_detected(self) -> None:
        qr = score_query(
            "t", ["a.md"], ["raw/note.md", "a.md"],
            raw_or_index_paths=["raw/note.md"],
        )
        assert qr.authority_false_positive is True

    def test_no_fp(self) -> None:
        qr = score_query(
            "t", ["a.md"], ["a.md", "b.md"],
            raw_or_index_paths=["raw/note.md"],
        )
        assert qr.authority_false_positive is False

    def test_fp_outside_top_k(self) -> None:
        qr = score_query(
            "t", ["a.md"], ["a.md", "b.md", "c.md", "d.md", "e.md", "raw/note.md"],
            raw_or_index_paths=["raw/note.md"],
            k=5,
        )
        assert qr.authority_false_positive is False


class TestAggregate:
    def test_aggregate_two_queries(self) -> None:
        results = [
            score_query("q1", ["a.md"], ["a.md"]),
            score_query("q2", ["b.md"], ["c.md", "b.md"]),
        ]
        report = aggregate_reports("test", results)
        assert report.engine == "test"
        assert report.query_count == 2
        # Both expected paths are in top-5, so recall is 1.0 for both
        assert report.avg_recall_at_k == 1.0
        assert report.top1_accuracy == 0.5  # only q1 has top-1 hit
        assert report.avg_mrr == 0.75  # (1.0 + 0.5) / 2

    def test_empty_results(self) -> None:
        report = aggregate_reports("test", [])
        assert report.query_count == 0
        assert report.avg_recall_at_k == 0.0

    def test_format_report(self) -> None:
        results = [score_query("q", ["a.md"], ["a.md"])]
        report = aggregate_reports("test", results)
        text = format_report(report)
        assert "Engine: test" in text
        assert "Recall@5" in text


class TestSerialization:
    def test_query_result_to_dict(self) -> None:
        qr = score_query("q", ["a.md"], ["a.md"])
        d = qr.to_dict()
        assert d["query"] == "q"
        assert d["recall_at_k"] == 1.0
        assert d["top1_hit"] is True

    def test_eval_report_to_dict(self) -> None:
        results = [score_query("q", ["a.md"], ["a.md"])]
        report = aggregate_reports("test", results)
        d = report.to_dict()
        assert d["engine"] == "test"
        assert len(d["per_query"]) == 1
