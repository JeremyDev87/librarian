from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .paths import UnsafePathError, validate_disjoint_roots


@dataclass(frozen=True)
class RetrievalConfig:
    """Filesystem ownership contract for a future retrieval runtime.

    Validation is intentionally side-effect free: it never creates the state root.
    """

    canonical_root: Path
    state_root: Path
    vendor_root: Path

    def validate(self) -> "RetrievalConfig":
        canonical = Path(self.canonical_root).expanduser()
        vendor = Path(self.vendor_root).expanduser()
        if not canonical.is_dir():
            raise UnsafePathError(f"canonical root must be an existing directory: {canonical}")
        if not vendor.is_dir():
            raise UnsafePathError(f"vendor root must be an existing directory: {vendor}")

        canonical_root, state_root = validate_disjoint_roots(canonical, self.state_root)
        canonical_root, vendor_root = validate_disjoint_roots(canonical_root, vendor)
        state_root, vendor_root = validate_disjoint_roots(state_root, vendor_root)
        return replace(
            self,
            canonical_root=canonical_root,
            state_root=state_root,
            vendor_root=vendor_root,
        )
