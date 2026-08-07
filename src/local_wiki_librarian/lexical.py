"""Read-only wikimap lexical search adapter.

Wraps the vendored ``wikimap.py`` CLI in a safe read-only interface that:

- Operates on a **snapshot generation directory** (never canonical wiki)
- Allows only ``update --no-map``, ``search``, and ``links``/``path`` subcommands
- Blocks ``install``, ``mv``, ``link``, ``note``, ``edge``, ``embed``, ``migrate``,
  and any write to the vault
- Forces ``--no-map`` to prevent ``MAP.md`` generation inside the snapshot
- Uses ``subprocess`` with bounded timeout and captures stdout/stderr
- Parses ``--json`` output into structured Python objects
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

__all__ = [
    "WikimapAdapterError",
    "SearchResult",
    "WikimapAdapter",
    "ALLOWED_COMMANDS",
    "BLOCKED_COMMANDS",
]

# Only these wikimap subcommands are safe for read-only use
ALLOWED_COMMANDS = frozenset({"update", "search", "links", "path"})
BLOCKED_COMMANDS = frozenset({
    "install", "map", "mv", "fix-links", "link", "note", "notes",
    "import-graphify", "migrate", "suggest", "embed", "semsearch",
    "edge", "edges",
})


class WikimapAdapterError(RuntimeError):
    """Raised when the wikimap adapter encounters an error."""


@dataclass(frozen=True)
class SearchResult:
    """One search hit from wikimap."""

    file: str
    line: int
    title: str
    score: float
    snippet: str = ""
    tags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class WikimapAdapter:
    """Safe read-only adapter around the vendored wikimap CLI.

    Attributes:
        wikimap_path: Path to the packaged ``_vendor/wikimap/wikimap.py``.
        snapshot_dir: The snapshot generation directory to search.
        timeout: Maximum seconds for any single wikimap invocation.
    """

    wikimap_path: Path
    snapshot_dir: Path
    timeout: int = 120
    last_weak: bool = field(init=False, default=False)
    last_partial: bool = field(init=False, default=False)
    last_terms: list[dict[str, object]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.wikimap_path = Path(self.wikimap_path)
        self.snapshot_dir = Path(self.snapshot_dir)
        if not self.wikimap_path.is_file():
            raise WikimapAdapterError(
                f"wikimap.py not found: {self.wikimap_path}"
            )
        if not self.snapshot_dir.is_dir():
            raise WikimapAdapterError(
                f"snapshot directory not found: {self.snapshot_dir}"
            )

    def _run(
        self,
        command: str,
        args: Sequence[str],
        *,
        json_output: bool = False,
    ) -> str:
        """Execute a wikimap subcommand with safety guards.

        Args:
            command: The wikimap subcommand (must be in ALLOWED_COMMANDS).
            args: Additional arguments to the subcommand.
            json_output: If True, append ``--json`` flag.

        Returns:
            stdout as a string.

        Raises:
            WikimapAdapterError: On blocked command, timeout, or non-zero exit.
        """
        if command in BLOCKED_COMMANDS:
            raise WikimapAdapterError(
                f"blocked wikimap command: {command} (write/mutation not allowed)"
            )
        if command not in ALLOWED_COMMANDS:
            raise WikimapAdapterError(
                f"unknown or disallowed wikimap command: {command}"
            )

        cmd = [
            sys.executable,
            str(self.wikimap_path),
            "--root",
            str(self.snapshot_dir),
            command,
        ]

        if command == "update":
            # Force --no-map to prevent MAP.md generation
            cmd.append("--no-map")
        elif json_output and command in ("search", "links", "path"):
            cmd.append("--json")

        cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WikimapAdapterError(
                f"wikimap {command} timed out after {self.timeout}s"
            ) from exc

        if result.returncode != 0:
            raise WikimapAdapterError(
                f"wikimap {command} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()[:500]}"
            )

        return result.stdout

    def index(self) -> dict:
        """Run ``wikimap update --no-map`` to build the lexical index.

        Returns:
            Parsed stdout (plain text summary).
        """
        output = self._run("update", [])
        # Verify no MAP.md was created
        map_path = self.snapshot_dir / "MAP.md"
        if map_path.exists():
            raise WikimapAdapterError(
                "SAFETY VIOLATION: MAP.md was created in snapshot despite --no-map"
            )
        # .wikimap directory is expected (index storage) but MAP.md is not
        return {"raw": output.strip()}

    def search(self, query: str, n: int = 10) -> list[SearchResult]:
        """Run ``wikimap search`` and parse results.

        Args:
            query: Search query string.
            n: Maximum number of results.

        Returns:
            List of SearchResult objects.
        """
        output = self._run("search", ["-n", str(n), "--json", query])
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise WikimapAdapterError(
                f"failed to parse wikimap search JSON: {exc}"
            ) from exc

        if (
            not isinstance(data, dict)
            or data.get("query") != query
            or not isinstance(data.get("results"), list)
            or not isinstance(data.get("terms"), list)
            or not isinstance(data.get("weak"), bool)
            or not isinstance(data.get("partial"), bool)
        ):
            raise WikimapAdapterError("wikimap search returned an invalid response schema")

        results: list[SearchResult] = []
        self.last_weak = data["weak"]
        self.last_partial = data["partial"]
        self.last_terms = [item for item in data["terms"] if isinstance(item, dict)]
        for item in data["results"]:
            if not isinstance(item, dict):
                continue
            matched = item.get("matched", [])
            snippet = matched[0] if isinstance(matched, list) and matched else ""
            results.append(SearchResult(
                file=item.get("path", item.get("file", "")),
                line=item.get("line", 0),
                title=item.get("heading", item.get("title", "")),
                score=float(item.get("score", 0.0)),
                snippet=snippet,
                tags=item.get("tags", []),
                related=item.get("related", []),
            ))
        return results

    def search_text(self, query: str, n: int = 10) -> str:
        """Run ``wikimap search`` and return raw text output (no JSON parsing)."""
        return self._run("search", ["-n", str(n), query])
