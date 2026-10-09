from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterable, Mapping

from providers.base import DiscoveredSource, ProviderAdapter


class StaticProviderAdapter:
    """Loads reviewed candidates from JSON; it never scans broadcaster players or invents URLs."""

    provider_id = "static-candidates"

    def __init__(self, candidate_dir: Path):
        self.candidate_dir = candidate_dir

    def discover(self, catalog: dict) -> list[DiscoveredSource]:
        discovered: list[DiscoveredSource] = []
        for path in sorted(self.candidate_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict) or not isinstance(payload.get("channels"), list):
                continue
            for channel in payload["channels"]:
                channel_id = str(channel.get("channelId", ""))
                for source in channel.get("sources", []):
                    candidate = dict(source)
                    candidate.setdefault("channelId", channel_id)
                    discovered.append(DiscoveredSource(channel_id, candidate, self.provider_id,
                                                       str(path.relative_to(self.candidate_dir.parent))))
        return discovered

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]:
        return [row for row in self.discover(catalog) if row.channel_id == channel_id]

    def health_check(self, source: Mapping[str, object]) -> dict:
        from check_sources import probe
        return probe(dict(source))


class StaticCandidateProvider:
    """Backward-compatible reader used by the repository validator."""

    provider_id = "static-candidates"

    def discover(self, candidate_dir: Path, catalog: dict):
        for path in sorted(candidate_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as stream:
                yield path, json.load(stream)


class ProviderRegistry:
    def __init__(self, providers: Iterable):
        self._providers = tuple(providers)

    def discover(self, candidate_dir: Path, catalog: dict):
        for provider in self._providers:
            yield from provider.discover(candidate_dir, catalog)


class DynamicProviderAdapter:
    """Adapter for a provider's documented API; handlers are injected, never reverse engineered."""

    def __init__(self, provider_id: str, resolver: Callable[[str], Iterable[dict]],
                 discoverer: Callable[[dict], Iterable[dict]] | None = None,
                 checker: Callable[[Mapping[str, object]], dict] | None = None):
        self.provider_id = provider_id
        self._resolver = resolver
        self._discoverer = discoverer
        self._checker = checker

    def discover(self, catalog: dict) -> list[DiscoveredSource]:
        if self._discoverer is None:
            return []
        return [self._wrap(row, "provider-api") for row in self._discoverer(catalog)]

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]:
        return [self._wrap(row, "provider-api") for row in self._resolver(channel_id)]

    def health_check(self, source: Mapping[str, object]) -> dict:
        if self._checker is None:
            raise RuntimeError(f"{self.provider_id} has no documented health-check API")
        return self._checker(source)

    def _wrap(self, value: dict, origin: str) -> DiscoveredSource:
        channel_id = str(value.get("channelId") or value.get("channel_id") or "")
        source = dict(value)
        source["channelId"] = channel_id
        source["providerId"] = self.provider_id
        return DiscoveredSource(channel_id, source, self.provider_id, origin)


class SubscriptionProviderAdapter(ABC):
    """Base for provider-owned or user-owned subscriptions with an explicit rights declaration."""

    provider_id: str

    @abstractmethod
    def discover(self, catalog: dict) -> list[DiscoveredSource]: ...

    @abstractmethod
    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]: ...

    @abstractmethod
    def health_check(self, source: Mapping[str, object]) -> dict: ...


class OfficialProviderRegistry:
    def __init__(self, providers: Iterable[ProviderAdapter | SubscriptionProviderAdapter]):
        self._providers = tuple(providers)
        self._by_id = {provider.provider_id: provider for provider in self._providers}
        if len(self._by_id) != len(self._providers):
            raise ValueError("provider ids must be unique")

    def discover(self, catalog: dict) -> list[DiscoveredSource]:
        known_ids = {str(row.get("id")) for row in catalog.get("channels", [])}
        found: list[DiscoveredSource] = []
        for provider in self._providers:
            for row in provider.discover(catalog):
                if row.channel_id not in known_ids:
                    continue
                found.append(row)
        return found

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]:
        known_ids = {str(row.get("id")) for row in catalog.get("channels", [])}
        if channel_id not in known_ids:
            return []
        return [row for provider in self._providers for row in provider.resolve(channel_id, catalog)
                if row.channel_id == channel_id]

    def health_check(self, source: Mapping[str, object], provider_id: str | None = None) -> dict:
        selected_id = provider_id or str(source.get("providerId", ""))
        provider = self._by_id.get(selected_id)
        if provider is None:
            raise ValueError(f"unknown provider {selected_id!r}")
        return provider.health_check(source)

