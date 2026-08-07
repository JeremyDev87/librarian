"""Read-only wikimap graph traversal adapter.

Wraps ``wikimap links`` and ``wikimap path`` commands for safe read-only
graph exploration over a snapshot generation directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .lexical import WikimapAdapter, WikimapAdapterError

__all__ = [
    "LinkInfo",
    "PathResult",
    "GraphAdapter",
]


@dataclass(frozen=True)
class LinkInfo:
    """Outlink/backlink information for a document."""

    target: str
    title: str = ""
    line: int = 0


@dataclass(frozen=True)
class PathResult:
    """Shortest path between two documents."""

    src: str
    dst: str
    hops: list[str]
    found: bool


class GraphAdapter:
    """Safe read-only graph traversal via wikimap links/path.

    Wraps an existing :class:`WikimapAdapter` instance.
    """

    def __init__(self, adapter: WikimapAdapter) -> None:
        self._adapter = adapter

    def links(self, target: str) -> dict[str, list[LinkInfo]]:
        """Get outlinks and backlinks for a document.

        Args:
            target: Document path or REQ-ID.

        Returns:
            Dict with ``outlinks`` and ``backlinks`` keys.
        """
        try:
            output = self._adapter._run("links", ["--json", target])
        except WikimapAdapterError as exc:
            # A missing document is a valid empty graph node.  Preserve that
            # public contract while allowing every other adapter failure to
            # reach the router's explicit warning channel.
            if str(exc).startswith("wikimap links exited 1: not found:"):
                return {"outlinks": [], "backlinks": []}
            raise
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise WikimapAdapterError(
                f"failed to parse wikimap links JSON: {exc}"
            ) from exc

        result: dict[str, list[LinkInfo]] = {"outlinks": [], "backlinks": []}
        if isinstance(data, dict):
            for direction in ("outlinks", "backlinks"):
                items = data.get(direction, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            result[direction].append(LinkInfo(
                                target=item.get("target", item.get("file", "")),
                                title=item.get("title", ""),
                                line=item.get("line", 0),
                            ))
                        elif isinstance(item, str):
                            result[direction].append(LinkInfo(target=item))
        return result

    def path(self, src: str, dst: str) -> PathResult:
        """Find shortest link path between two documents.

        Args:
            src: Source document path.
            dst: Destination document path.

        Returns:
            PathResult with the path hops or ``found=False``.
        """
        try:
            output = self._adapter._run("path", ["--json", src, dst])
        except WikimapAdapterError:
            # wikimap returns non-zero when no path found or target missing
            return PathResult(src=src, dst=dst, hops=[], found=False)
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            # wikimap may output plain text if no path found
            return PathResult(src=src, dst=dst, hops=[], found=False)

        if isinstance(data, dict):
            hops = data.get("path", [])
            return PathResult(
                src=src,
                dst=dst,
                hops=hops if isinstance(hops, list) else [],
                found=bool(hops),
            )
        return PathResult(src=src, dst=dst, hops=[], found=False)
