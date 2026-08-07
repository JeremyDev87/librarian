from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import local_wiki_librarian.cli as cli_module
from local_wiki_librarian.cli import _audit, _health, load_runtime
from local_wiki_librarian.lexical import WikimapAdapter
from local_wiki_librarian.manifest import build_authority_manifest
from local_wiki_librarian.refresh import refresh_wiki
from local_wiki_librarian.snapshot import build_snapshot
from local_wiki_librarian.vendor import bundled_wikimap_path


def test_audit_always_emits_common_status_contract(tmp_path) -> None:
    report = _audit(tmp_path / "missing-state")
    assert report["status"] == "degraded"
    assert report["degraded"] is True
    assert report["warnings"]


def test_runtime_rejects_snapshot_and_authority_tampering(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    page = wiki / "metadata/README.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nstatus: active\n---\n# Owner", encoding="utf-8")
    build_snapshot(wiki, state)
    build_authority_manifest(state)
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    snapshot_page = state / "snapshots" / current["generation"] / "metadata/README.md"
    snapshot_page.write_text("---\nstatus: active\ndomain: forged\n---\n# Owner", encoding="utf-8")
    authority = json.loads((state / "authority.json").read_text(encoding="utf-8"))
    authority["entries"][0]["domain"] = "forged"
    (state / "authority.json").write_text(json.dumps(authority), encoding="utf-8")

    with pytest.raises(RuntimeError, match="snapshot file integrity"):
        load_runtime(state)


def test_runtime_uses_verified_bytes_if_shared_generation_changes_before_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    page = wiki / "metadata/README.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nstatus: active\ndomain: original\n---\n# Original\n",
        encoding="utf-8",
    )
    build_snapshot(wiki, state)
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    shared_page = state / "snapshots" / current["generation"] / "metadata/README.md"
    real_compile = cli_module.compile_authority

    def mutate_shared_then_compile(snapshot_dir: Path, generation: str):
        shared_page.write_text(
            "---\nstatus: active\ndomain: forged\n---\n# Forged\n",
            encoding="utf-8",
        )
        return real_compile(snapshot_dir, generation)

    monkeypatch.setattr(cli_module, "compile_authority", mutate_shared_then_compile)
    verified_dir, authority, _ = load_runtime(state)

    assert authority.entries[0].domain == "original"
    assert "# Original" in (verified_dir / "metadata/README.md").read_text(encoding="utf-8")
    assert "# Forged" in shared_page.read_text(encoding="utf-8")


def test_health_fails_when_librarian_state_pointer_is_not_a_usable_runtime(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "current.json").write_text("{}", encoding="utf-8")

    report = _health(state)

    assert report["status"] == "error"
    assert report["degraded"] is False
    assert report["state_available"] is True
    assert report["librarian_available"] is False


@pytest.mark.parametrize(
    "source",
    [
        "raise RuntimeError('unusable')\n",
        "print('not-json')\n",
        "print('{}')\n",
    ],
)
def test_health_fails_when_configured_wikimap_file_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    page = wiki / "notes/policy.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nauthority: high\nstatus: active\n---\n# Policy\n", encoding="utf-8")
    refresh_wiki(wiki, state, bundled_wikimap_path())
    unusable = tmp_path / "unusable-wikimap.py"
    unusable.write_text(source, encoding="utf-8")
    monkeypatch.setenv("LIBRARIAN_WIKIMAP_PATH", str(unusable))

    report = _health(state)
    audit = _audit(state)

    assert report["status"] == "error"
    assert report["librarian_available"] is False
    assert report["wikimap_available"] is False
    assert report["operational_backends"] == []
    assert audit["status"] == "degraded"
    assert audit["passed"] is False


def test_health_fails_when_configured_wikimap_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    page = wiki / "notes/policy.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nauthority: high\nstatus: active\n---\n# Policy\n", encoding="utf-8")
    refresh_wiki(wiki, state, bundled_wikimap_path())
    unusable = tmp_path / "slow-wikimap.py"
    unusable.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    monkeypatch.setenv("LIBRARIAN_WIKIMAP_PATH", str(unusable))

    report = _health(state)

    assert report["status"] == "error"
    assert report["librarian_available"] is False
    assert report["operational_backends"] == []


def test_health_is_degraded_when_promoted_snapshot_contains_stale_bytes(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    page = wiki / "metadata/README.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nstatus: active\n---\n# Owner", encoding="utf-8")
    build_snapshot(wiki, state)
    page.chmod(0o000)
    try:
        build_snapshot(wiki, state)
    finally:
        page.chmod(0o644)
    build_authority_manifest(state)
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    snapshot = state / "snapshots" / current["generation"]
    WikimapAdapter(bundled_wikimap_path(), snapshot).index()
    index_path = snapshot / ".wikimap" / "index.db"
    current["wikimap_index_sha256"] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    (state / "current.json").write_text(
        json.dumps(current, sort_keys=True), encoding="utf-8"
    )

    report = _health(state)

    assert report["status"] == "degraded"
    assert report["degraded"] is True
    assert report["snapshot_state_counts"]["stale"] == 1
    assert "stale=1" in report["warnings"][0]


def test_cli_exposes_only_local_read_only_commands() -> None:
    choices = next(
        action.choices for action in cli_module.build_parser()._actions
        if action.dest == "command"
    )
    assert choices is not None
    assert set(choices) == {
        "health", "search", "ask", "locate", "trace", "audit",
    }


def test_cli_discovers_packaged_wikimap() -> None:
    path = cli_module._find_wikimap_path()

    assert path is not None
    assert path.name == "wikimap.py"
