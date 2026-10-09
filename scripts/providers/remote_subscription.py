from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping

from providers.base import DiscoveredSource
from providers.channel_matcher import ChannelMatcher
from providers.registry import SubscriptionProviderAdapter

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 20_000
SENSITIVE_QUERY_KEYS = {"token", "auth", "authorization", "signature", "sig", "expires", "expire", "key"}


def validate_subscription_endpoint(url: str, *, resolve_dns: bool = False) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("subscription feed must use an absolute HTTPS URL")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("subscription feed host must be public")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("subscription feed host must be public")
    if resolve_dns:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0].split("%", 1)[0]).is_global for row in addresses):
            raise ValueError("subscription feed DNS must resolve only to public addresses")


def validate_publishable_stream_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("stream URL must be public HTTPS without embedded credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("stream URL host must be public")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("stream URL host must be public")
    query_keys = {part.split("=", 1)[0].lower() for part in parsed.query.split("&") if part}
    if query_keys & SENSITIVE_QUERY_KEYS:
        raise ValueError("stream URL contains an expiring/authenticated query; it cannot be published")


class _SafeFeedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_subscription_endpoint(new_url, resolve_dns=True)
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch_subscription(url: str) -> bytes:
    validate_subscription_endpoint(url, resolve_dns=True)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, application/json, text/plain",
        "User-Agent": "YunshijieTV-SubscriptionProvider/1.0",
    })
    opener = urllib.request.build_opener(_SafeFeedRedirectHandler)
    try:
        with opener.open(request, timeout=15) as response:
            validate_subscription_endpoint(response.geturl(), resolve_dns=True)
            body = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"subscription feed returned HTTP {error.code}") from error
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("subscription feed exceeds 8 MiB")
    if not body:
        raise ValueError("subscription feed is empty")
    return body


def parse_m3u(text: str) -> list[dict]:
    rows: list[dict] = []
    attributes: dict[str, str] = {}
    display_name = ""
    pending = False
    for raw in text.replace("\ufeff", "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            metadata = line.partition(":")[2]
            head, comma, title = metadata.partition(",")
            attributes = {}
            for match in re.finditer(r'([\w-]+)=(?:"([^"]*)"|([^,\s]+))', head):
                attributes[match.group(1).casefold()] = match.group(2) if match.group(2) is not None else match.group(3)
            display_name = title.strip() if comma else ""
            pending = True
        elif line.startswith("#"):
            continue
        elif pending:
            rows.append({**attributes, "name": display_name, "url": line})
            attributes = {}
            display_name = ""
            pending = False
            if len(rows) > MAX_ENTRIES:
                raise ValueError("subscription feed has too many entries")
    return rows


def parse_json_feed(text: str) -> list[dict]:
    payload = json.loads(text)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload.get(key) for key in ("channels", "streams", "items", "data")
                     if isinstance(payload.get(key), list)), None)
    else:
        rows = None
    if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
        raise ValueError("JSON subscription must contain a bounded channels[], streams[], items[], or data[] list")
    return [row for row in rows if isinstance(row, dict)]


def parse_subscription(body: bytes | str) -> list[dict]:
    text = body.decode("utf-8-sig", errors="strict") if isinstance(body, bytes) else body.lstrip("\ufeff")
    if text.lstrip().startswith("#EXTM3U"):
        return parse_m3u(text)
    return parse_json_feed(text)


class RemoteSubscriptionProvider(SubscriptionProviderAdapter):
    """Imports M3U/M3U8/JSON feeds, but publishes only explicitly redistributable public streams."""

    provider_id = "remote-subscription"

    def __init__(self, feeds: list[dict], fetcher: Callable[[str], bytes] = fetch_subscription):
        self.feeds = feeds
        self.fetcher = fetcher
        self.last_stats = {"feeds": 0, "entries": 0, "matched": 0, "published": 0, "rejected": 0,
                           "withoutPublicationPermission": 0}
        self._discovered: list[DiscoveredSource] = []

    def discover(self, catalog: dict) -> list[DiscoveredSource]:
        matcher = ChannelMatcher(catalog)
        found: list[DiscoveredSource] = []
        stats = {key: 0 for key in self.last_stats}
        for feed_index, feed in enumerate(self.feeds):
            stats["feeds"] += 1
            feed_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(feed.get("providerId") or f"subscription-{feed_index + 1}"))[:48].strip("-").lower()
            if not feed_id:
                continue
            authorization = str(feed.get("authorization") or feed.get("publicationAuthorization") or "").strip()
            if feed.get("redistributable") is not True or len(authorization) < 8:
                stats["withoutPublicationPermission"] += 1
                continue
            feed_url = str(feed.get("url") or "").strip()
            validate_subscription_endpoint(feed_url)
            entries = parse_subscription(self.fetcher(feed_url))
            stats["entries"] += len(entries)
            seen_urls: set[tuple[str, str]] = set()
            base_priority = int(feed.get("priority", 100))
            if base_priority < 1:
                raise ValueError("subscription priority must be positive")
            for position, entry in enumerate(entries, start=1):
                match = matcher.match(entry)
                if match is None:
                    continue
                stats["matched"] += 1
                channel_id = match.channel_id
                url = str(entry.get("url") or entry.get("streamUrl") or entry.get("stream_url") or entry.get("uri") or "").strip()
                try:
                    validate_publishable_stream_url(url)
                except ValueError:
                    stats["rejected"] += 1
                    continue
                dedupe_key = (channel_id, url)
                if dedupe_key in seen_urls:
                    continue
                seen_urls.add(dedupe_key)
                protocol = str(entry.get("protocol") or "").upper()
                if not protocol:
                    protocol = "DASH" if urllib.parse.urlsplit(url).path.lower().endswith(".mpd") else "HLS"
                if protocol not in {"HLS", "DASH"}:
                    stats["rejected"] += 1
                    continue
                digest = hashlib.sha256(f"{channel_id}\0{url}".encode("utf-8")).hexdigest()[:10]
                item_priority = entry.get("priority")
                priority = int(item_priority) if isinstance(item_priority, int) and item_priority > 0 else base_priority + position - 1
                source = {
                    "id": f"sub-{feed_id}-{channel_id}-{digest}",
                    "channelId": channel_id,
                    "protocol": protocol,
                    "type": "STATIC",
                    "url": url,
                    "priority": priority,
                    "quality": str(entry.get("quality") or entry.get("resolution") or "AUTO").upper(),
                    "enabled": True,
                    "authorization": authorization,
                    "matchMethod": match.method,
                }
                found.append(DiscoveredSource(channel_id, source, self.provider_id, f"feed:{feed_id}"))
                stats["published"] += 1
        self.last_stats = stats
        # Preserve priority from feed ordering, with stable source-id tie breaking.
        self._discovered = sorted(found, key=lambda row: (row.channel_id, row.source["priority"], row.source["id"]))
        return list(self._discovered)

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]:
        if self._discovered:
            return [row for row in self._discovered if row.channel_id == channel_id]
        return [row for row in self.discover(catalog) if row.channel_id == channel_id]

    def health_check(self, source: Mapping[str, object]) -> dict:
        from check_sources import probe
        return probe(dict(source))
