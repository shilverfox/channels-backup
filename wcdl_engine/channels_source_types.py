"""Shared, credential-free contracts for metadata-source adapters."""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class SourceResult:
    feeds: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    status: str = 'ok'
    counts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    description: str
    kind: str = 'metadata'
    status: str = 'ready'
    scanner: Optional[Callable] = field(default=None, repr=False)
    priority: int = 0
    note: str = ''

    def public(self):
        return {'name': self.name, 'description': self.description,
                'kind': self.kind, 'status': self.status, 'note': self.note}
