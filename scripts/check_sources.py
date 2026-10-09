from __future__ import annotations

import json
import ipaddress
import argparse
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from validate_config import ROOT, load_candidates, require_https_public_url

TIMEOUT_SECONDS = 12
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
USER_AGENT = "YunshijieTV-SourceHealth/1.0 (+https://github.com/)"


def assert_public_dns(host: str) -> None:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise OSError("DNS returned no address")
    for address in addresses:
        ip = address[4][0].split("%", 1)[0]
        if not ipaddress.ip_address(ip).is_global:
            raise OSError("DNS resolved to a non-public address")


class SafeHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        require_https_public_url(new_url, "redirect URL")
        host = urllib.parse.urlsplit(new_url).hostname
        if not host:
            raise OSError("redirect URL has no host")
        assert_public_dns(host)
        return super().redirect_request(request, response, code, message, headers, new_url)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch(url: str, byte_limit: int = MAX_MANIFEST_BYTES) -> tuple[int, bytes, str, int]:
    require_https_public_url(url, "probe URL")
    host = urllib.parse.urlsplit(url).hostname
    assert host
    assert_public_dns(host)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-2097151"})
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(SafeHttpsRedirectHandler)
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(byte_limit + 1)
            if len(body) > byte_limit:
                body = body[:byte_limit]
            final_url = response.geturl()
            require_https_public_url(final_url, "redirected URL")
            return response.status, body, final_url, round((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as error:
        return error.code, error.read(8192), error.geturl(), round((time.monotonic() - started) * 1000)


def probe_hls(url: str) -> dict:
    total_latency = 0
    response_code = None
    current = url
    for _ in range(4):
        response_code, body, current, latency = fetch(current)
        total_latency += latency
        if response_code not in (200, 206):
            raise RuntimeError(f"HTTP_{response_code}")
        text = body.decode("utf-8-sig", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise RuntimeError("HLS_MANIFEST_INVALID")
        variant_pending = False
        media_uri = None
        playlist_uri = None
        for line in lines[1:]:
            if line.startswith("#EXT-X-STREAM-INF:"):
                variant_pending = True
            elif line.startswith("#"):
                continue
            elif variant_pending:
                playlist_uri = urllib.parse.urljoin(current, line)
                break
            else:
                media_uri = urllib.parse.urljoin(current, line)
                break
        if playlist_uri:
            current = playlist_uri
            continue
        if not media_uri:
            raise RuntimeError("HLS_HAS_NO_MEDIA_SEGMENT")
        segment_code, segment, _, segment_latency = fetch(media_uri, 4096)
        total_latency += segment_latency
        if segment_code not in (200, 206) or not segment:
            raise RuntimeError(f"FIRST_SEGMENT_HTTP_{segment_code}")
        return {"httpCode": segment_code, "latencyMs": total_latency, "detail": "manifest_and_first_segment_ok"}
    raise RuntimeError("HLS_VARIANT_DEPTH_EXCEEDED")


def probe_dash(url: str) -> dict:
    status, body, final_url, latency = fetch(url)
    if status not in (200, 206):
        raise RuntimeError(f"HTTP_{status}")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise RuntimeError("DASH_MANIFEST_INVALID") from error
    if not root.tag.lower().endswith("mpd"):
        raise RuntimeError("DASH_MANIFEST_INVALID")
    base_url = next((element.text.strip() for element in root.iter() if element.tag.lower().endswith("baseurl") and element.text and element.text.strip()), None)
    if base_url:
        media_url = urllib.parse.urljoin(final_url, base_url)
        code, payload, _, media_latency = fetch(media_url, 4096)
        if code not in (200, 206) or not payload:
            raise RuntimeError(f"FIRST_SEGMENT_HTTP_{code}")
        return {"httpCode": code, "latencyMs": latency + media_latency, "detail": "manifest_and_first_segment_ok"}
    segment = next((element.attrib.get("media") for element in root.iter() if element.tag.lower().endswith("segmenturl") and element.attrib.get("media")), None)
    if not segment:
        raise RuntimeError("DASH_FIRST_SEGMENT_NOT_RESOLVABLE")
    code, payload, _, media_latency = fetch(urllib.parse.urljoin(final_url, segment), 4096)
    if code not in (200, 206) or not payload:
        raise RuntimeError(f"FIRST_SEGMENT_HTTP_{code}")
    return {"httpCode": code, "latencyMs": latency + media_latency, "detail": "manifest_and_first_segment_ok"}


def probe(candidate: dict) -> dict:
    if str(candidate.get("type", "STATIC")).upper() != "STATIC":
        raise RuntimeError("DYNAMIC_SOURCE_NEEDS_RESOLVER")
    protocol = str(candidate.get("protocol", "")).upper()
    if protocol == "HLS":
        return probe_hls(candidate["url"])
    if protocol == "DASH":
        return probe_dash(candidate["url"])
    raise RuntimeError(f"UNSUPPORTED_PROTOCOL_{protocol}")


def update_health(candidate: dict, previous: dict | None, result: dict | None, checked_at: str) -> dict:
    prev = previous or {}
    ok = result is not None
    failures = 0 if ok else int(prev.get("failCount", 0)) + 1
    successes = int(prev.get("successCount", 0)) + 1 if ok else int(prev.get("successCount", 0))
    if ok:
        status = "HEALTHY"
    elif failures == 1 and prev.get("status") in {"HEALTHY", "DEGRADED"}:
        status = prev["status"]
    elif failures < 3:
        status = "DEGRADED"
    else:
        status = "OFFLINE"
    row = {
        "sourceId": candidate["id"],
        "channelId": candidate["channelId"],
        "status": status,
        "lastCheckSucceeded": ok,
        "latencyMs": result.get("latencyMs") if ok else None,
        "httpCode": result.get("httpCode") if ok else previous.get("httpCode") if previous else None,
        "detail": result.get("detail") if ok else (result or {}).get("error", "probe_failed"),
        "lastCheckTime": checked_at,
        "failCount": failures,
        "successCount": successes,
    }
    return row


def check_all(candidate_groups: dict[str, list[dict]], previous: dict[str, dict], checker=probe, checked_at: str | None = None):
    checked_at = checked_at or utc_now()
    rows = []
    for channel_id, sources in candidate_groups.items():
        for source in sources:
            try:
                result = checker(source)
            except Exception as error:  # Individual source failures are health data, not job failures.
                result = {"error": f"{type(error).__name__}:{error}"}
            rows.append(update_health(source, previous.get(source["id"]), result if "error" not in result else None, checked_at))
            if "error" in result:
                rows[-1]["detail"] = result["error"][:300]
    total = len(rows)
    failed = sum(not row["lastCheckSucceeded"] for row in rows)
    return {"schemaVersion": 1, "checkedAt": checked_at, "checkedCount": total, "failedCount": failed,
            "sources": sorted(rows, key=lambda row: (row["channelId"], row["sourceId"]))}


def select_groups(catalog: dict, groups: dict[str, list[dict]], scope: str,
                  channel_id: str | None = None, category_id: str | None = None) -> dict[str, list[dict]]:
    normalized = scope.upper()
    channels = {row["id"]: row for row in catalog["channels"]}
    if normalized == "ALL":
        return groups
    if normalized == "CHANNEL":
        if channel_id not in channels:
            raise ValueError("CHANNEL refresh requires a known channelId")
        return {channel_id: groups[channel_id]}
    if normalized == "CATEGORY":
        category_ids = {row["id"] for row in catalog.get("categories", [])}
        if category_id not in category_ids:
            raise ValueError("CATEGORY refresh requires a known categoryId")
        return {key: value for key, value in groups.items() if category_id in channels[key].get("categoryIds", [])}
    raise ValueError(f"Unsupported refresh scope: {scope}")


def merge_health_snapshot(current: dict, previous: dict, checked_ids: set[str]) -> dict:
    """Keep older health rows for sources outside a scoped refresh."""
    untouched = [row for row in previous.get("sources", []) if row.get("sourceId") not in checked_ids]
    rows = untouched + current["sources"]
    current["sources"] = sorted(rows, key=lambda row: (row["channelId"], row["sourceId"]))
    current["checkedCount"] = len(checked_ids)
    current["failedCount"] = sum(not row["lastCheckSucceeded"] for row in current["sources"] if row.get("lastCheckTime") == current["checkedAt"])
    return current


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".new")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe configured, authorized source candidates")
    parser.add_argument("--scope", choices=("ALL", "CHANNEL", "CATEGORY"), default="ALL")
    parser.add_argument("--channel-id")
    parser.add_argument("--category-id")
    args = parser.parse_args()
    catalog, groups = load_candidates()
    selected = select_groups(catalog, groups, args.scope, args.channel_id, args.category_id)
    previous_path = ROOT / "health" / "latest.json"
    previous_payload = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    previous = {row["sourceId"]: row for row in previous_payload.get("sources", [])}
    payload = check_all(selected, previous)
    checked_ids = {source["id"] for sources in selected.values() for source in sources}
    if args.scope != "ALL":
        payload = merge_health_snapshot(payload, previous_payload, checked_ids)
    payload["scope"] = args.scope
    payload["scopeTarget"] = args.channel_id if args.scope == "CHANNEL" else args.category_id if args.scope == "CATEGORY" else "ALL"
    atomic_json(previous_path, payload)
    print(f"checked={payload['checkedCount']} failed={payload['failedCount']}")
    for row in payload["sources"]:
        print(f"{row['channelId']} {row['sourceId']} {row['status']} http={row['httpCode']} latencyMs={row['latencyMs']} failCount={row['failCount']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
