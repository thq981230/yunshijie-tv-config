from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from providers.remote_subscription import RemoteSubscriptionProvider
from providers.registry import OfficialProviderRegistry
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
    return json.dumps(feeds, ensure_ascii=False, separators=(",", ":"))


def discover(feeds: list[dict], catalog: dict) -> tuple[dict, dict]:
    provider = RemoteSubscriptionProvider(feeds)
    registry = OfficialProviderRegistry([provider])
    rows = registry.discover(catalog)
    channel_ids = [row["id"] for row in catalog["channels"]]
    grouped: dict[str, list[dict]] = {channel_id: [] for channel_id in channel_ids}
    seen: dict[str, set[str]] = {channel_id: set() for channel_id in channel_ids}
    for row in rows:
        channel_id = row.channel_id
        source = row.source
        url = source["url"]
        if url in seen[channel_id] or len(grouped[channel_id]) >= 5:
            continue
        seen[channel_id].add(url)
        grouped[channel_id].append(source)
    payload = {"schemaVersion": 1, "channels": [
        {"channelId": channel_id, "sources": grouped[channel_id]} for channel_id in channel_ids
    ]}
    return payload, provider.last_stats


def main() -> int:
    raw_feeds = os.environ.get("YUNSHIJIE_SUBSCRIPTION_FEEDS_JSON", "")
    if not raw_feeds:
        raw_feeds = github_action_feed_config()
    feeds = load_feed_specs(raw_feeds)
    if not feeds:
        OUTPUT.unlink(missing_ok=True)
        print("subscription feeds configured=0; keeping reviewed candidate files only")
        return 0

    catalog = read_json(ROOT / "catalog" / "channels.json")
    payload, stats = discover(feeds, catalog)
    source_count = sum(len(row["sources"]) for row in payload["channels"])
    if source_count:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temp = OUTPUT.with_name(OUTPUT.name + ".new")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        json.loads(temp.read_text(encoding="utf-8"))
        temp.replace(OUTPUT)
    else:
        OUTPUT.unlink(missing_ok=True)
    # Feed URLs and stream URLs are intentionally omitted from logs.
    print("subscription discovery: " + " ".join(f"{key}={value}" for key, value in stats.items()))
    print(f"generated authorized candidates={source_count} channels={sum(bool(row['sources']) for row in payload['channels'])}")
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
