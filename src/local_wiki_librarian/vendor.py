from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import re
from pathlib import Path
from typing import Any

from .paths import UnsafePathError, resolve_within

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_KEYS = {
    "schema_version",
    "name",
    "version",
    "repository",
    "commit",
    "license",
    "vendored_at",
    "files",
}


class VendorVerificationError(RuntimeError):
    """Raised when vendored source or provenance fails closed verification."""


def bundled_vendor_root() -> Path:
    """Return the filesystem root of the Wikimap payload shipped in this package."""
    root = Path(str(files("local_wiki_librarian").joinpath("_vendor", "wikimap")))
    if not root.is_dir():
        raise FileNotFoundError("packaged Wikimap payload not found")
    return root


def bundled_wikimap_path() -> Path:
    """Return the package-relative Wikimap executable path."""
    path = bundled_vendor_root() / "wikimap.py"
    if not path.is_file():
        raise FileNotFoundError("packaged Wikimap runtime not found")
    return path


def load_provenance(vendor_root: Path) -> dict[str, Any]:
    try:
        path = resolve_within(vendor_root, "UPSTREAM.json")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnsafePathError) as exc:
        raise VendorVerificationError(f"cannot read UPSTREAM.json: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _REQUIRED_KEYS:
        raise VendorVerificationError("UPSTREAM.json has an unsupported schema")
    if data["schema_version"] != 1:
        raise VendorVerificationError("UPSTREAM.json schema_version must be 1")
    if not isinstance(data["files"], dict) or not data["files"]:
        raise VendorVerificationError("UPSTREAM.json files must be a non-empty object")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_vendor(vendor_root: Path) -> dict[str, Any]:
    root = Path(vendor_root)
    provenance = load_provenance(root)
    verified: list[str] = []
    for relative, expected in provenance["files"].items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise VendorVerificationError(f"invalid file checksum declaration: {relative!r}")
        try:
            path = resolve_within(root, relative)
        except UnsafePathError as exc:
            raise VendorVerificationError(f"unsafe vendored path: {relative}") from exc
        if not path.is_file():
            raise VendorVerificationError(f"missing vendored file: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise VendorVerificationError(
                f"checksum mismatch for {relative}: expected {expected}, got {actual}"
            )
        verified.append(relative)
    return {
        "name": provenance["name"],
        "version": provenance["version"],
        "commit": provenance["commit"],
        "verified_files": sorted(verified),
    }
