from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from providers.registry import ProviderRegistry, StaticCandidateProvider

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PROTOCOLS = {"HLS", "DASH"}
ALLOWED_TYPES = {"STATIC", "DYNAMIC"}
SENSITIVE_QUERY_KEYS = {"token", "auth", "authorization", "signature", "sig", "expires", "expire", "key"}


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def require_https_public_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label}: expected an absolute HTTPS URL without credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError(f"{label}: local host is not allowed")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and (not address.is_global or address.is_multicast):
        raise ValueError(f"{label}: private or non-public IP is not allowed")
    keys = {part.split("=", 1)[0].lower() for part in parsed.query.split("&") if part}
    if keys & SENSITIVE_QUERY_KEYS:
        raise ValueError(f"{label}: expiring/authenticated URL query parameters are not allowed; use a Resolver")


def load_candidates():
    catalog = read_json(ROOT / "catalog" / "channels.json")
    channel_ids = {row["id"] for row in catalog.get("channels", [])}
    if not channel_ids:
        raise ValueError("catalog/channels.json must contain at least one channel")
    candidate_ids: set[str] = set()
    by_channel: dict[str, list[dict]] = {channel_id: [] for channel_id in channel_ids}
    registry = ProviderRegistry([StaticCandidateProvider()])
    for path, payload in registry.discover(ROOT / "candidates", catalog):
        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("channels"), list):
            raise ValueError(f"{path.relative_to(ROOT)}: schemaVersion=1 and channels[] are required")
        seen_channels: set[str] = set()
        for channel in payload["channels"]:
            channel_id = channel.get("channelId")
            if channel_id not in channel_ids:
                raise ValueError(f"{path.relative_to(ROOT)}: unknown channelId {channel_id!r}")
            if channel_id in seen_channels:
                raise ValueError(f"{path.relative_to(ROOT)}: duplicate channelId {channel_id}")
            seen_channels.add(channel_id)
            sources = channel.get("sources", [])
            if not isinstance(sources, list) or (sources and len(sources) not in range(1, 6)):
                raise ValueError(f"{path.relative_to(ROOT)}: {channel_id} must have 0 or 1-5 candidate sources")
            for source in sources:
                source_id = source.get("id", "")
                if not source_id or source_id in candidate_ids:
                    raise ValueError(f"{path.relative_to(ROOT)}: missing or duplicate source id {source_id!r}")
                candidate_ids.add(source_id)
                if source.get("channelId", channel_id) != channel_id:
                    raise ValueError(f"{source_id}: channelId does not match its parent")
                if source.get("sourceClass") == "COMMUNITY_SOURCE":
                    if not str(source.get("providerId", "")).strip():
                        raise ValueError(f"{source_id}: community source must name its provider")
                    if source.get("authorization"):
                        raise ValueError(f"{source_id}: community source must not claim authorization")
                elif not isinstance(source.get("authorization"), str) or len(source["authorization"].strip()) < 8:
                    raise ValueError(f"{source_id}: a public license/permission reference is required")
                protocol = str(source.get("protocol", "")).upper()
                source_type = str(source.get("type", "STATIC")).upper()
                if protocol not in ALLOWED_PROTOCOLS or source_type not in ALLOWED_TYPES:
                    raise ValueError(f"{source_id}: unsupported protocol/type")
                if source_type == "STATIC":
                    require_https_public_url(source.get("url", ""), source_id)
                    if source.get("sourceClass") == "COMMUNITY_SOURCE" and urlsplit(source["url"]).query:
                        raise ValueError(f"{source_id}: community URL must not contain query credentials")
                if not isinstance(source.get("priority", 0), int) or source.get("priority", 0) < 1:
                    raise ValueError(f"{source_id}: priority must be a positive integer")
                if not isinstance(source.get("quality", "AUTO"), str):
                    raise ValueError(f"{source_id}: quality must be a string")
                if source.get("enabled", True) not in (True, False):
                    raise ValueError(f"{source_id}: enabled must be boolean")
                by_channel[channel_id].append(source)
    return catalog, by_channel


