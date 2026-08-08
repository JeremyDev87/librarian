from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_manifest_module():
    path = REPO_ROOT / "scripts" / "build-release-manifest.py"
    spec = importlib.util.spec_from_file_location("build_release_manifest", path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load release manifest generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def test_release_manifest_captures_source_artifacts_provenance_and_verifiers(tmp_path: Path) -> None:
    module = _load_manifest_module()
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Release Test")

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "local-wiki-librarian"\nversion = "0.1.0"\n', encoding="utf-8")
    vendor = tmp_path / "src/local_wiki_librarian/_vendor/wikimap"
    vendor.mkdir(parents=True)
    (vendor / "wikimap.py").write_text("print('ok')\n", encoding="utf-8")
    (vendor / "LICENSE").write_text("MIT\n", encoding="utf-8")
    upstream = {
        "schema_version": 1,
        "name": "wikimap",
        "version": "1.0.0",
        "repository": "https://example.invalid/wikimap",
        "commit": "a" * 40,
        "license": "MIT",
        "vendored_at": "2026-01-01",
        "files": {
            "wikimap.py": hashlib.sha256((vendor / "wikimap.py").read_bytes()).hexdigest(),
            "LICENSE": hashlib.sha256((vendor / "LICENSE").read_bytes()).hexdigest(),
        },
    }
    (vendor / "UPSTREAM.json").write_text(json.dumps(upstream), encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "build-release-manifest.py").write_text("producer\n", encoding="utf-8")
    (scripts / "verify-installed-package.py").write_text("verifier\n", encoding="utf-8")
    (tmp_path / "NOTICE").write_text("notice\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "fixture")

    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "local_wiki_librarian-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("local_wiki_librarian/__init__.py", "__version__ = '0.1.0'\n")
        archive.write(vendor / "LICENSE", "local_wiki_librarian/_vendor/wikimap/LICENSE")
        archive.write(vendor / "UPSTREAM.json", "local_wiki_librarian/_vendor/wikimap/UPSTREAM.json")
    sdist = dist / "local-wiki-librarian-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = tmp_path / "payload.txt"
        payload.write_text("source\n", encoding="utf-8")
        archive.add(payload, arcname="local-wiki-librarian-0.1.0/README.md")
        archive.add(vendor / "LICENSE", arcname="local-wiki-librarian-0.1.0/src/local_wiki_librarian/_vendor/wikimap/LICENSE")
        archive.add(vendor / "UPSTREAM.json", arcname="local-wiki-librarian-0.1.0/src/local_wiki_librarian/_vendor/wikimap/UPSTREAM.json")

    verifier_wheel = tmp_path / "verifier-wheel.json"
    verifier_sdist = tmp_path / "verifier-sdist.json"
    verifier_wheel.write_text('{"status":"ok","artifact":"wheel"}\n', encoding="utf-8")
    verifier_sdist.write_text('{"status":"ok","artifact":"sdist"}\n', encoding="utf-8")
    result = module.build_manifest(
        source_root=tmp_path,
        artifacts=[wheel, sdist],
        tag="v0.1.0",
        source_sha=_git(tmp_path, "rev-parse", "HEAD"),
        verifier_results=[verifier_wheel, verifier_sdist],
    )

    assert result["source"]["tag"] == "v0.1.0"
    assert result["source"]["version"] == "0.1.0"
    assert result["source"]["tree"] == _git(tmp_path, "rev-parse", "HEAD^{tree}")
    assert {item["name"] for item in result["artifacts"]} == {wheel.name, sdist.name}
    assert result["artifacts"][0]["sha256"]
    assert result["artifacts"][0]["members"]
    assert result["provenance"]["upstream_json"]["sha256"]
    assert result["provenance"]["vendor_files"]["wikimap.py"]["sha256"] == upstream["files"]["wikimap.py"]
    assert result["producer_scripts"]["scripts/build-release-manifest.py"]
    assert len(result["verifiers"]) == 2
    assert result["policy"]["notice"]["included_in_artifacts"] is False

    output = tmp_path / "release-manifest.json"
    assert module.main([
        "--source-root", str(tmp_path),
        "--source-sha", _git(tmp_path, "rev-parse", "HEAD"),
        "--tag", "v0.1.0",
        "--artifacts", str(wheel), str(sdist),
        "--verifier-result", str(verifier_wheel), str(verifier_sdist),
        "--output", str(output),
    ]) == 0
    assert output.is_file()


def _workflow(name: str) -> tuple[str, dict]:
    path = REPO_ROOT / ".github" / "workflows" / name
    text = path.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_candidate_workflow_is_read_only_and_freezes_one_build() -> None:
    text, workflow = _workflow("release-candidate.yml")
    assert "workflow_dispatch" in text
    assert workflow["permissions"]["contents"] == "read"
    assert "id-token" not in text
    assert text.count("python -m build") == 1
    assert "twine check dist/*" in text
    assert "build-release-manifest.py" in text
    assert "actions/upload-artifact@" in text
    assert all("@" in line and len(line.split("@", 1)[1].split()[0]) == 40 for line in text.splitlines() if "uses:" in line)
    assert "${{" not in "\n".join(line for line in text.splitlines() if "run:" in line)


def test_publish_workflow_has_separate_oidc_gate_and_admission_order() -> None:
    text, workflow = _workflow("publish-pypi.yml")
    assert "workflow_dispatch" in text
    assert "verify-release-admission.py" in text
    assert "gh release download" in text
    assert "pypa/gh-action-pypi-publish@" in text
    assert "environment: pypi" in text
    assert "id-token: write" in text
    assert "contents: write" not in text
    assert "${{" not in "\n".join(line for line in text.splitlines() if "run:" in line)
    assert text.index("verify-release-admission.py") < text.index("pypa/gh-action-pypi-publish@")
    assert all("@" in line and len(line.split("@", 1)[1].split()[0]) == 40 for line in text.splitlines() if "uses:" in line)
    assert workflow["jobs"]["publish"]["environment"] == "pypi"


def test_release_runbook_keeps_owner_gates_and_notice_policy_explicit() -> None:
    text = (REPO_ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    for marker in ("v0.1.0", "Trusted Publishing", "NOTICE", "PyPI", "Owner Gate", "rollback"):
        assert marker in text
    assert "merge" in text.lower()
