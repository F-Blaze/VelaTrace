class VelaTraceError(Exception):
    """An actionable failure safe to present without exposing credentials."""


class CapabilityError(VelaTraceError):
    """The connected tool cannot safely perform this operation."""


class ValidationError(VelaTraceError):
    """Input was refused before any mutation."""
