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
    previous_channels = (previous or {}).get("channels", {})
    previous_available_ids = [channel_id for channel_id, channel in previous_channels.items()
                              if channel.get("status") == "AVAILABLE"]
    previous_source_ids = {
        source.get("id")
        for channel in previous_channels.values()
        for source in channel.get("sources", [])
    }
    failed_previous = [row for row in checked
                       if row.get("sourceId") in previous_source_ids and not row.get("lastCheckSucceeded", False)]
    invalid_previous_content = bool(failed_previous) and all(
        any(marker in str(row.get("detail", "")) for marker in
            ("HLS_MANIFEST_INVALID", "HLS_VIDEO_TRACK_MISSING", "VIDEO_TRACK_UNVERIFIED"))
        for row in failed_previous
    )
    # A stricter content check may invalidate an earlier false positive. Publish that
    # correction if another source is still proven healthy; retain the outage guard
    # for network-wide failures and for runs with no playable source at all.
    prior_playback_still_proven = bool(previous_available_ids) and all(
        any(
            old_source.get("health") == "HEALTHY" and
            old_source.get("id") in {candidate.get("id") for candidate in groups.get(channel_id, [])} and
            rows.get(old_source.get("id"), {}).get("lastCheckTime") == health.get("checkedAt") and
            rows.get(old_source.get("id"), {}).get("lastCheckSucceeded", False)
            for old_source in previous_channels.get(channel_id, {}).get("sources", [])
        )
        for channel_id in previous_available_ids
    )
    if checked and failed / len(checked) >= 0.8 and not (
        (invalid_previous_content and any(row.get("lastCheckSucceeded", False) for row in checked))
        or prior_playback_still_proven
    ):
        raise RuntimeError(f"refusing promotion: {failed}/{len(checked)} candidates failed (>=80%); keep Last Known Good config")
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
        healthy.sort(key=lambda pair: (-source_score(pair[0], pair[1]),
                                       int(pair[0].get("priority", 100)), pair[0]["id"]))
        demoted.sort(key=lambda pair: (int(pair[1].get("failCount", 0)), -source_score(pair[0], pair[1]),
                                       int(pair[0].get("priority", 100)), pair[0]["id"]))
        offline.sort(key=lambda pair: (int(pair[1].get("failCount", 0)), int(pair[0].get("priority", 100)), pair[0]["id"]))
        selected_healthy = select_diverse(healthy, 5)
        published = []
        ranked_healthy = selected_healthy + [pair for pair in healthy if pair not in selected_healthy]
        for candidate, state in ranked_healthy + demoted + offline:
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
                "consecutiveFailureCount": state.get("consecutiveFailureCount", 0),
                "consecutiveSuccessCount": state.get("consecutiveSuccessCount", 0),
                "firstSegmentMs": state.get("firstSegmentMs"),
            })
            if candidate.get("sourceClass"):
                published[-1]["sourceClass"] = candidate["sourceClass"]
            if candidate.get("providerId"):
                published[-1]["providerId"] = candidate["providerId"]
            if candidate.get("sourceProviders"):
                published[-1]["sourceProviders"] = candidate["sourceProviders"]
        has_healthy = any(source["health"] == "HEALTHY" for source in published)
        channels[channel_id] = {
            "status": "AVAILABLE" if has_healthy else ("OFFLINE" if published else "NO_SOURCE"),
            "sources": published[:5],
        }

    old_map = (previous or {}).get("channels", {})
    old_available = [channel_id for channel_id, row in old_map.items() if row.get("status") == "AVAILABLE"]
    new_available = [channel_id for channel_id, row in channels.items() if row.get("status") == "AVAILABLE"]
    if old_available and len(new_available) / len(old_available) < 0.6:
        lost = [channel_id for channel_id in old_available if channels.get(channel_id, {}).get("status") != "AVAILABLE"]
        verified_bad = all(previous_channel_invalidated(channel_id, old_map, rows, health.get("checkedAt"))
                           for channel_id in lost)
        if not (lost and verified_bad):
            raise RuntimeError(f"refusing promotion: AVAILABLE dropped from {len(old_available)} to {len(new_available)} (>40%); keep Last Known Good config")
    version = int((previous or {}).get("version", 0))
    if channels != old_map:
        version += 1
    return {"schemaVersion": 1, "version": max(version, 1), "generatedAt": utc_now(), "channels": channels}


def quality_rank(value: str) -> int:
    normalized = str(value).upper()
    return {"8K": 8000, "4K": 4000, "2160P": 2160, "1080P": 1080, "FHD": 1080,
            "720P": 720, "HD": 720, "480P": 480, "360P": 360, "SD": 360}.get(normalized, 0)


def source_score(candidate: dict, state: dict) -> int:
    """Rank validated candidates using health, provider tier, latency, and history."""
    score = 50  # HEALTHY and checked successfully
    scheme = str(candidate.get("url", "")).partition(":")[0].casefold()
    if scheme == "https":
        score += 10
    score += max(0, round((70 - int(candidate.get("priority", 100))) / 3))
    quality = quality_rank(candidate.get("quality", "AUTO"))
    if quality >= 720:
        score += 5
    if quality >= 1080:
        score += 8
    successes = int(state.get("successCount") or 0)
    failures = int(state.get("failCount") or 0)
    if successes / max(1, successes + failures) > 0.95:
        score += 15
    if int(state.get("latencyMs") or 2**31) < 500:
        score += 10
    if int(state.get("consecutiveFailureCount") or 0):
        score -= 20
    if state.get("status") == "DEGRADED":
        score -= 30
    if int(state.get("consecutiveSuccessCount") or 0) >= 5:
        score += 3
    return score


def select_diverse(candidates: list[tuple[dict, dict]], limit: int) -> list[tuple[dict, dict]]:
    selected: list[tuple[dict, dict]] = []
    used_providers: set[str] = set()
    for distinct_provider_pass in (True, False):
        for pair in candidates:
            if len(selected) >= limit:
                return selected
            provider = str(pair[0].get("providerId") or "")
            if distinct_provider_pass and provider in used_providers:
                continue
            if pair in selected:
                continue
            selected.append(pair)
            used_providers.update(pair[0].get("sourceProviders") or [provider])
    return selected


def previous_channel_invalidated(channel_id: str, previous_channels: dict, rows: dict, checked_at: str | None) -> bool:
    old = previous_channels.get(channel_id, {})
    previous_healthy = [source for source in old.get("sources", []) if source.get("health") == "HEALTHY"]
    markers = ("HLS_MANIFEST_INVALID", "HLS_VIDEO_TRACK_MISSING", "VIDEO_TRACK_UNVERIFIED")
    return bool(previous_healthy) and all(
        (state := rows.get(source.get("id"), {})).get("lastCheckTime") == checked_at and
        not state.get("lastCheckSucceeded", False) and
        any(marker in str(state.get("detail", "")) for marker in markers)
        for source in previous_healthy
    )


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
