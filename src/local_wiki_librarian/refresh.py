"""Atomic maintenance operations for Librarian's derived Wiki state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .authority import compile_authority
from .lexical import WikimapAdapter
from .paths import resolve_within
from .snapshot import SnapshotResult, build_snapshot
from .vendor import bundled_vendor_root, bundled_wikimap_path, verify_vendor


@dataclass(frozen=True)
class RefreshResult:
    generation: str
    summary: dict
    authority_entries: int
    unresolved_redirects: int
    authority_sha256: str
    wikimap_index_sha256: str

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "status": "ok",
            "generation": self.generation,
            "summary": self.summary,
            "authority_entries": self.authority_entries,
            "unresolved_redirects": self.unresolved_redirects,
            "authority_sha256": self.authority_sha256,
            "wikimap_index_sha256": self.wikimap_index_sha256,
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _find_wikimap_path() -> Path | None:
    """Locate vendored Wikimap in a source tree or installed wheel."""
    configured = os.environ.get("LIBRARIAN_WIKIMAP_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    try:
        vendor_root = bundled_vendor_root()
        verify_vendor(vendor_root)
        return bundled_wikimap_path()
    except (FileNotFoundError, OSError, RuntimeError):
        return None


def _write_fsync(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_atomic(path: Path, payload: bytes) -> None:
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        _write_fsync(tmp, payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _quick_check_index(index_path: Path) -> None:
    if not index_path.is_file():
        raise RuntimeError("wikimap index was not created")
    try:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("wikimap index integrity check failed") from exc
    if not result or result[0] != "ok":
        raise RuntimeError("wikimap index integrity check failed")


def _semantic_index_sha256(index_path: Path) -> str:
    """Hash query-relevant Wikimap rows while ignoring volatile SQLite bytes."""
    queries = (
        "SELECT path, sha, title, words FROM files ORDER BY path",
        "SELECT path, line, level, heading, content FROM sections ORDER BY path, line, level, heading, content",
        "SELECT rowid, path, line, heading, content FROM sections_fts ORDER BY rowid",
        "SELECT src, dst, kind FROM links ORDER BY src, dst, kind",
        "SELECT path, tag FROM tags ORDER BY path, tag",
        "SELECT path, alias FROM aliases ORDER BY path, alias",
        "SELECT src, dst, alt FROM img_alts ORDER BY src, dst, alt",
        "SELECT src, dst, relation, rationale, origin, created, src_sha, dst_sha FROM edges "
        "ORDER BY src, dst, relation, rationale, origin, created, src_sha, dst_sha",
        "SELECT path, sha, vec FROM embeds ORDER BY path",
        "SELECT question, insight, created, sources FROM notes ORDER BY question, insight, created, sources",
    )
    digest = hashlib.sha256()
    try:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            for query in queries:
                digest.update(query.encode("utf-8"))
                for row in connection.execute(query):
                    digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                    digest.update(b"\n")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("wikimap semantic index verification failed") from exc
    return digest.hexdigest()


def _authority_bytes(snapshot_dir: Path, generation: str) -> tuple[bytes, int, int]:
    authority = compile_authority(snapshot_dir, generation)
    if authority.unresolved_redirects:
        raise RuntimeError("authority compilation has unresolved redirects")
    payload = json.dumps(
        authority.to_dict(), indent=2, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return payload, len(authority.entries), len(authority.unresolved_redirects)


def refresh_wiki(canonical_root: Path, state_root: Path, wikimap_path: Path) -> RefreshResult:
    """Build authority and Wikimap sidecars before one pointer promotion."""
    canonical_root = Path(canonical_root)
    state_root = Path(state_root)
    wikimap_path = Path(wikimap_path)
    if not wikimap_path.is_file():
        raise FileNotFoundError("wikimap runtime not found")

    prepared: dict[str, Any] = {}

    def prepare(snapshot_dir: Path, manifest: dict) -> dict:
        generation = str(manifest["generation"])
        summary = manifest.get("summary", {})
        stale = int(summary.get("stale", 0) or 0)
        quarantined = int(summary.get("quarantined", 0) or 0)
        deleted = int(summary.get("deleted", 0) or 0)
        if stale or quarantined or deleted:
            raise RuntimeError(
                "refresh requires a fully fresh canonical read: "
                f"stale={stale}, quarantined={quarantined}, deleted={deleted}"
            )
        authority_payload, entry_count, unresolved = _authority_bytes(snapshot_dir, generation)
        authority_path = snapshot_dir / "authority.json"
        _write_fsync(authority_path, authority_payload)
        WikimapAdapter(wikimap_path, snapshot_dir).index()
        index_path = snapshot_dir / ".wikimap" / "index.db"
        _quick_check_index(index_path)
        index_payload = index_path.read_bytes()
        prepared.update({
            "authority_payload": authority_payload,
            "authority_entries": entry_count,
            "unresolved_redirects": unresolved,
            "authority_sha256": _sha256(authority_payload),
            "wikimap_index_sha256": _sha256(index_payload),
        })
        return {
            "authority_sha256": prepared["authority_sha256"],
            "wikimap_index_sha256": prepared["wikimap_index_sha256"],
        }

    snapshot: SnapshotResult = build_snapshot(
        canonical_root, state_root, prepare_generation=prepare,
    )
    # Compatibility-only projection. Runtime authority comes from the promoted
    # generation, so a crash here cannot create a mixed generation.
    _write_atomic(state_root / "authority.json", prepared["authority_payload"])
    return RefreshResult(
        generation=snapshot.generation,
        summary=snapshot.summary,
        authority_entries=int(prepared["authority_entries"]),
        unresolved_redirects=int(prepared["unresolved_redirects"]),
        authority_sha256=str(prepared["authority_sha256"]),
        wikimap_index_sha256=str(prepared["wikimap_index_sha256"]),
    )


def _verified_pointer_for_generation(
    state_root: Path, generation: str, wikimap_path: Path | None = None,
) -> tuple[dict, bytes]:
    current = json.loads(resolve_within(state_root, "current.json").read_text(encoding="utf-8"))
    if current.get("previous_generation") != generation:
        raise RuntimeError("requested rollback generation is not the trusted previous generation")
    snapshots = resolve_within(state_root, "snapshots")
    snapshot_dir = resolve_within(snapshots, generation)
    manifest_payload = resolve_within(snapshot_dir, "manifest.json").read_bytes()
    manifest = json.loads(manifest_payload)
    if manifest.get("schema_version") != 1 or manifest.get("generation") != generation:
        raise RuntimeError("generation manifest identity mismatch")
    if current.get("previous_manifest_sha256") != _sha256(manifest_payload):
        raise RuntimeError("previous generation manifest is not integrity-bound")
    usable: set[str] = set()
    payloads: dict[str, bytes] = {}
    for entry in manifest.get("files", []):
        relative = entry.get("relative_path")
        if not isinstance(relative, str):
            raise RuntimeError("generation manifest entry is malformed")
        path = resolve_within(snapshot_dir, relative)
        if entry.get("state") in {"copied", "stale"}:
            payload = path.read_bytes()
            if len(payload) != entry.get("size") or _sha256(payload) != entry.get("sha256"):
                raise RuntimeError("snapshot file integrity mismatch")
            usable.add(relative)
            payloads[relative] = payload
    actual = {
        path.relative_to(snapshot_dir).as_posix()
        for path in snapshot_dir.rglob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    if actual != usable:
        raise RuntimeError("snapshot Markdown inventory differs from generation manifest")
    authority_payload = resolve_within(snapshot_dir, "authority.json").read_bytes()
    if current.get("previous_authority_sha256") != _sha256(authority_payload):
        raise RuntimeError("previous generation authority is not integrity-bound")
    authority = json.loads(authority_payload)
    if authority.get("generation") != generation or authority.get("unresolved_redirects"):
        raise RuntimeError("generation authority is not rollback-safe")
    index_path = resolve_within(snapshot_dir, ".wikimap/index.db")
    index_payload = index_path.read_bytes()
    if current.get("previous_wikimap_index_sha256") != _sha256(index_payload):
        raise RuntimeError("previous generation Wikimap index is not integrity-bound")
    _quick_check_index(index_path)

    runtime = Path(wikimap_path) if wikimap_path is not None else _find_wikimap_path()
    if runtime is None or not runtime.is_file():
        raise FileNotFoundError("packaged wikimap runtime not found")
    with tempfile.TemporaryDirectory(prefix="librarian-rollback-verify-") as temp:
        verified_view = Path(temp) / "snapshot"
        verified_view.mkdir()
        for relative, payload in payloads.items():
            target = resolve_within(verified_view, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        rebuilt_authority, _, _ = _authority_bytes(verified_view, generation)
        if rebuilt_authority != authority_payload:
            raise RuntimeError("generation authority differs from verified snapshot compilation")
        WikimapAdapter(runtime, verified_view).index()
        rebuilt_index = resolve_within(verified_view, ".wikimap/index.db")
        _quick_check_index(rebuilt_index)
        if _semantic_index_sha256(rebuilt_index) != _semantic_index_sha256(index_path):
            raise RuntimeError("generation Wikimap index differs from verified snapshot rebuild")

    pointer = {
        **manifest,
        "manifest_sha256": _sha256(manifest_payload),
        "authority_sha256": _sha256(authority_payload),
        "wikimap_index_sha256": _sha256(index_payload),
    }
    return pointer, authority_payload


def rollback_generation(state_root: Path, generation: str) -> RefreshResult:
    """Atomically repoint to a fully verified prior Librarian generation."""
    state_root = Path(state_root)
    current_path = state_root / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    previous = str(current.get("generation", ""))
    pointer, authority_payload = _verified_pointer_for_generation(state_root, generation)
    if previous:
        pointer["previous_generation"] = previous
        for key in ("manifest_sha256", "authority_sha256", "wikimap_index_sha256"):
            value = current.get(key)
            if value is None:
                raise RuntimeError(f"current generation lacks trusted {key}")
            pointer[f"previous_{key}"] = value
    pointer_payload = json.dumps(
        pointer, indent=2, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    _write_atomic(current_path, pointer_payload)
    _write_atomic(state_root / "authority.json", authority_payload)
    authority = json.loads(authority_payload)
    return RefreshResult(
        generation=generation,
        summary=dict(pointer.get("summary", {})),
        authority_entries=len(authority.get("entries", [])),
        unresolved_redirects=len(authority.get("unresolved_redirects", [])),
        authority_sha256=str(pointer["authority_sha256"]),
        wikimap_index_sha256=str(pointer["wikimap_index_sha256"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain derived Wiki Librarian state")
    parser.add_argument("--wiki-root", type=Path)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--wikimap-path", type=Path)
    parser.add_argument("--rollback-generation")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.rollback_generation:
            result = rollback_generation(args.state_root, args.rollback_generation)
        else:
            if args.wiki_root is None:
                raise ValueError("--wiki-root is required for refresh")
            wikimap_path = args.wikimap_path or _find_wikimap_path()
            if wikimap_path is None:
                raise FileNotFoundError("packaged wikimap runtime not found")
            result = refresh_wiki(args.wiki_root, args.state_root, wikimap_path)
        data = result.to_dict()
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        data = {
            "schema_version": 1,
            "status": "error",
            "error": "wiki maintenance failed",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if data["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())