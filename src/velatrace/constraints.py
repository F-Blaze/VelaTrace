"""Numeric routing constraints; no board text or model can confirm these."""
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import os
import re
import tempfile
import uuid

from .errors import ValidationError


class Scope(str, Enum):
    SESSION = "session"
    UNIVERSAL = "universal"


@dataclass(frozen=True)
class Constraint:
    id: str
    scope: Scope
    kind: str
    target: str
    minimum_mm: float

    def __post_init__(self):
        if (not isinstance(self.scope, Scope) or self.kind not in {"clearance", "trace-width", "header-clearance"}
                or not isinstance(self.target, str) or not self.target.strip()
                or not isinstance(self.id, str) or not self.id
                or type(self.minimum_mm) not in {int, float}
                or not math.isfinite(self.minimum_mm) or not 0 < self.minimum_mm <= 100):
            raise ValidationError("Constraint requires a supported kind, target, scope and 0–100 mm minimum.")

    @property
    def description(self) -> str:
        noun = "trace width" if self.kind == "trace-width" else "clearance"
        return f"≥{self.minimum_mm:g} mm {noun} from/to {self.target} ({self.scope.value})"


def propose_constraint(text: str, scope: Scope = Scope.SESSION) -> Constraint:
    """A proposal only: routing always separately confirms the entire numeric list."""
    # Deliberately small grammar. Never discard an unparsed clause or explicit quantity.
    if isinstance(text, str):
        match = re.fullmatch(r"\s*(?:keep\s+(?:traces\s+)?(?:(?:at\s+least|>=|≥)\s*)?(?:(\d+(?:\.\d+)?)\s*mm\s+)?away\s+from|avoid)\s+(?:all\s+)?headers?\s*[.]?\s*", text, re.I)
        if match:
            minimum = float(match[1]) if match[1] else 2.0
            return Constraint(uuid.uuid4().hex, scope, "header-clearance", "all header footprints", minimum)
    raise ValidationError("Specify a numeric clearance or trace width and explicit target; ambiguous constraints cannot route.")


class ConstraintStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._items: dict[str, Constraint] = {}
        if self.path.exists():
            try:
                if self.path.stat().st_size > 250_000:
                    raise ValueError()
                rows = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ValueError()
                for row in rows:
                    value = Constraint(**{**row, "scope": Scope(row["scope"])})
                    if value.scope != Scope.UNIVERSAL or value.id in self._items:
                        raise ValueError()
                    self._items[value.id] = value
            except (ValueError, TypeError, KeyError, OSError) as exc:
                raise ValidationError("Universal constraint config is invalid; repair it before routing.") from exc

    @property
    def items(self) -> tuple[Constraint, ...]:
        return tuple(self._items.values())

    @property
    def fingerprint(self) -> str:
        data = json.dumps([asdict(item) for item in self.items], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps([asdict(item) for item in self.items if item.scope == Scope.UNIVERSAL], indent=2)
        fd, name = tempfile.mkstemp(prefix=".constraints-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def add(self, item: Constraint):
        if item.id in self._items or len(self._items) >= 500:
            raise ValidationError("Duplicate constraint ID or too many constraints.")
        self._items[item.id] = item
        try:
            if item.scope == Scope.UNIVERSAL:
                self._persist()
        except OSError:
            del self._items[item.id]
            raise ValidationError("Could not save universal constraints.") from None

    def remove(self, identifier: str):
        if identifier not in self._items:
            raise ValidationError("Unknown constraint ID.")
        value = self._items.pop(identifier)
        try:
            if value.scope == Scope.UNIVERSAL:
                self._persist()
        except OSError:
            self._items[identifier] = value
            raise ValidationError("Could not save universal constraints.") from None
