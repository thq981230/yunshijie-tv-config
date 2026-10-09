from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SourceCandidate:
    id: str
    channel_id: str
    url: str
    protocol: str
    authorization: str
    priority: int = 100
    quality: str = "AUTO"
    source_type: str = "STATIC"


class ProviderAdapter(Protocol):
    """An adapter may only resolve feeds the operator has documented permission to use."""

    provider_id: str

    def discover(self, channel_id: str) -> list[SourceCandidate]: ...
