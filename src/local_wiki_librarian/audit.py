"""Deterministic read-only Catalog Auditor."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .authority import AuthorityManifest
from .catalog import Catalog
from .connectome import Connectome


@dataclass(frozen=True)
class AuditFinding:
    code: str
    message: str
    severity: str = "error"
    path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditContext:
    catalog: Catalog
    authority: AuthorityManifest
    connectome: Optional[Connectome] = None
    index_collections: dict[str, dict] = field(default_factory=dict)
    expected_collections: set[str] = field(default_factory=set)
    expected_collection_paths: dict[str, str] = field(default_factory=dict)
    index_degraded: bool = False
    verification_max_age_days: int = 180
    as_of: Optional[datetime] = None


@dataclass(frozen=True)
class AuditReport:
    findings: list[AuditFinding]

    @property
    def passed(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "finding_count": len(self.findings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _normal_path(path: str) -> str:
    return str(Path(path).expanduser()).rstrip("/")


def run_catalog_audit(context: AuditContext) -> AuditReport:
    """Compare projections and emit findings without mutating any surface."""
    findings: list[AuditFinding] = [
        AuditFinding(code=f.code, message=f.message, severity=f.severity, path=f.path)
        for f in context.catalog.findings
    ]
    for redirect in context.authority.unresolved_redirects:
        findings.append(AuditFinding(
            code="UNRESOLVED_REDIRECT",
            message=f"{redirect.get('from', '')} -> {redirect.get('to', '')}",
            path=redirect.get("from"),
        ))
    for entry in context.authority.entries:
        unsafe_prefix = entry.relative_path.startswith(("raw/", "assets/raw/", "assets/"))
        unsafe_name = Path(entry.relative_path).name in {"index.md", "log.md"}
        if entry.tier == "current" and (entry.do_not_answer_as_current or unsafe_prefix or unsafe_name):
            findings.append(AuditFinding(
                code="UNSAFE_CURRENT_TIER",
                message="current tier conflicts with evidence-only/current-answer exclusion",
                path=entry.relative_path,
            ))
        if entry.tier in {"current", "authority"} and entry.last_verified:
            raw_verified = entry.last_verified.replace("Z", "+00:00")
            try:
                verified_at = datetime.fromisoformat(raw_verified)
                if verified_at.tzinfo is None:
                    verified_at = verified_at.replace(tzinfo=timezone.utc)
                as_of = context.as_of or datetime.now(timezone.utc)
                if as_of - verified_at > timedelta(days=context.verification_max_age_days):
                    findings.append(AuditFinding(
                        code="STALE_VERIFICATION",
                        message=f"verification is older than {context.verification_max_age_days} days",
                        path=entry.relative_path,
                    ))
            except ValueError:
                findings.append(AuditFinding(
                    code="STALE_VERIFICATION",
                    message="last_verified is malformed",
                    path=entry.relative_path,
                ))
    if context.connectome:
        for finding in context.connectome.findings:
            code = "BROKEN_TYPED_EDGE" if finding.code == "BROKEN_TYPED_EDGE" else finding.code
            findings.append(AuditFinding(
                code=code,
                message=finding.message,
                path=f"{finding.source}:{finding.line}",
            ))
    expected_names = set(context.expected_collections) | set(context.expected_collection_paths)
    for name in sorted(expected_names):
        expected = context.expected_collection_paths.get(name)
        actual = context.index_collections.get(name)
        if actual is None:
            findings.append(AuditFinding(
                code="INDEX_DEGRADED", message=f"collection {name} is missing", path=name
            ))
            continue
        if int(actual.get("files", 0) or 0) == 0:
            findings.append(AuditFinding(
                code="COLLECTION_EMPTY", message=f"collection {name} has zero files", path=name
            ))
        actual_path = str(actual.get("path", ""))
        if expected and actual_path and _normal_path(actual_path) != _normal_path(expected):
            findings.append(AuditFinding(
                code="COLLECTION_PATH_DRIFT",
                message=f"collection {name} path differs from Registry projection",
                path=name,
            ))
    if context.index_degraded:
        findings.append(AuditFinding(
            code="INDEX_DEGRADED", message="retrieval state is unavailable or incomplete"
        ))
    findings.sort(key=lambda item: (item.code, item.path or "", item.message))
    return AuditReport(findings=findings)
