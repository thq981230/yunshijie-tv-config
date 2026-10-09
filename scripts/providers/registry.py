from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Protocol


class ProviderAdapter(Protocol):
    """A provider may supply only sources it is authorized to publish."""

    def discover(self, candidate_dir: Path, catalog: dict) -> Iterable[tuple[Path, dict]]:
        """Yield declared candidate documents; adapters must not bypass access controls."""


class StaticCandidateProvider:
    """Reads reviewed candidate JSON files; it does not crawl or invent stream URLs."""

    def discover(self, candidate_dir: Path, catalog: dict) -> Iterable[tuple[Path, dict]]:
        for path in sorted(candidate_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as stream:
                yield path, json.load(stream)


class ProviderRegistry:
    def __init__(self, providers: Iterable[ProviderAdapter]):
        self._providers = tuple(providers)

    def discover(self, candidate_dir: Path, catalog: dict) -> Iterable[tuple[Path, dict]]:
        for provider in self._providers:
            yield from provider.discover(candidate_dir, catalog)
