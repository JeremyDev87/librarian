from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_wiki_librarian.vendor import (
    VendorVerificationError,
    bundled_vendor_root,
    bundled_wikimap_path,
    load_provenance,
    verify_vendor,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_ROOT = REPO_ROOT / "src" / "local_wiki_librarian" / "_vendor" / "wikimap"
EXPECTED_COMMIT = "9c26d7b66322741532ede0b474f0e5106643f275"


def test_wikimap_provenance_is_exactly_pinned() -> None:
    provenance = load_provenance(VENDOR_ROOT)

    assert provenance["name"] == "wikimap"
    assert provenance["version"] == "1.0.0"
    assert provenance["commit"] == EXPECTED_COMMIT
    assert provenance["repository"] == "https://github.com/dhha22/wikimap"
    assert provenance["license"] == "MIT"
    assert set(provenance["files"]) == {"wikimap.py", "LICENSE"}


def test_wikimap_vendor_hashes_and_notice_are_valid() -> None:
    result = verify_vendor(VENDOR_ROOT)

    assert result["verified_files"] == ["LICENSE", "wikimap.py"]
    license_text = (VENDOR_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Donghyun Ha" in license_text
    assert "Permission is hereby granted" in license_text


def test_bundled_vendor_is_discoverable_and_verified() -> None:
    root = bundled_vendor_root()

    assert bundled_wikimap_path() == root / "wikimap.py"
    assert verify_vendor(root)["verified_files"] == ["LICENSE", "wikimap.py"]


def test_wikimap_vendor_tampering_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "wikimap"
    root.mkdir()
    provenance = {
        "schema_version": 1,
        "name": "wikimap",
        "version": "1.0.0",
        "repository": "https://github.com/dhha22/wikimap",
        "commit": EXPECTED_COMMIT,
        "license": "MIT",
        "vendored_at": "2026-07-15",
        "files": {
            "wikimap.py": "0" * 64,
            "LICENSE": "1" * 64,
        },
    }
    (root / "UPSTREAM.json").write_text(json.dumps(provenance), encoding="utf-8")
    (root / "wikimap.py").write_text("print('tampered')\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")

    with pytest.raises(VendorVerificationError, match="checksum mismatch"):
        verify_vendor(root)
