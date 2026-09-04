"""Expose the exact core diagnostic classes without duplicating their identity."""

from ..core.contracts import DiagnosticIssue, DiagnosticPhase, DiagnosticSeverity

__all__ = ["DiagnosticIssue", "DiagnosticPhase", "DiagnosticSeverity"]
