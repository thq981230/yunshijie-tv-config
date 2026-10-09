from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from providers.remote_subscription import RemoteSubscriptionProvider
from providers.registry import OfficialProviderRegistry
from validate_config import ROOT, read_json

OUTPUT = ROOT / "candidates" / "subscriptions.generated.json"


def load_feed_specs(raw: str) -> list[dict]:
    if not raw.strip():
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(feed, dict) for feed in value):
        raise ValueError("YUNSHIJIE_SUBSCRIPTION_FEEDS_JSON must be a JSON array of feed objects")
    if len(value) > 20:
        raise ValueError("at most 20 subscription feeds are supported")
    return value


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
    except Exception as error:
        # Error text from HTTP libraries can contain a URL. Keep workflow logs credential-safe.
        print(f"subscription discovery failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(2)

