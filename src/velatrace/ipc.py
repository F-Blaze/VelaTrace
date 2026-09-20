"""Official kicad-python adapter. This module deliberately exposes no writes.

All board mutations belong to the separately reviewed write-safety boundary.
"""
from pathlib import Path
from typing import Any

from .errors import CapabilityError
from .models import Component, DesignSnapshot, Pin


class KiCadReader:
    def __init__(self, client: Any):
        self.client = client
        version = client.get_version()
        if version.major < 9:
            raise CapabilityError("VelaTrace requires KiCad 9 or newer with IPC enabled.")
        self.version = (version.major, version.minor, version.patch)

    @classmethod
    def connect(cls) -> "KiCadReader":
        try:
            from kipy import KiCad
            return cls(KiCad(timeout_ms=2000))
        except ImportError as exc:
            raise CapabilityError("Install the pinned kicad-python dependency first.") from exc
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(
                "Cannot connect to KiCad. Open PCB Editor and enable Preferences > Plugins > IPC API."
            ) from exc

    def read_board(self) -> DesignSnapshot:
        from kipy.board_types import Field

        board = self.client.get_board()
        components = []
        for fp in board.get_footprints():
            fields = {item.name: item.text.value for item in fp.texts_and_fields
                      if isinstance(item, Field)}
            library = fp.definition.id
            components.append(Component(
                reference=fp.reference_field.text.value,
                value=fp.value_field.text.value,
                footprint=f"{library.library}:{library.name}",
                pins=tuple(Pin(pad.number, pad.net.name) for pad in fp.definition.pads),
                fields=fields,
                position_mm=(fp.position.x / 1_000_000, fp.position.y / 1_000_000),
                uuid=fp.id.value,
            ))
        # Net names, not internal net codes, identify connectivity.
        return DesignSnapshot(tuple(components), "ipc-pcb", Path(board.name), warnings=(
            "Footprint positions are geometry, not proof that placement is complete.",
        ))

    def export_dsn(self, destination: Path) -> None:
        raise CapabilityError(
            "The pinned IPC client cannot export DSN. Export Specctra DSN from KiCad's File menu; "
            "routing must verify it matches the saved board before use."
        )
