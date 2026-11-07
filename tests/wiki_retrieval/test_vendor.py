from __future__ import annotations

import hashlib
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
    provenance = load_provenance(VENDOR_ROOT)

    assert result["verified_files"] == ["LICENSE", "wikimap.py"]
    assert hashlib.sha256((VENDOR_ROOT / "wikimap.py").read_bytes()).hexdigest() == provenance["files"]["wikimap.py"]
    assert hashlib.sha256((VENDOR_ROOT / "LICENSE").read_bytes()).hexdigest() == "e950a4da5722fea564dff1266790dd4d1415a6e70ee37baeb6147d0e9dc93e91"
    license_text = (VENDOR_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 Donghyun Ha" in license_text
    assert "Permission is hereby granted" in license_text


def test_bundled_vendor_is_discoverable_and_verified() -> None:
    root = bundled_vendor_root()

    assert bundled_wikimap_path() == root / "wikimap.py"
    assert verify_vendor(root)["verified_files"] == ["LICENSE", "wikimap.py"]


@pytest.mark.parametrize("tampered_file", ["wikimap.py", "LICENSE"])
def test_wikimap_vendor_tampering_fails_closed(tmp_path: Path, tampered_file: str) -> None:
    root = tmp_path / "wikimap"
    root.mkdir()
    provenance = load_provenance(VENDOR_ROOT)
    (root / "UPSTREAM.json").write_text(json.dumps(provenance), encoding="utf-8")
    for filename in provenance["files"]:
        (root / filename).write_bytes((VENDOR_ROOT / filename).read_bytes())
    verify_vendor(root)
    target = root / tampered_file
    target.write_bytes(target.read_bytes() + b"\n# tampered\n")

    with pytest.raises(VendorVerificationError, match=f"checksum mismatch for {tampered_file}"):
        verify_vendor(root)
