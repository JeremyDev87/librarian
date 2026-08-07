"""Scoring metrics for wiki retrieval evaluation.

Computes recall@k, MRR, top-1 accuracy, and authority false-positive rate
from ranked retrieval results against ground-truth expected paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
__all__ = [
    "QueryResult",
    "EvalReport",
    "score_query",
    "aggregate_reports",
    "format_report",
]


@dataclass(frozen=True)
class QueryResult:
    """One query's evaluation result."""

    query: str
    expected_paths: list[str]
    retrieved_paths: list[str]
    raw_or_index_in_results: list[str] = field(default_factory=list)
    k: int = 5
    expected_no_answer: bool = False

    @property
    def recall_at_k(self) -> float:
        """Fraction of expected paths found in top-k results."""
        top_k = self.retrieved_paths[: self.k]
        if self.expected_no_answer:
            return 1.0 if not top_k else 0.0
        if not self.expected_paths:
            return 0.0
        hits = sum(1 for exp in self.expected_paths if exp in top_k)
        return hits / len(self.expected_paths)

    @property
    def mrr(self) -> float:
        """Mean Reciprocal Rank — 1/rank of first relevant result."""
        if self.expected_no_answer:
            return 1.0 if not self.retrieved_paths[: self.k] else 0.0
        for i, path in enumerate(self.retrieved_paths):
            if path in self.expected_paths:
                return 1.0 / (i + 1)
        return 0.0

    @property
    def top1_hit(self) -> bool:
        """True if the first retrieved result is an expected path."""
        if self.expected_no_answer:
            return not self.retrieved_paths[: self.k]
        return bool(self.retrieved_paths) and self.retrieved_paths[0] in self.expected_paths

    @property
    def authority_false_positive(self) -> bool:
        """True if raw/index/log pages appear in top-k results."""
        if not self.raw_or_index_in_results:
            return False
        top_k_set = set(self.retrieved_paths[: self.k])
        return any(p in top_k_set for p in self.raw_or_index_in_results)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "expected_paths": self.expected_paths,
            "expected_no_answer": self.expected_no_answer,
            "retrieved_paths": self.retrieved_paths[: self.k],
            "recall_at_k": round(self.recall_at_k, 4),
            "mrr": round(self.mrr, 4),
            "top1_hit": self.top1_hit,
            "authority_false_positive": self.authority_false_positive,
        }


@dataclass(frozen=True)
class EvalReport:
    """Aggregate evaluation metrics across all queries."""

    engine: str
    query_count: int
    avg_recall_at_k: float
    avg_mrr: float
    top1_accuracy: float
    authority_false_positive_count: int
    authority_false_positive_rate: float
    per_query: list[QueryResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "query_count": self.query_count,
            "avg_recall_at_k": round(self.avg_recall_at_k, 4),
            "avg_mrr": round(self.avg_mrr, 4),
            "top1_accuracy": round(self.top1_accuracy, 4),
            "authority_false_positive_count": self.authority_false_positive_count,
            "authority_false_positive_rate": round(self.authority_false_positive_rate, 4),
            "per_query": [qr.to_dict() for qr in self.per_query],
        }


def score_query(
    query: str,
    expected_paths: list[str],
    retrieved_paths: list[str],
    raw_or_index_paths: list[str] | None = None,
    k: int = 5,
    expected_no_answer: bool = False,
) -> QueryResult:
    """Score a single query's retrieval results.

    Args:
        query: The search query string.
        expected_paths: Ground-truth relevant file paths.
        retrieved_paths: Engine-returned ranked file paths.
        raw_or_index_paths: Known raw/index/log paths for false-positive detection.
        k: Cutoff for recall@k.

    Returns:
        QueryResult with computed metrics.
    """
    return QueryResult(
        query=query,
        expected_paths=expected_paths,
        retrieved_paths=retrieved_paths,
        raw_or_index_in_results=raw_or_index_paths or [],
        k=k,
        expected_no_answer=expected_no_answer,
    )


def aggregate_reports(engine: str, results: list[QueryResult]) -> EvalReport:
    """Aggregate per-query results into an overall report.

    Args:
        engine: Engine name (for example, "wikimap").
        results: List of per-query QueryResult objects.

    Returns:
        EvalReport with aggregate metrics.
    """
    if not results:
        return EvalReport(
            engine=engine,
            query_count=0,
            avg_recall_at_k=0.0,
            avg_mrr=0.0,
            top1_accuracy=0.0,
            authority_false_positive_count=0,
            authority_false_positive_rate=0.0,
            per_query=[],
        )

    n = len(results)
    avg_recall = sum(r.recall_at_k for r in results) / n
    avg_mrr = sum(r.mrr for r in results) / n
    top1_hits = sum(1 for r in results if r.top1_hit)
    fp_count = sum(1 for r in results if r.authority_false_positive)

    return EvalReport(
        engine=engine,
        query_count=n,
        avg_recall_at_k=avg_recall,
        avg_mrr=avg_mrr,
        top1_accuracy=top1_hits / n,
        authority_false_positive_count=fp_count,
        authority_false_positive_rate=fp_count / n,
        per_query=list(results),
    )


def format_report(report: EvalReport) -> str:
    """Format an EvalReport as a human-readable summary string."""
    lines = [
        f"Engine: {report.engine}",
        f"  Queries:       {report.query_count}",
        f"  Recall@5:      {report.avg_recall_at_k:.4f}",
        f"  MRR:           {report.avg_mrr:.4f}",
        f"  Top-1 Accuracy: {report.top1_accuracy:.4f}",
        f"  Authority FP:  {report.authority_false_positive_count} "
        f"({report.authority_false_positive_rate:.2%})",
    ]
    return "\n".join(lines)
