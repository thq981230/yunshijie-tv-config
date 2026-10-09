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
SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "authorization", "signature", "sig", "sign", "expires", "expire",
    "key", "auth_key", "txsecret", "tx_secret", "wstime", "wssecret", "ws_secret", "hdnts", "policy",
    "jwt", "secret", "accesskey", "access_key", "credential", "credentials",
}


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
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("stream URL must be public HTTP(S) without embedded credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("stream URL host must be public")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("stream URL host must be public")
    query_keys = {urllib.parse.unquote_plus(part.split("=", 1)[0]).casefold() for part in parsed.query.split("&") if part}
    if query_keys & SENSITIVE_QUERY_KEYS:
        raise ValueError("stream URL contains an expiring/authenticated query; it cannot be published")
    if parsed.fragment:
        raise ValueError("stream URL fragment cannot be published")


def _safe_entry_headers(entry: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    values = entry.get("headers")
    if isinstance(values, dict):
        for key, value in values.items():
            name = str(key).strip().casefold()
            if name in {"user-agent", "referer", "referrer"} and value is not None:
                result["Referer" if name in {"referer", "referrer"} else "User-Agent"] = str(value).strip()
    for key, header in (("http-user-agent", "User-Agent"), ("http-referrer", "Referer"),
                        ("http-referer", "Referer")):
        if entry.get(key):
            result[header] = str(entry[key]).strip()
    for key, value in result.items():
        if not value or "\r" in value or "\n" in value:
            raise ValueError("stream headers are invalid")
        if key == "User-Agent" and any(marker in value.casefold() for marker in
                                       ("bearer ", "authorization:", "cookie:", "token=", "auth=")):
            raise ValueError("credential-bearing User-Agent cannot be published")
        if key == "Referer":
            try:
                validate_publishable_stream_url(value)
            except ValueError as error:
                raise ValueError("stream referrer is not a public, credential-free URL") from error
    forbidden = {str(key).casefold() for key in (values or {})} & {"authorization", "cookie", "proxy-authorization"}
    if forbidden:
        raise ValueError("credential headers cannot be published")
    return result


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
    headers: dict[str, str] = {}
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
            headers = {}
            pending = True
        elif pending and line.upper().startswith("#EXTVLCOPT:"):
            key, separator, value = line.partition(":")[2].partition("=")
            if separator and key.casefold() in {"http-user-agent", "http-referrer", "http-referer"}:
                headers[key.casefold()] = value.strip().strip('"')
        elif line.startswith("#"):
            continue
        elif pending:
            rows.append({**attributes, **headers, "headers": headers.copy(), "name": display_name, "url": line})
            attributes = {}
            headers = {}
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
    """Imports approved public streams or explicitly labelled community test streams."""

    provider_id = "remote-subscription"

    def __init__(self, feeds: list[dict], fetcher: Callable[[str], bytes] = fetch_subscription,
                 cached_sources: Mapping[str, list[dict]] | None = None):
        self.feeds = feeds
        self.fetcher = fetcher
        self.cached_sources = {key: list(value) for key, value in (cached_sources or {}).items()}
        self.last_stats = {"feeds": 0, "feedSuccess": 0, "feedFailed": 0, "fallbackFeeds": 0,
                           "entries": 0, "matched": 0, "published": 0, "rejected": 0,
                           "localMulticast": 0, "withoutPublicationPermission": 0}
        self._discovered: list[DiscoveredSource] = []
        self.last_unmatched: list[dict] = []
        self.feed_reports: list[dict] = []

    def discover(self, catalog: dict, channel_ids: set[str] | None = None) -> list[DiscoveredSource]:
        matcher = ChannelMatcher(catalog)
        found: list[DiscoveredSource] = []
        stats = {key: 0 for key in self.last_stats}
        matched_channels: set[str] = set()
        unmatched: dict[tuple[str, str, str], dict] = {}
        reports: list[dict] = []
        for feed_index, feed in enumerate(self.feeds):
            stats["feeds"] += 1
            feed_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(feed.get("providerId") or f"subscription-{feed_index + 1}"))[:48].strip("-").lower()
            if not feed_id:
                continue
            authorization = str(feed.get("authorization") or feed.get("publicationAuthorization") or "").strip()
            community_test = feed.get("communityTest") is True
            if not community_test and (feed.get("redistributable") is not True or len(authorization) < 8):
                stats["withoutPublicationPermission"] += 1
                continue
            try:
                feed_url = str(feed.get("url") or "").strip()
                validate_subscription_endpoint(feed_url)
                entries = parse_subscription(self.fetcher(feed_url))
                if not entries:
                    raise ValueError("feed returned no entries")
                stats["feedSuccess"] += 1
            except Exception as error:
                stats["feedFailed"] += 1
                cached = self._cached_for_feed(feed_id)
                found.extend(cached)
                if cached:
                    stats["fallbackFeeds"] += 1
                reports.append({"providerId": feed_id, "status": "PROVIDER_DEGRADED", "parsedEntries": 0,
                                "candidateSources": len(cached), "cacheUsed": bool(cached),
                                "error": type(error).__name__})
                continue
            stats["entries"] += len(entries)
            seen_urls: set[tuple[str, str]] = set()
            feed_found: list[DiscoveredSource] = []
            feed_matched = 0
            base_priority = int(feed.get("priority", 100))
            if base_priority < 1:
                raise ValueError("subscription priority must be positive")
            for position, entry in enumerate(entries, start=1):
                match = matcher.match(entry)
                if match is None:
                    if entry.get("name") or entry.get("tvg-id") or entry.get("tvg-name"):
                        key = (feed_id, str(entry.get("tvg-id") or ""), str(entry.get("name") or entry.get("tvg-name") or ""))
                        unmatched.setdefault(key, {"providerId": feed_id, "tvgId": key[1], "name": key[2]})
                    continue
                if channel_ids is not None and match.channel_id not in channel_ids:
                    continue
                stats["matched"] += 1
                feed_matched += 1
                channel_id = match.channel_id
                matched_channels.add(channel_id)
                url = str(entry.get("url") or entry.get("streamUrl") or entry.get("stream_url") or entry.get("uri") or "").strip()
                parsed_url = urllib.parse.urlsplit(url)
                host = parsed_url.hostname or ""
                try:
                    address = ipaddress.ip_address(host.strip("[]"))
                except ValueError:
                    address = None
                if parsed_url.scheme.casefold() in {"rtp", "udp"} and (address is None or address.is_multicast):
                    stats["localMulticast"] += 1
                    stats["rejected"] += 1
                    continue
                try:
                    validate_publishable_stream_url(url)
                    headers = _safe_entry_headers(entry)
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
                priority = int(item_priority) if isinstance(item_priority, int) and item_priority > 0 else base_priority
                source = {
                    "id": f"sub-{feed_id}-{channel_id}-{digest}",
                    "channelId": channel_id,
                    "protocol": protocol,
                    "type": "STATIC",
                    "url": url,
                    "priority": priority,
                    "quality": str(entry.get("quality") or entry.get("resolution") or "AUTO").upper(),
                    "enabled": True,
                    "sourceClass": "COMMUNITY_SOURCE" if community_test else "AUTHORIZED",
                    "providerId": feed_id,
                    "sourceProviders": [feed_id],
                    "matchMethod": match.method,
                    "matchConfidence": match.confidence,
                    "headers": headers,
                }
                if not community_test:
                    source["authorization"] = authorization
                feed_found.append(DiscoveredSource(channel_id, source, self.provider_id, f"feed:{feed_id}"))
                stats["published"] += 1
            if feed_found:
                found.extend(feed_found)
                reports.append({"providerId": feed_id, "status": "SUCCESS", "parsedEntries": len(entries),
                                "matchedEntries": feed_matched, "candidateSources": len(feed_found), "cacheUsed": False})
            else:
                cached = self._cached_for_feed(feed_id)
                found.extend(cached)
                if cached:
                    stats["fallbackFeeds"] += 1
                reports.append({"providerId": feed_id, "status": "PROVIDER_DEGRADED" if cached else "EMPTY",
                                "parsedEntries": len(entries), "matchedEntries": feed_matched,
                                "candidateSources": len(cached), "cacheUsed": bool(cached),
                                "error": "NO_PUBLISHABLE_MATCH"})
        stats["matchedChannels"] = len(matched_channels)
        stats["candidateChannels"] = len({row.channel_id for row in found})
        self.last_stats = stats
        self.last_unmatched = sorted(unmatched.values(), key=lambda row: (row["providerId"], row["tvgId"], row["name"]))[:20000]
        self.feed_reports = reports
        # Preserve priority from feed ordering, with stable source-id tie breaking.
        self._discovered = sorted(found, key=lambda row: (row.channel_id, row.source["priority"], row.source["id"]))
        return list(self._discovered)

    def _cached_for_feed(self, feed_id: str) -> list[DiscoveredSource]:
        rows = []
        for source in self.cached_sources.get(feed_id, []):
            try:
                if source.get("sourceClass") != "COMMUNITY_SOURCE" or source.get("providerId") != feed_id:
                    continue
                validate_publishable_stream_url(str(source.get("url") or ""))
                headers = _safe_entry_headers(source)
                if source.get("headers") != headers:
                    source = {**source, "headers": headers}
                channel_id = str(source.get("channelId") or "")
                if not channel_id:
                    continue
                rows.append(DiscoveredSource(channel_id, dict(source), self.provider_id, f"cache:{feed_id}"))
            except ValueError:
                continue
        return rows

    def resolve(self, channel_id: str, catalog: dict) -> list[DiscoveredSource]:
        if self._discovered:
            return [row for row in self._discovered if row.channel_id == channel_id]
        return [row for row in self.discover(catalog) if row.channel_id == channel_id]

    def health_check(self, source: Mapping[str, object]) -> dict:
        from check_sources import probe
        return probe(dict(source))
