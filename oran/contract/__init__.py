"""Fail-closed kernel for the explicitly selected O-RAN contract authority."""

from .digests import verify_contract_authority, verify_contract_integrity

__all__ = ["verify_contract_authority", "verify_contract_integrity"]

# This package is the contract startup boundary.  Do not permit a component to
# begin using schemas or canonical digests before the immutable handoff gate.
verify_contract_authority()
