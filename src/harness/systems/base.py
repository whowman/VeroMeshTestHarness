"""What every simulated source system provides.

A system is a projection of the truth into that system's own shape, IDs and quirks,
served behind a mock of its real API. Each quirk it introduces is recorded as a
defect: what VeroMesh should find. Systems are plug-ins: a module in this package that
defines a ``SourceSystem`` subclass registers itself by its ``name``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, timedelta

from fastapi import APIRouter

from ..truth import Person, Position, Truth, chance

REGISTRY: dict[str, type[SourceSystem]] = {}


class SourceSystem:
    name = ""  # its path prefix in the harness, and its registry key
    title = ""  # e.g. "Microsoft Entra ID (Graph API)"

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if cls.name:
            REGISTRY[cls.name] = cls

    def __init__(self, truth: Truth):
        self.truth = truth
        self._cache: tuple | None = None

    # ------------------------------------------------------------ to implement

    def project(self) -> tuple[dict, list[dict]]:
        """The system's records as of the truth's today, and the defects its quirks made."""
        raise NotImplementedError

    def router(self) -> APIRouter:
        """The mock of the system's API (mounted at /<name>)."""
        raise NotImplementedError

    # ------------------------------------------------------------ helpers

    def state(self) -> tuple[dict, list[dict]]:
        """The projection, rebuilt only when the clock has moved."""
        key = (self.truth.today, len(self.truth.applied))
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, *self.project())
        return self._cache[1], self._cache[2]

    def records(self) -> dict:
        return self.state()[0]

    def defects(self) -> list[dict]:
        return self.state()[1]

    def quirk(self, rate: float, *keys) -> bool:
        """Whether this quirk falls on these keys (stable across re-projection)."""
        return chance(self.truth.seed, self.name, *keys) < rate

    def guid(self, *keys) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"harness:{self.name}:{self.truth.seed}:" + ":".join(map(str, keys))))

    def sys_id(self, *keys) -> str:
        return hashlib.sha256(f"{self.name}:{self.truth.seed}:{':'.join(map(str, keys))}".encode()).hexdigest()[:32]

    def seen_as_of(self, lag_days: int) -> date:
        """The day the system last synced, ``lag_days`` behind the truth."""
        return self.truth.today - timedelta(days=lag_days)

    def changed_on(self, p: Person) -> date:
        """When the person's record last changed in the truth."""
        days = [p.hired, *(h.start for h in p.history)]
        if p.left:
            days.append(p.left)
        return max(days)

    @staticmethod
    def previous(p: Person, day: date) -> Position | None:
        """The position held before the current one (for a lagging system)."""
        held = [h for h in p.history if h.start <= day]
        return held[-2] if len(held) > 1 else None


def defect(kind: str, system: str, about: dict, detail: str) -> dict:
    """An answer-key entry: a quirk VeroMesh should find, and what it is about."""
    return {"kind": kind, "system": system, "about": about, "detail": detail}
