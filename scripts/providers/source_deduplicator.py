from __future__ import annotations

import copy
import urllib.parse
from collections import OrderedDict
from typing import Iterable

from providers.base import DiscoveredSource


def canonical_source_key(source: dict) -> tuple[str, str, str, str, str]:
    """Normalize host spelling/default ports while keeping the full query intact."""
    parsed = urllib.parse.urlsplit(str(source.get("url") or ""))
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").encode("idna").decode("ascii").casefold()
    port = parsed.port
    if (scheme, port) in {("https", 443), ("http", 80)}:
        port = None
    host_port = host if port is None else f"{host}:{port}"
    return (str(source.get("protocol") or "HLS").upper(), scheme, host_port,
            parsed.path or "/", parsed.query)


class SourceDeduplicator:
    """Collapses duplicate public playback URLs and retains every feed's provenance."""

    def merge(self, sources: Iterable[DiscoveredSource]) -> list[DiscoveredSource]:
        merged: OrderedDict[tuple, DiscoveredSource] = OrderedDict()
        for item in sources:
            key = (item.channel_id, *canonical_source_key(item.source))
            current = merged.get(key)
            if current is None:
                source = copy.deepcopy(item.source)
                providers = source.get("sourceProviders") or [source.get("providerId")]
                source["sourceProviders"] = list(dict.fromkeys(str(value) for value in providers if value))
                merged[key] = DiscoveredSource(item.channel_id, source, item.provider_id, item.origin)
                continue

            providers = current.source.setdefault("sourceProviders", [])
            incoming = item.source.get("sourceProviders") or [item.source.get("providerId")]
            providers.extend(value for value in incoming if value and value not in providers)
            current_priority = int(current.source.get("priority", 100))
            incoming_priority = int(item.source.get("priority", 100))
            if incoming_priority < current_priority:
                provenance = list(providers)
                chosen = copy.deepcopy(item.source)
                chosen["sourceProviders"] = provenance
                merged[key] = DiscoveredSource(item.channel_id, chosen, item.provider_id, item.origin)

        return list(merged.values())
