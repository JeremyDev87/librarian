"""Stable, JSON-serializable contracts for the read-only Wiki Librarian."""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class StopState(str, enum.Enum):
    INDEX_DEGRADED = "INDEX_DEGRADED"
    NO_CANON_FOUND = "NO_CANON_FOUND"
    AUTHORITY_AMBIGUOUS = "AUTHORITY_AMBIGUOUS"
    CONFLICT_OPEN = "CONFLICT_OPEN"
    FRESH_SOURCE_REQUIRED = "FRESH_SOURCE_REQUIRED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    WRITE_APPROVAL_REQUIRED = "WRITE_APPROVAL_REQUIRED"


class CandidateVerdict(str, enum.Enum):
    SELECTED = "selected"
    SUPPORTING = "supporting"
    EVIDENCE_ONLY = "evidence_only"
    REJECTED = "rejected"


@dataclass(frozen=True)
class LibrarianQuery:
    query: str
    intent: str = "explain"
    time_scope: str = "saved_knowledge"
    requested_domain: Optional[str] = None
    external_verification_allowed: bool = False
    max_graph_hops: int = 1

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query must not be empty")
        if self.max_graph_hops < 0 or self.max_graph_hops > 2:
            raise ValueError("max_graph_hops must be between 0 and 2")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateCard:
    path: str
    canonical_path: str
    domain: Optional[str] = None
    entity_type: Optional[str] = None
    source_role: Optional[str] = None
    status: Optional[str] = None
    do_not_answer_as_current: bool = False
    retrieval_score: Optional[float] = None
    graph_distance: Optional[int] = None
    freshness: Optional[str] = None
    authority_class: str = "unknown"
    verdict: CandidateVerdict = CandidateVerdict.REJECTED
    reason: str = ""
    engines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data


@dataclass(frozen=True)
class TrailStep:
    document: str
    reason_read: str
    result: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class LibrarianPacket:
    query: LibrarianQuery
    status: str = "ok"
    route: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    authority: dict[str, Any] = field(default_factory=dict)
    candidates: list[CandidateCard] = field(default_factory=list)
    trail: list[TrailStep] = field(default_factory=list)
    answer: dict[str, Any] = field(default_factory=lambda: {
        "verified": [],
        "inference": [],
        "unknowns": [],
        "fresh_verification_needed": False,
    })
    stop_reason: Optional[StopState] = None
    recommended_action: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "query": self.query.to_dict(),
            "status": self.status,
            "route": self.route,
            "retrieval": self.retrieval,
            "authority": self.authority,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "trail": [step.to_dict() for step in self.trail],
            "answer": self.answer,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "recommended_action": self.recommended_action,
            "warnings": list(self.warnings),
        }
