from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from check_sources import atomic_json
from validate_config import ROOT, load_candidates, validate_sources_payload


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def promote(catalog: dict, groups: dict[str, list[dict]], health: dict, previous: dict | None) -> dict:
    rows = {row["sourceId"]: row for row in health.get("sources", [])}
    checked = [row for row in rows.values() if row.get("lastCheckTime") == health.get("checkedAt")]
    failed = sum(not row.get("lastCheckSucceeded", False) for row in checked)
    if checked and failed / len(checked) >= 0.8:
        raise RuntimeError(f"refusing promotion: {failed}/{len(checked)} candidates failed (>=80%); keep Last Known Good config")

    previous_channels = (previous or {}).get("channels", {})
    channels: dict[str, dict] = {}
    for channel in catalog["channels"]:
        channel_id = channel["id"]
        candidate_by_id = {source["id"]: source for source in groups.get(channel_id, [])
                           if source.get("enabled", True) and source.get("type", "STATIC") == "STATIC"}
        healthy: list[tuple[dict, dict]] = []
        demoted: list[tuple[dict, dict]] = []
        offline: list[tuple[dict, dict]] = []
        prior_ids = {source.get("id") for source in previous_channels.get(channel_id, {}).get("sources", [])}
        for source_id, candidate in candidate_by_id.items():
            state = rows.get(source_id, {})
            if state.get("status") == "HEALTHY" and state.get("lastCheckSucceeded"):
                healthy.append((candidate, state))
            elif source_id in prior_ids and state.get("status") in {"HEALTHY", "DEGRADED"}:
                # A transient failure must not erase a last-known-good URL; keep it behind passing sources.
                demoted.append((candidate, state))
            elif source_id in prior_ids and state.get("status") in {"OFFLINE", "EXPIRED"}:
                # Retain status metadata for the UI, but the app will never attempt an offline source.
                offline.append((candidate, state))
        def order(pair):
            successes = int(pair[1].get("successCount") or 0)
            failures = int(pair[1].get("failCount") or 0)
            success_rate = successes / max(1, successes + failures)
            return (int(pair[0].get("priority", 100)), int(pair[1].get("latencyMs") or 2**31),
                    -success_rate, -quality_rank(pair[0].get("quality", "AUTO")), pair[0]["id"])
        healthy.sort(key=order)
        demoted.sort(key=order)
        offline.sort(key=order)
        published = []
        for candidate, state in healthy + demoted + offline:
            published.append({
                "id": candidate["id"],
                "channelId": channel_id,
                "protocol": candidate["protocol"].upper(),
                "type": "STATIC",
                "url": candidate["url"],
                "quality": candidate.get("quality", "AUTO"),
                "priority": candidate.get("priority", 100),
                "enabled": True,
                "health": "HEALTHY" if state.get("lastCheckSucceeded") else (
                    state.get("status") if state.get("status") in {"DEGRADED", "OFFLINE", "EXPIRED"} else "DEGRADED"),
                "headers": candidate.get("headers", {}),
                "latencyMs": state.get("latencyMs"),
                "httpCode": state.get("httpCode"),
                "lastCheckTime": state.get("lastCheckTime"),
                "failCount": state.get("failCount", 0),
                "successCount": state.get("successCount", 0),
            })
            if candidate.get("sourceClass"):
                published[-1]["sourceClass"] = candidate["sourceClass"]
            if candidate.get("providerId"):
                published[-1]["providerId"] = candidate["providerId"]
        has_healthy = any(source["health"] == "HEALTHY" for source in published)
        channels[channel_id] = {
            "status": "AVAILABLE" if has_healthy else ("OFFLINE" if published else "NO_SOURCE"),
            "sources": published[:5],
        }

    old_map = (previous or {}).get("channels", {})
    version = int((previous or {}).get("version", 0))
    if channels != old_map:
        version += 1
    return {"schemaVersion": 1, "version": max(version, 1), "generatedAt": utc_now(), "channels": channels}


def quality_rank(value: str) -> int:
    normalized = str(value).upper()
    return {"8K": 8000, "4K": 4000, "2160P": 2160, "1080P": 1080, "FHD": 1080,
            "720P": 720, "HD": 720, "480P": 480, "360P": 360, "SD": 360}.get(normalized, 0)


def main() -> int:
    catalog, groups = load_candidates()
    health_path = ROOT / "health" / "latest.json"
    if not health_path.exists():
        raise RuntimeError("health/latest.json missing; run check_sources.py first")
    health = json.loads(health_path.read_text(encoding="utf-8"))
    output = ROOT / "public" / "sources.json"
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
    candidate = promote(catalog, groups, health, previous)
    validate_sources_payload(candidate, {row["id"] for row in catalog["channels"]})

    temp = output.with_name("sources.new.json")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(candidate, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    # The complete temp file has been parsed and validated before replacing the published file.
    json.loads(temp.read_text(encoding="utf-8"))
    temp.replace(output)
    archive = ROOT / "releases" / f"sources-{candidate['version']}.json"
    if not archive.exists() or json.loads(archive.read_text(encoding="utf-8")).get("channels") != candidate["channels"]:
        archive_temp = archive.with_name(archive.name + ".new")
        shutil.copyfile(output, archive_temp)
        archive_temp.replace(archive)
    available = sum(row["status"] == "AVAILABLE" for row in candidate["channels"].values())
    print(f"published version={candidate['version']} availableChannels={available}/{len(candidate['channels'])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"promotion rejected: {error}", file=sys.stderr)
        sys.exit(2)
