class VelaTraceError(Exception):
    """An actionable failure safe to present without exposing credentials."""


class CapabilityError(VelaTraceError):
    """The connected tool cannot safely perform this operation."""


class ValidationError(VelaTraceError):
    """Input was refused before any mutation."""


class ExportUnavailable(CapabilityError):
    """KiCad's bundled Python cannot export DSN here; the manual export path remains."""


class RoutingCancelled(VelaTraceError):
    """The user cancelled routing; nothing was written to the board."""
