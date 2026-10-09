from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol


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


@dataclass(frozen=True)
class DiscoveredSource:
    channel_id: str
    source: dict
    provider_id: str
    origin: str


class ProviderAdapter(Protocol):
    """A provider adapter only exposes sources it has documented permission to use."""

    provider_id: str

    def discover(self, catalog: dict) -> Iterable[DiscoveredSource]: ...

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]: ...

    def health_check(self, source: Mapping[str, object]) -> dict: ...

