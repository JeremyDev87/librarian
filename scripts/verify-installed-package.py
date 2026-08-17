#!/usr/bin/env python3
"""Verify a frozen Local Wiki Librarian distribution in isolation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Dict, List, Optional
import venv


def _run(command: List[str], *, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    clean_env = dict(os.environ if env is None else env)
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    return subprocess.run(
        command, check=True, capture_output=True, text=True, env=clean_env
    )


def _run_unchecked(
    command: List[str], *, env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    clean_env = dict(os.environ if env is None else env)
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    return subprocess.run(
        command, check=False, capture_output=True, text=True, env=clean_env,
    )


def _json(command: list[str], *, env: dict[str, str]) -> dict:
    return json.loads(_run(command, env=env).stdout)


def _tree_digest(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--coexist-package", default="librarian==0.3.0")
    args = parser.parse_args(argv)
    artifact = args.artifact.resolve(strict=True)
    if args.runs < 2:
        parser.error("--runs must be at least 2")

    with tempfile.TemporaryDirectory(prefix="local-wiki-librarian-wheel-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        bindir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
        _run([str(python), "-m", "pip", "install", args.coexist_package, str(artifact)])
        _run([str(python), "-m", "pip", "check"])
        _run([
            str(python), "-c",
            "import librarian, local_wiki_librarian; "
            "assert librarian.__name__ == 'librarian'; "
            "assert local_wiki_librarian.__name__ == 'local_wiki_librarian'",
        ])

        wiki = root / "wiki"
        page = wiki / "notes" / "policy.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\nauthority: high\nstatus: active\n---\n"
            "# Atomic local retrieval\npackage smoke phrase\n",
            encoding="utf-8",
        )
        source_digest = _tree_digest(wiki)
        state = root / "state"
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        refresh = bindir / ("wiki-librarian-refresh.exe" if os.name == "nt" else "wiki-librarian-refresh")
        cli = bindir / ("wiki-librarian.exe" if os.name == "nt" else "wiki-librarian")
        generations = []
        for _ in range(args.runs):
            refreshed = _json([
                str(refresh), "--wiki-root", str(wiki), "--state-root", str(state),
            ], env=env)
            health = _json([str(cli), "health", "--state-root", str(state)], env=env)
            search = _json([
                str(cli), "search", "package smoke phrase", "--state-root", str(state),
            ], env=env)
            audit = _json([str(cli), "audit", "--state-root", str(state)], env=env)
            assert refreshed["status"] == "ok"
            assert health["status"] == "ok"
            assert search["status"] == "ok"
            assert search["results"][0]["path"] == "notes/policy.md"
            assert audit["passed"] is True
            assert _tree_digest(wiki) == source_digest
            generations.append(refreshed["generation"])

        # Prove the installed maintenance entrypoint separates stage from
        # promotion and binds the latter to the exact current pointer.
        current_path = state / "current.json"
        current_before = current_path.read_bytes()
        current = json.loads(current_before)
        expected_current_sha256 = hashlib.sha256(current_before).hexdigest()
        page.write_text(
            "---\nauthority: high\nstatus: active\n---\n"
            "# Atomic local retrieval\npackage staged promotion phrase\n",
            encoding="utf-8",
        )
        staged_source_digest = _tree_digest(wiki)
        staged = _json([
            str(refresh), "--wiki-root", str(wiki), "--state-root", str(state),
            "--stage-only",
        ], env=env)
        assert staged["status"] == "ok"
        assert staged["promoted"] is False
        assert current_path.read_bytes() == current_before
        promoted = _json([
            str(refresh), "--state-root", str(state),
            "--promote-generation", staged["generation"],
            "--expected-manifest-sha256", staged["manifest_sha256"],
            "--expected-candidate-sha256", staged["candidate_sha256"],
            "--expected-current-generation", current["generation"],
            "--expected-current-sha256", expected_current_sha256,
        ], env=env)
        assert promoted["promoted"] is True
        assert json.loads(current_path.read_text(encoding="utf-8"))["generation"] == staged["generation"]

        # A different canonical root is denied before staging without an exact
        # migration receipt; the active pointer remains byte-identical.
        other_wiki = root / "other-wiki"
        other_page = other_wiki / "notes" / "policy.md"
        other_page.parent.mkdir(parents=True)
        other_page.write_text("---\nstatus: active\n---\n# Other root\n", encoding="utf-8")
        promoted_pointer = current_path.read_bytes()
        denied = _run_unchecked([
            str(refresh), "--wiki-root", str(other_wiki), "--state-root", str(state),
            "--stage-only",
        ], env=env)
        assert denied.returncode == 2
        assert json.loads(denied.stdout)["status"] == "error"
        assert current_path.read_bytes() == promoted_pointer

        # Roll back and restore using only generation-local verified bytes.
        rollback_sha = hashlib.sha256(promoted_pointer).hexdigest()
        rolled_back = _json([
            str(refresh), "--state-root", str(state),
            "--rollback-generation", current["generation"],
            "--expected-current-sha256", rollback_sha,
        ], env=env)
        assert rolled_back["generation"] == current["generation"]
        restore_sha = hashlib.sha256(current_path.read_bytes()).hexdigest()
        restored = _json([
            str(refresh), "--state-root", str(state),
            "--rollback-generation", staged["generation"],
            "--expected-current-sha256", restore_sha,
        ], env=env)
        assert restored["generation"] == staged["generation"]
        assert _tree_digest(wiki) == staged_source_digest

        vendor = json.loads(_run([
            str(python), "-c",
            "import json; from local_wiki_librarian.vendor import bundled_vendor_root, verify_vendor; "
            "print(json.dumps(verify_vendor(bundled_vendor_root()), sort_keys=True))",
        ]).stdout)
        schema_probe = _run([
            str(python), "-c",
            "from importlib.resources import files; "
            "assert files('local_wiki_librarian').joinpath('schemas/librarian-packet.schema.json').is_file()",
        ])
        assert schema_probe.returncode == 0
        print(json.dumps({
            "status": "ok",
            "runs": args.runs,
            "distinct_generations": len(set(generations)),
            "search_path": "notes/policy.md",
            "source_unchanged": True,
            "stage_promote_verified": True,
            "root_migration_default_denied": True,
            "rollback_restore_verified": True,
            "coexist_package": args.coexist_package,
            "vendor_commit": vendor["commit"],
            "vendor_verified_files": vendor["verified_files"],
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
