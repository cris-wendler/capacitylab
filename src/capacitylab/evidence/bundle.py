"""A set of evidence items plus explicitly registered gaps, with contradiction detection."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, Field

from capacitylab.evidence.models import EvidenceItem, EvidenceKind


class MissingEvidence(BaseModel):
    """Evidence known to be absent. Stakeholders are shown these so gaps are not silently filled."""

    id: str
    description: str
    relevant_roles: list[str] = Field(default_factory=list)
    impact: str = ""


class Contradiction(BaseModel):
    key: str
    evidence_ids: list[str]
    values: list[float | str]
    detail: str


class EvidenceBundle:
    def __init__(self, items: Iterable[EvidenceItem] = (), missing: Iterable[MissingEvidence] = ()):
        self._items: dict[str, EvidenceItem] = {}
        self.missing: list[MissingEvidence] = list(missing)
        for item in items:
            self.add(item)

    def add(self, item: EvidenceItem) -> EvidenceItem:
        if item.id in self._items:
            raise ValueError(f"duplicate evidence id {item.id}")
        self._items[item.id] = item
        return item

    def get(self, evidence_id: str) -> EvidenceItem:
        return self._items[evidence_id]

    def __contains__(self, evidence_id: str) -> bool:
        return evidence_id in self._items

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def ids(self) -> list[str]:
        return list(self._items)

    def by_kind(self, kind: EvidenceKind) -> list[EvidenceItem]:
        return [i for i in self._items.values() if i.kind == kind]

    def first(self, kind: EvidenceKind) -> EvidenceItem | None:
        found = self.by_kind(kind)
        return found[0] if found else None

    def next_id(self, prefix: str) -> str:
        n = 1 + sum(1 for i in self._items if i.startswith(prefix + "-"))
        candidate = f"{prefix}-{n:03d}"
        while candidate in self._items:
            n += 1
            candidate = f"{prefix}-{n:03d}"
        return candidate

    def digest(self) -> str:
        h = hashlib.sha256()
        for evidence_id in sorted(self._items):
            h.update(self._items[evidence_id].digest().encode())
        for gap in sorted(self.missing, key=lambda g: g.id):
            h.update(gap.model_dump_json().encode())
        return h.hexdigest()

    def copy(self) -> EvidenceBundle:
        return EvidenceBundle((i.model_copy(deep=True) for i in self._items.values()), list(self.missing))

    def contradictions(self) -> list[Contradiction]:
        """Group assertions by key and report sources that disagree beyond their stated tolerance."""
        grouped: dict[str, list[tuple[str, float | str, float]]] = defaultdict(list)
        for item in self._items.values():
            for a in item.assertions:
                grouped[a.key].append((item.id, a.value, a.tolerance))
        found: list[Contradiction] = []
        for key, entries in sorted(grouped.items()):
            if len(entries) < 2:
                continue
            values = [v for _, v, _ in entries]
            ids = [i for i, _, _ in entries]
            if all(isinstance(v, int | float) for v in values):
                lo, hi = min(values), max(values)
                tol = max(t for _, _, t in entries)
                spread = (hi - lo) / max(abs(hi), 1e-9)
                if spread > tol:
                    found.append(
                        Contradiction(
                            key=key,
                            evidence_ids=ids,
                            values=values,
                            detail=f"sources differ by {spread:.0%} (tolerance {tol:.0%})",
                        )
                    )
            elif len({str(v) for v in values}) > 1:
                found.append(Contradiction(key=key, evidence_ids=ids, values=values, detail="sources disagree"))
        return found