def validate_sources_payload(payload: dict, known_channel_ids: set[str]) -> None:
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("channels"), dict):
        raise ValueError("public/sources.json must use schemaVersion=1 and channels object")
    if set(payload["channels"]) != known_channel_ids:
        missing = sorted(known_channel_ids - set(payload["channels"]))
        extra = sorted(set(payload["channels"]) - known_channel_ids)
        raise ValueError(f"sources channel set mismatch; missing={missing}, extra={extra}")
    ids: set[str] = set()
    for channel_id, channel in payload["channels"].items():
        if channel.get("status") not in {"AVAILABLE", "NO_SOURCE", "OFFLINE"}:
            raise ValueError(f"{channel_id}: invalid channel status")
        sources = channel.get("sources")
        if not isinstance(sources, list) or len(sources) > 5:
            raise ValueError(f"{channel_id}: sources must be a list with at most 5 items")
        healthy = 0
        for source in sources:
            source_id = source.get("id", "")
            if not source_id or source_id in ids:
                raise ValueError(f"duplicate or missing published source id {source_id!r}")
            ids.add(source_id)
            if source.get("channelId") != channel_id:
                raise ValueError(f"{source_id}: published channelId mismatch")
            if source.get("type") != "STATIC":
                raise ValueError(f"{source_id}: DYNAMIC sources cannot be published in phase 1")
            require_https_public_url(source.get("url", ""), source_id)
            if source.get("sourceClass") == "COMMUNITY_SOURCE" and urlsplit(source["url"]).query:
                raise ValueError(f"{source_id}: community URL must not contain query credentials")
            if source.get("protocol") not in ALLOWED_PROTOCOLS:
                raise ValueError(f"{source_id}: unsupported protocol")
            if source.get("sourceClass") == "COMMUNITY_SOURCE" and not source.get("providerId"):
                raise ValueError(f"{source_id}: community provider is missing")
            if source.get("health") not in {"HEALTHY", "DEGRADED", "OFFLINE", "EXPIRED"}:
                raise ValueError(f"{source_id}: invalid published health state")
            healthy += source.get("health") == "HEALTHY"
        if channel["status"] == "AVAILABLE" and healthy == 0:
            raise ValueError(f"{channel_id}: AVAILABLE requires a HEALTHY source")
        if channel["status"] == "NO_SOURCE" and sources:
            raise ValueError(f"{channel_id}: NO_SOURCE cannot include any sources")
        if channel["status"] == "OFFLINE" and (not sources or healthy > 0):
            raise ValueError(f"{channel_id}: OFFLINE requires retained unhealthy source metadata")


def validate_repository(include_published: bool = True) -> None:
    catalog, _ = load_candidates()
    channels = catalog["channels"]
    ids = [row.get("id") for row in channels]
    numbers = [row.get("number") or row.get("logicalNumber") for row in channels]
    if len(set(ids)) != len(ids) or any(not item for item in ids):
        raise ValueError("catalog channel ids must be present and unique")
    if len(set(numbers)) != len(numbers) or any(not item for item in numbers):
        raise ValueError("catalog channel numbers must be present and unique")
    category_ids = {row.get("id") for row in catalog.get("categories", [])}
    for channel in channels:
        if channel.get("enabled", True) and not channel.get("name"):
            raise ValueError(f"{channel.get('id')}: channel name is required")
        if set(channel.get("categoryIds", [])) - category_ids:
            raise ValueError(f"{channel['id']}: references an unknown category")
        if "sources" in channel or "streamUrl" in channel or "streamProtocol" in channel:
            raise ValueError(f"{channel['id']}: catalog must not contain playback URLs or sources")
    public_sources = ROOT / "public" / "sources.json"
    if include_published and public_sources.exists():
        validate_sources_payload(read_json(public_sources), set(ids))
    manifest_path = ROOT / "public" / "manifest.json"
    if include_published and manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("schemaVersion") != 1:
            raise ValueError("public/manifest.json schemaVersion must be 1")
        for key in ("channelUrl", "sourceUrl"):
            value = manifest.get(key, "")
            if not value:
                raise ValueError(f"manifest {key} is required")
            if urlsplit(value).scheme:
                require_https_public_url(value, f"manifest.{key}")


if __name__ == "__main__":
    validate_repository(include_published="--inputs-only" not in sys.argv)
    print("config valid")
