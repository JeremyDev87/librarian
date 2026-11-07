from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import local_wiki_librarian.refresh as refresh_module
from local_wiki_librarian.cli import _audit, _health, _search, load_runtime
from local_wiki_librarian.refresh import (
    build_root_migration_receipt,
    refresh_wiki,
    rollback_generation,
    stage_refresh,
)

ROOT = Path(__file__).resolve().parents[2]
WIKIMAP = ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap" / "wikimap.py"


def _write_wiki(root: Path, title: str) -> None:
    page = root / "knowledge/policies/policy.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\nstatus: active\nauthority: high\ndomain: wiki\n---\n"
        f"# {title}\natomic wikimap refresh policy\n",
        encoding="utf-8",
    )


def _metadata_digest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        rows.append((path.relative_to(root).as_posix(), stat.st_mode, stat.st_size, stat.st_mtime_ns,
                     hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def test_refresh_promotes_complete_generation_and_preserves_source(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    before = _metadata_digest(wiki)

    result = refresh_wiki(wiki, state, WIKIMAP)

    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    generation = state / "snapshots" / result.generation
    assert current["generation"] == result.generation
    assert current["authority_sha256"] == hashlib.sha256((generation / "authority.json").read_bytes()).hexdigest()
    assert current["wikimap_index_sha256"] == hashlib.sha256((generation / ".wikimap/index.db").read_bytes()).hexdigest()
    assert (generation / "authority.json").is_file()
    assert (generation / ".wikimap/index.db").is_file()
    assert _metadata_digest(wiki) == before
    assert not (wiki / ".wikimap").exists()
    assert not (wiki / "MAP.md").exists()

    health = _health(state)
    search = _search("atomic wikimap refresh", state, 5)
    assert health["status"] == "ok"
    assert search["status"] == "ok"
    assert search["results"][0]["path"] == "knowledge/policies/policy.md"
    assert _audit(state)["passed"] is True


def test_refresh_cli_discovers_vendored_wikimap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "CLI Discovery")
    monkeypatch.setattr(refresh_module, "_find_wikimap_path", lambda: WIKIMAP)

    exit_code = refresh_module.main([
        "--wiki-root", str(wiki), "--state-root", str(state),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert (state / "current.json").is_file()


def test_refresh_discovery_rejects_invalid_bundled_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIBRARIAN_WIKIMAP_PATH", raising=False)
    monkeypatch.setattr(refresh_module, "bundled_vendor_root", lambda: tmp_path)

    assert refresh_module._find_wikimap_path() is None


def test_root_migration_rejects_extra_receipt_key_before_staging(tmp_path: Path) -> None:
    old_wiki = tmp_path / "old-wiki"
    new_wiki = tmp_path / "new-wiki"
    state = tmp_path / "state"
    _write_wiki(old_wiki, "Version One")
    refresh_wiki(old_wiki, state, WIKIMAP)
    _write_wiki(new_wiki, "Version Two")
    receipt = build_root_migration_receipt(state, new_wiki)
    receipt["unexpected"] = "must fail before generation creation"
    current_before = (state / "current.json").read_bytes()
    generations_before = {path.name for path in (state / "snapshots").iterdir()}

    with pytest.raises(Exception, match="receipt schema mismatch"):
        stage_refresh(new_wiki, state, WIKIMAP, migration_receipt=receipt)

    assert (state / "current.json").read_bytes() == current_before
    assert {path.name for path in (state / "snapshots").iterdir()} == generations_before


def test_refresh_failure_preserves_current_and_removes_incomplete_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    current_before = (state / "current.json").read_bytes()
    generations_before = {p.name for p in (state / "snapshots").iterdir()}
    _write_wiki(wiki, "Version Two")

    def fail_index(_self):
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(refresh_module.WikimapAdapter, "index", fail_index)
    with pytest.raises(Exception, match="generation preparation failed"):
        refresh_wiki(wiki, state, WIKIMAP)

    assert (state / "current.json").read_bytes() == current_before
    assert {p.name for p in (state / "snapshots").iterdir()} == generations_before == {first.generation}


def test_refresh_refuses_stale_promotion_and_preserves_last_good_generation(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    current_before = (state / "current.json").read_bytes()
    generations_before = {path.name for path in (state / "snapshots").iterdir()}
    page = wiki / "knowledge/policies/policy.md"
    page.chmod(0o000)
    try:
        with pytest.raises(Exception, match="generation preparation failed"):
            refresh_wiki(wiki, state, WIKIMAP)
    finally:
        page.chmod(0o644)

    assert (state / "current.json").read_bytes() == current_before
    assert {path.name for path in (state / "snapshots").iterdir()} == generations_before == {first.generation}


def test_verified_runtime_rejects_promoted_index_tampering(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    result = refresh_wiki(wiki, state, WIKIMAP)
    index = state / "snapshots" / result.generation / ".wikimap/index.db"
    index.chmod(0o644)
    index.write_bytes(index.read_bytes() + b"tamper")

    with pytest.raises(RuntimeError, match="wikimap index integrity"):
        load_runtime(state)


def test_two_generation_rollback_restores_previous_search(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    state = tmp_path / "state"
    _write_wiki(wiki, "Version One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    _write_wiki(wiki, "Version Two")
    second = refresh_wiki(wiki, state, WIKIMAP)
    current = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert current["generation"] == second.generation
    assert current["previous_generation"] == first.generation
    first_dir = state / "snapshots" / first.generation
    assert current["previous_manifest_sha256"] == hashlib.sha256(
        (first_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert current["previous_authority_sha256"] == hashlib.sha256(
        (first_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert current["previous_wikimap_index_sha256"] == hashlib.sha256(
        (first_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    rolled_back = rollback_generation(state, first.generation)
    assert rolled_back.generation == first.generation
    after = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert after["generation"] == first.generation
    assert after["previous_generation"] == second.generation
    second_dir = state / "snapshots" / second.generation
    assert after["previous_manifest_sha256"] == hashlib.sha256(
        (second_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert after["previous_authority_sha256"] == hashlib.sha256(
        (second_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert after["previous_wikimap_index_sha256"] == hashlib.sha256(
        (second_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    reversed_rollback = rollback_generation(state, second.generation)
    assert reversed_rollback.generation == second.generation
    reversed_pointer = json.loads((state / "current.json").read_text(encoding="utf-8"))
    assert reversed_pointer["generation"] == second.generation
    assert reversed_pointer["previous_generation"] == first.generation
    assert reversed_pointer["previous_manifest_sha256"] == hashlib.sha256(
        (first_dir / "manifest.json").read_bytes()
    ).hexdigest()
    assert reversed_pointer["previous_authority_sha256"] == hashlib.sha256(
        (first_dir / "authority.json").read_bytes()
    ).hexdigest()
    assert reversed_pointer["previous_wikimap_index_sha256"] == hashlib.sha256(
        (first_dir / ".wikimap/index.db").read_bytes()
    ).hexdigest()

    search = _search("Version Two", state, 5)
    assert search["status"] == "ok"
    assert search["results"][0]["path"] == "knowledge/policies/policy.md"


@pytest.mark.parametrize("operation", ["refresh", "receipt"])
def test_boundary_incomplete_discovery_refuses_refresh_and_receipt(tmp_path, monkeypatch, operation):
    import local_wiki_librarian.snapshot as snapshot
    wiki, state, new = tmp_path / "wiki", tmp_path / "state", tmp_path / "new"
    _write_wiki(wiki, "Old")
    refresh_wiki(wiki, state, WIKIMAP)
    _write_wiki(new, "New")
    before = (state / "current.json").read_bytes()

    def incomplete_walk(root, **kwargs):
        yield str(root / "knowledge/policies"), [], ["policy.md"]
        if kwargs.get("onerror"):
            kwargs["onerror"](PermissionError("injected subtree failure"))

    monkeypatch.setattr(snapshot.os, "walk", incomplete_walk)
    with pytest.raises(RuntimeError):
        if operation == "receipt":
            build_root_migration_receipt(state, new)
        else:
            refresh_wiki(wiki, state, WIKIMAP)
    assert (state / "current.json").read_bytes() == before


def test_boundary_predictable_atomic_temp_link_is_ignored(tmp_path):
    import os
    target, sentinel = tmp_path / "current.json", tmp_path / "sentinel"
    target.write_bytes(b"OLD")
    sentinel.write_bytes(b"SENTINEL")
    before = sentinel.stat()
    old_temp = tmp_path / f".current.json.{os.getpid()}.tmp"
    old_temp.symlink_to(sentinel)
    from local_wiki_librarian.paths import write_bytes_atomic
    write_bytes_atomic(tmp_path, "current.json", b"NEW")
    assert target.read_bytes() == b"NEW"
    assert sentinel.read_bytes() == b"SENTINEL"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns
    assert old_temp.is_symlink()


@pytest.mark.parametrize("name", ["current.json", ".librarian-snapshot.lock"])
def test_boundary_refresh_state_link_is_refused(tmp_path, name):
    state, sentinel = tmp_path / "state", tmp_path / "sentinel"
    state.mkdir()
    sentinel.write_bytes(b'{"generation":"external"}')
    before = sentinel.stat()
    (state / name).symlink_to(sentinel)
    from local_wiki_librarian.paths import UnsafePathError
    with pytest.raises((RuntimeError, UnsafePathError)):
        if name == "current.json":
            refresh_module.current_pointer_sha256(state)
        else:
            with refresh_module._writer_lock(state):
                pytest.fail("unsafe lock acquired")
    assert sentinel.read_bytes() == b'{"generation":"external"}'
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns


def test_boundary_legacy_authority_link_preserves_sentinel(tmp_path):
    from local_wiki_librarian.manifest import build_authority_manifest
    from local_wiki_librarian.paths import UnsafePathError
    from local_wiki_librarian.snapshot import build_snapshot
    wiki, state, sentinel = tmp_path / "wiki", tmp_path / "state", tmp_path / "sentinel"
    _write_wiki(wiki, "Policy")
    build_snapshot(wiki, state)
    sentinel.write_bytes(b"SENTINEL")
    before = sentinel.stat()
    (state / "authority.json").symlink_to(sentinel)
    with pytest.raises(UnsafePathError):
        build_authority_manifest(state)
    assert sentinel.read_bytes() == b"SENTINEL"
    assert sentinel.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("operation", ["snapshot", "refresh", "receipt"])
def test_boundary_real_unreadable_subtree_refuses_partial_input(tmp_path, operation):
    import os
    from local_wiki_librarian.snapshot import build_snapshot
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX read permissions; deterministic onerror cases still run")
    wiki, state, new = tmp_path / "wiki", tmp_path / "state", tmp_path / "new"
    _write_wiki(wiki, "Policy")
    _write_wiki(new, "New")
    if operation != "snapshot":
        refresh_wiki(wiki, state, WIKIMAP)
    root = new if operation == "receipt" else wiki
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "page.md").write_text("unreadable")
    before = (state / "current.json").read_bytes() if operation != "snapshot" else None
    blocked.chmod(0)
    try:
        with pytest.raises(RuntimeError):
            if operation == "receipt":
                build_root_migration_receipt(state, new)
            elif operation == "refresh":
                refresh_wiki(wiki, state, WIKIMAP)
            else:
                build_snapshot(wiki, state)
    finally:
        blocked.chmod(0o755)
    if before is not None:
        assert (state / "current.json").read_bytes() == before
    else:
        assert not (state / "current.json").exists()


def test_boundary_receipt_rejects_source_replaced_after_discovery(tmp_path, monkeypatch):
    from local_wiki_librarian.paths import UnsafePathError
    wiki, state, new, external = (tmp_path / name for name in ("wiki", "state", "new", "external"))
    _write_wiki(wiki, "Policy")
    refresh_wiki(wiki, state, WIKIMAP)
    _write_wiki(new, "New")
    external.write_bytes(b"EXTERNAL")
    before = (state / "current.json").read_bytes()
    original = refresh_module.discover_markdown

    def swap(root):
        entries = original(root)
        entries[0].absolute_path.unlink()
        entries[0].absolute_path.symlink_to(external)
        return entries

    monkeypatch.setattr(refresh_module, "discover_markdown", swap)
    with pytest.raises(UnsafePathError):
        build_root_migration_receipt(state, new)
    assert (state / "current.json").read_bytes() == before
    assert external.read_bytes() == b"EXTERNAL"


@pytest.mark.parametrize("phase", ["replace", "directory_fsync", "readback"])
def test_boundary_refresh_pointer_failure_preserves_complete_generations(tmp_path, monkeypatch, phase):
    import local_wiki_librarian.paths as paths
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _write_wiki(wiki, "One")
    first = refresh_wiki(wiki, state, WIKIMAP)
    before = (state / "current.json").read_bytes()
    _write_wiki(wiki, "Two")
    source_before = _metadata_digest(wiki)
    original_replace, original_fsync, original_read = paths.os.replace, paths.os.fsync, paths.read_bytes_within
    replaced = False

    def replace(source, destination, **kwargs):
        nonlocal replaced
        if destination == "current.json" and phase == "replace":
            raise OSError("before pointer replace")
        result = original_replace(source, destination, **kwargs)
        if destination == "current.json":
            replaced = True
        return result

    def fsync(descriptor):
        if replaced and phase == "directory_fsync":
            raise OSError("after pointer replace fsync")
        return original_fsync(descriptor)

    def read(root, relative):
        if replaced and phase == "readback" and str(relative) == "current.json":
            raise OSError("after pointer replace readback")
        return original_read(root, relative)

    monkeypatch.setattr(paths.os, "replace", replace)
    monkeypatch.setattr(paths.os, "fsync", fsync)
    monkeypatch.setattr(paths, "read_bytes_within", read)
    with pytest.raises(OSError):
        refresh_wiki(wiki, state, WIKIMAP)
    after = (state / "current.json").read_bytes()
    pointer = json.loads(after)
    generation = state / "snapshots" / pointer["generation"]
    for relative, key in (("manifest.json", "manifest_sha256"), ("authority.json", "authority_sha256"), (".wikimap/index.db", "wikimap_index_sha256")):
        assert hashlib.sha256((generation / relative).read_bytes()).hexdigest() == pointer[key]
    assert (state / "snapshots" / first.generation / "manifest.json").is_file()
    assert _metadata_digest(wiki) == source_before
    assert (after == before) == (phase == "replace")


def test_boundary_refresh_refuses_quarantine(tmp_path, monkeypatch):
    import os
    import local_wiki_librarian.paths as paths
    wiki, state = tmp_path / "wiki", tmp_path / "state"
    _write_wiki(wiki, "One")
    refresh_wiki(wiki, state, WIKIMAP)
    before = (state / "current.json").read_bytes()
    (wiki / "new.md").write_text("# Unreadable new page")
    original = paths.os.open

    def fail(path, flags, mode=0o777, *, dir_fd=None):
        if path == "new.md" and dir_fd is not None and os.fstat(dir_fd).st_ino == wiki.stat().st_ino:
            raise PermissionError("new source unreadable")
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(paths.os, "open", fail)
    with pytest.raises(RuntimeError, match="generation preparation failed"):
        refresh_wiki(wiki, state, WIKIMAP)
    assert (state / "current.json").read_bytes() == before


def test_boundary_exact_migration_receipt_and_wrong_cas(tmp_path):
    old, new, state = (tmp_path / name for name in ("old", "new", "state"))
    _write_wiki(old, "Old")
    first = refresh_wiki(old, state, WIKIMAP)
    before = (state / "current.json").read_bytes()
    _write_wiki(new, "New")
    receipt = build_root_migration_receipt(state, new)
    staged = stage_refresh(new, state, WIKIMAP, migration_receipt=receipt)
    with pytest.raises(RuntimeError, match="current pointer digest mismatch"):
        refresh_module.promote_generation(
            state, staged.generation, staged.manifest_sha256, staged.candidate_sha256,
            first.generation, "0" * 64, WIKIMAP,
        )
    assert (state / "current.json").read_bytes() == before
    promoted = refresh_module.promote_generation(
        state, staged.generation, staged.manifest_sha256, staged.candidate_sha256,
        first.generation, hashlib.sha256(before).hexdigest(), WIKIMAP,
    )
    assert promoted.promoted is True
    assert json.loads((state / "current.json").read_bytes())["canonical_root"] == str(new)
