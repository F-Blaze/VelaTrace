"""Immutable design facts; all design-origin strings are untrusted data."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Pin:
    number: str
    net: str
    name: str = ""


@dataclass(frozen=True)
class Component:
    reference: str
    value: str
    footprint: str
    pins: tuple[Pin, ...] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    position_mm: tuple[float, float] | None = None
    kind: str = "unknown"
    uuid: str = ""

    @property
    def nets(self) -> frozenset[str]:
        return frozenset(pin.net for pin in self.pins if pin.net)


@dataclass(frozen=True)
class DesignSnapshot:
    components: tuple[Component, ...]
    source: str
    path: Path | None = None
    copper_layers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def connectivity(self) -> dict[str, tuple[tuple[str, str], ...]]:
        graph: dict[str, list[tuple[str, str]]] = {}
        for component in self.components:
            for pin in component.pins:
                if pin.net:
                    graph.setdefault(pin.net, []).append((component.reference, pin.number))
        return {net: tuple(nodes) for net, nodes in graph.items()}
