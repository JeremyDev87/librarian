"""Read-only Query Librarian: route and classify evidence into a packet."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .catalog import Catalog, RegistryEntry, route_query
from .contracts import (
    CandidateCard,
    CandidateVerdict,
    LibrarianPacket,
    LibrarianQuery,
    StopState,
    TrailStep,
)

_NON_CURRENT = {"raw", "index", "history", "redirect", "draft", "unknown"}


def _engines(item: dict[str, Any]) -> list[str]:
    values = item.get("engines")
    if isinstance(values, list):
        return sorted({str(value) for value in values})
    engine = item.get("engine")
    return [str(engine)] if engine else []


def _card(item: dict[str, Any]) -> CandidateCard:
    path = str(item.get("path", ""))
    canonical = str(item.get("canonical_path") or path)
    tier = str(item.get("tier", "unknown"))
    forbidden = bool(item.get("do_not_answer_as_current")) or tier in _NON_CURRENT
    if tier in {"raw", "index", "history"}:
        verdict = CandidateVerdict.EVIDENCE_ONLY
        reason = f"{tier} tier is discovery/evidence only"
    elif tier == "redirect":
        verdict = CandidateVerdict.REJECTED
        reason = "redirect aliases cannot answer as current"
    elif forbidden:
        verdict = CandidateVerdict.REJECTED
        reason = "authority is unknown, draft, or explicitly non-current"
    else:
        verdict = CandidateVerdict.SUPPORTING
        reason = "eligible authority candidate; owner selection pending"
    return CandidateCard(
        path=path,
        canonical_path=canonical,
        domain=item.get("domain"),
        entity_type=item.get("entity_type"),
        source_role=item.get("source_role"),
        status=item.get("status"),
        do_not_answer_as_current=forbidden,
        retrieval_score=item.get("score"),
        graph_distance=item.get("graph_distance"),
        freshness=item.get("last_verified"),
        authority_class=tier,
        verdict=verdict,
        reason=reason,
        engines=_engines(item),
    )


def _route_domains(route: RegistryEntry) -> set[str]:
    return {route.domain, *route.authority_domains}


def _route_has_candidate(cards: list[CandidateCard], route: RegistryEntry) -> bool:
    owners = set(route.first_loads or [route.first_load])
    domains = _route_domains(route)
    return any(
        card.authority_class in {"current", "authority"}
        and not card.do_not_answer_as_current
        and (card.path in owners or card.canonical_path in owners or card.domain in domains)
        for card in cards
    )


def _candidate_route(cards: list[CandidateCard], catalog: Catalog) -> tuple[Optional[RegistryEntry], bool]:
    domains = {
        card.domain for card in cards
        if card.domain and card.authority_class in {"current", "authority"} and not card.do_not_answer_as_current
    }
    matches = [entry for entry in catalog.entries if domains & _route_domains(entry)]
    unique = {entry.domain: entry for entry in matches}
    if len(unique) == 1:
        return next(iter(unique.values())), False
    return None, len(unique) > 1


def _select_owner(cards: list[CandidateCard], route: Optional[RegistryEntry]) -> list[CandidateCard]:
    eligible = [card for card in cards if card.authority_class in {"current", "authority"} and not card.do_not_answer_as_current]
    selected_path: Optional[str] = None
    if route:
        owners = set(route.first_loads or [route.first_load])
        for card in eligible:
            if card.canonical_path in owners or card.path in owners:
                selected_path = card.path
                break
        if selected_path is None:
            routed = [card for card in eligible if card.domain in _route_domains(route)]
            current = [card for card in routed if card.authority_class == "current"]
            if current:
                selected_path = current[0].path
            elif routed:
                selected_path = routed[0].path
    result: list[CandidateCard] = []
    for card in cards:
        if card.path == selected_path:
            result.append(CandidateCard(**{
                **card.__dict__,
                "verdict": CandidateVerdict.SELECTED,
                "reason": "selected owner/canonical authority candidate",
            }))
        else:
            result.append(card)
    return result


def compile_answer_packet(
    query: LibrarianQuery,
    catalog: Catalog,
    results: Iterable[dict[str, Any]],
    *,
    degraded: bool = False,
    warnings: Optional[list[str]] = None,
) -> LibrarianPacket:
    """Compile deterministic retrieval results; never generate prose truth."""
    warning_list = list(warnings or [])
    raw_results = [item for item in results if item.get("path")]
    initial_cards = [_card(item) for item in raw_results]
    route = route_query(query.query, catalog, query.requested_domain)
    route_source = "explicit" if query.requested_domain else ("trigger" if route else None)
    route_ambiguous = False
    if catalog.valid and not query.requested_domain and (route is None or not _route_has_candidate(initial_cards, route)):
        fallback, route_ambiguous = _candidate_route(initial_cards, catalog)
        if fallback is not None:
            route = fallback
            route_source = "candidate_domain"
    cards = _select_owner(
        initial_cards,
        route if catalog.valid else None,
    )
    selected = [card for card in cards if card.verdict == CandidateVerdict.SELECTED]
    rejected = [card for card in cards if card.verdict == CandidateVerdict.REJECTED]
    evidence = [card for card in cards if card.verdict == CandidateVerdict.EVIDENCE_ONLY]
    has_open_conflict = any(
        item.get("conflict_open") is True or item.get("relation") == "conflicts_with"
        for item in raw_results
    )

    stop_reason: Optional[StopState] = None
    status = "ok"
    recommended_action: Optional[str] = None
    if degraded:
        status = "degraded"
        stop_reason = StopState.INDEX_DEGRADED
        recommended_action = "restore and verify the Librarian snapshot/catalog before current-answer promotion"
        selected = []
    elif not catalog.valid:
        status = "stopped"
        stop_reason = StopState.AUTHORITY_AMBIGUOUS
        recommended_action = "repair and verify the Domain Registry before routing"
        selected = []
    elif route_ambiguous:
        status = "stopped"
        stop_reason = StopState.AUTHORITY_AMBIGUOUS
        recommended_action = "clarify the candidate domain before owner selection"
        selected = []
    elif route is None:
        status = "stopped"
        stop_reason = StopState.NO_CANON_FOUND
        recommended_action = "add or clarify a Domain Registry route"
    elif not selected:
        status = "stopped"
        stop_reason = StopState.NO_CANON_FOUND
        recommended_action = "locate the routed domain owner/canonical page"
    elif has_open_conflict:
        status = "stopped"
        stop_reason = StopState.CONFLICT_OPEN
        recommended_action = "resolve the typed authority conflict before promotion"
    elif query.time_scope == "current":
        status = "stopped"
        stop_reason = StopState.FRESH_SOURCE_REQUIRED
        recommended_action = "verify current state from an approved fresh source"

    verified_paths = [card.canonical_path for card in selected] if stop_reason not in {
        StopState.INDEX_DEGRADED,
        StopState.NO_CANON_FOUND,
        StopState.AUTHORITY_AMBIGUOUS,
        StopState.CONFLICT_OPEN,
        StopState.FRESH_SOURCE_REQUIRED,
        StopState.INSUFFICIENT_EVIDENCE,
    } else []
    trail = []
    if route:
        trail.append(TrailStep(
            document=catalog.source_path,
            reason_read="route query to domain owner",
            result=f"domain={route.domain}; first_load={route.first_load}",
        ))
    for card in cards:
        trail.append(TrailStep(
            document=card.path,
            reason_read="evaluate retrieval candidate",
            result=f"{card.verdict.value}: {card.reason}",
        ))

    return LibrarianPacket(
        query=query,
        status=status,
        route={
            "selected_domain": route.domain if route else None,
            "source": route_source,
            "registry_entry": catalog.source_path if route else None,
            "first_load": route.first_load if route else None,
            "first_loads": route.first_loads if route else [],
            "authority_domains": route.authority_domains if route else [],
            "compiler": route.answer_compiler if route else None,
        },
        retrieval={
            "degraded": degraded,
            "candidate_count": len(cards),
            "engines": sorted({engine for card in cards for engine in card.engines}),
        },
        authority={
            "owner_document": selected[0].canonical_path if selected else None,
            "supporting_documents": [
                card.canonical_path for card in cards if card.verdict == CandidateVerdict.SUPPORTING
            ],
            "evidence_only": [card.canonical_path for card in evidence],
            "rejected_candidates": [card.canonical_path for card in rejected],
            "unresolved_conflicts": [],
        },
        candidates=cards,
        trail=trail,
        answer={
            "verified": verified_paths,
            "inference": [],
            "unknowns": [] if verified_paths else ["no verified current answer was compiled"],
            "fresh_verification_needed": stop_reason == StopState.FRESH_SOURCE_REQUIRED,
        },
        stop_reason=stop_reason,
        recommended_action=recommended_action,
        warnings=warning_list,
    )
