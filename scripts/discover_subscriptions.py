from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from providers.remote_subscription import RemoteSubscriptionProvider
from providers.base import DiscoveredSource
from providers.source_deduplicator import SourceDeduplicator, canonical_source_key
from validate_config import ROOT, read_json

OUTPUT = ROOT / "candidates" / "subscriptions.generated.json"
OIDC_AUDIENCE = "api://yunshijie-tv-subscriptions"
WORKER_SUBSCRIPTION_API = "https://yunshijie-tv-refresh-api.yunshijie-tv.workers.dev/internal/v1/subscriptions"


class SafeSubscriptionConfigError(RuntimeError):
    """An allow-listed diagnostic that never contains request URLs or credentials."""


def load_feed_specs(raw: str) -> list[dict]:
    if not raw.strip():
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(feed, dict) for feed in value):
        raise ValueError("YUNSHIJIE_SUBSCRIPTION_FEEDS_JSON must be a JSON array of feed objects")
    if len(value) > 20:
        raise ValueError("at most 20 subscription feeds are supported")
    return value


def github_action_feed_config() -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not request_url and not request_token:
        return ""
    if not request_url or not request_token:
        raise SafeSubscriptionConfigError("GitHub Actions OIDC environment is incomplete")
    parsed = urllib.parse.urlsplit(request_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".actions.githubusercontent.com"):
        raise SafeSubscriptionConfigError("GitHub Actions OIDC endpoint is not trusted")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "audience"]
    query.append(("audience", OIDC_AUDIENCE))
    token_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                         urllib.parse.urlencode(query), parsed.fragment))
    request = urllib.request.Request(token_url, headers={"Authorization": f"Bearer {request_token}",
                                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            token_payload = json.loads(response.read(32769))
    except urllib.error.HTTPError as error:
        raise SafeSubscriptionConfigError(f"GitHub OIDC token request returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SafeSubscriptionConfigError("GitHub OIDC token request failed") from error
    oidc_token = str(token_payload.get("value") or "")
    if not oidc_token or len(oidc_token) > 32768:
        raise SafeSubscriptionConfigError("GitHub Actions did not return a bounded OIDC token")

    request = urllib.request.Request(WORKER_SUBSCRIPTION_API,
        headers={"Authorization": f"Bearer {oidc_token}", "Accept": "application/json",
                 "User-Agent": "YunshijieTV-SourceHealth/1.0 (+https://github.com/thq981230/yunshijie-tv-config)"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(65537)
    except urllib.error.HTTPError as error:
        response_body = error.read(1024)
        code = ""
        try:
            error_payload = json.loads(response_body.decode("utf-8"))
            candidate = error_payload.get("error", "")
            reason = error_payload.get("reason", "")
            if candidate in {"OIDC_TOKEN_REQUIRED", "OIDC_IDENTITY_REJECTED", "SERVICE_UNAVAILABLE"}:
                code = candidate
            if reason in {"CLAIM_ISSUER", "CLAIM_AUDIENCE", "CLAIM_REPOSITORY", "CLAIM_REF",
                          "CLAIM_REPOSITORY_ID", "CLAIM_OWNER_ID", "CLAIM_WORKFLOW_REF", "CLAIM_EVENT",
                          "CLAIM_TIME", "SIGNING_KEY_NOT_FOUND", "SIGNATURE_INVALID", "TOKEN_INVALID",
                          "TOKEN_FORMAT", "TOKEN_HEADER"}:
                code = f"{code}:{reason}" if code else reason
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        suffix = f" ({code})" if code else ""
        raise SafeSubscriptionConfigError(f"Worker subscription request returned HTTP {error.code}{suffix}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise SafeSubscriptionConfigError("Worker subscription request failed") from error
    if len(body) > 65536:
        raise SafeSubscriptionConfigError("Worker subscription configuration exceeds 64 KiB")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SafeSubscriptionConfigError("Worker returned invalid subscription JSON") from error
    feeds = payload.get("feeds")
    if not isinstance(feeds, list):
        raise SafeSubscriptionConfigError("Worker returned invalid subscription configuration")
    print(f"subscription config source=Cloudflare Worker OIDC feeds={len(feeds)}")
    return json.dumps(feeds, ensure_ascii=False, separators=(",", ":"))


def discover(feeds: list[dict], catalog: dict, cached_payload: dict | None = None,
             channel_ids: set[str] | None = None) -> tuple[dict, dict, list[dict], list[dict]]:
    cached_by_provider: dict[str, list[dict]] = {}
    for channel in (cached_payload or {}).get("channels", []):
        for source in channel.get("sources", []):
            provider_id = str(source.get("providerId") or "")
            if provider_id:
                cached_by_provider.setdefault(provider_id, []).append(source)
    provider = RemoteSubscriptionProvider(feeds, cached_sources=cached_by_provider)
    rows = provider.discover(catalog, channel_ids=channel_ids)
    known_channel_ids = [row["id"] for row in catalog["channels"]]
    grouped = merge_sources(rows, known_channel_ids)
    payload = {"schemaVersion": 1, "channels": [
        {"channelId": channel_id, "sources": grouped[channel_id]} for channel_id in known_channel_ids
    ]}
    return payload, provider.last_stats, provider.feed_reports, provider.last_unmatched


def merge_sources(rows: list[DiscoveredSource], channel_ids: list[str], limit: int | None = None) -> dict[str, list[dict]]:
    """Deduplicate URLs while preserving provider diversity and the full candidate pool."""
    candidates = {channel_id: [] for channel_id in channel_ids}
    for row in rows:
        if row.channel_id in candidates:
            candidates[row.channel_id].append(row.source)
    grouped: dict[str, list[dict]] = {channel_id: [] for channel_id in channel_ids}
    for channel_id in channel_ids:
        deduped = SourceDeduplicator().merge(
            DiscoveredSource(channel_id, source, "remote-subscription", str(source.get("providerId") or ""))
            for source in candidates[channel_id]
        )
        pool = [row.source for row in sorted(deduped, key=lambda row: (
            int(row.source.get("priority", 100)), str(row.source.get("providerId") or ""), row.source.get("id", "")
        ))]
        if limit is None:
            grouped[channel_id] = pool
            continue
        # Keep at least one URL from each provider before filling the explicit limit.
        selected: list[dict] = []
        selected_keys: set[tuple] = set()
        for distinct_provider_pass in (True, False):
            for source in pool:
                if len(selected) >= limit:
                    break
                key = canonical_source_key(source)
                provider_id = str(source.get("providerId") or "")
                providers = source.get("sourceProviders") or [provider_id]
                if key in selected_keys or (distinct_provider_pass and any(value in {row.get("providerId") for row in selected} for value in providers)):
                    continue
                selected.append(source)
                selected_keys.add(key)
        grouped[channel_id] = selected
    return grouped


def main() -> int:
    raw_feeds = os.environ.get("YUNSHIJIE_SUBSCRIPTION_FEEDS_JSON", "")
    if not raw_feeds:
        raw_feeds = github_action_feed_config()
    feeds = load_feed_specs(raw_feeds)
    if not feeds:
        print("subscription feeds configured=0; keeping the previous provider cache")
        return 0

    catalog = read_json(ROOT / "catalog" / "channels.json")
    previous = read_json(OUTPUT) if OUTPUT.exists() else None
    scope = os.environ.get("REFRESH_SCOPE", "ALL").upper()
    channel_id = os.environ.get("REFRESH_CHANNEL_ID", "").strip()
    category_id = os.environ.get("REFRESH_CATEGORY_ID", "").strip()
    by_id = {row["id"]: row for row in catalog["channels"]}
    if scope == "CHANNEL":
        if channel_id not in by_id:
            raise ValueError("CHANNEL refresh requires a known channelId")
        target_ids = {channel_id}
    elif scope == "CATEGORY":
        target_ids = {row["id"] for row in catalog["channels"] if category_id in row.get("categoryIds", [])}
        if not target_ids:
            raise ValueError("CATEGORY refresh requires a known categoryId")
    else:
        target_ids = None
    payload, stats, reports, unmatched = discover(feeds, catalog, previous, target_ids)
    if target_ids is not None and previous:
        old = {row["channelId"]: row.get("sources", []) for row in previous.get("channels", [])}
        for row in payload["channels"]:
            if row["channelId"] not in target_ids:
                row["sources"] = old.get(row["channelId"], [])
    source_count = sum(len(row["sources"]) for row in payload["channels"])
    if source_count:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temp = OUTPUT.with_name(OUTPUT.name + ".new")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        json.loads(temp.read_text(encoding="utf-8"))
        temp.replace(OUTPUT)
    # Feed URLs and stream URLs are intentionally omitted from logs.
    print("subscription discovery: " + " ".join(f"{key}={value}" for key, value in stats.items()))
    print(f"provider status: {json.dumps(reports, ensure_ascii=False, separators=(',', ':'))}")
    print(f"generated candidate pool={source_count} channels={sum(bool(row['sources']) for row in payload['channels'])}")
    unmatched_path = ROOT / "unmatched_channels.json"
    temp_unmatched = unmatched_path.with_name("unmatched_channels.new.json")
    temp_unmatched.write_text(json.dumps({"generatedAt": datetime.now(timezone.utc).isoformat(),
                                           "items": unmatched}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(temp_unmatched.read_text(encoding="utf-8"))
    temp_unmatched.replace(unmatched_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SafeSubscriptionConfigError as error:
        print(f"subscription discovery failed: {error}", file=sys.stderr)
        sys.exit(2)
    except Exception as error:
        # Error text from HTTP libraries can contain a URL. Keep workflow logs credential-safe.
        print(f"subscription discovery failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(2)
