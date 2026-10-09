from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_sources import check_all, probe_hls, update_health
from promote_sources import promote
from validate_config import validate_sources_payload


class HealthTests(unittest.TestCase):
    def source(self, source_id="a", channel_id="c1"):
        return {"id": source_id, "channelId": channel_id, "protocol": "HLS", "type": "STATIC",
                "url": "https://media.example/live.m3u8", "priority": 1, "authorization": "license:open"}

    def test_hls_probe_requires_first_segment(self):
        responses = [
            (200, b"#EXTM3U\n#EXTINF:4,\nseg-1.ts\n", "https://media.example/live.m3u8", 10),
            (206, b"segment-bytes", "https://media.example/seg-1.ts", 20),
        ]
        with patch("check_sources.fetch", side_effect=responses) as fetch:
            result = probe_hls("https://media.example/live.m3u8")
        self.assertEqual(result["httpCode"], 206)
        self.assertEqual(result["latencyMs"], 30)
        self.assertEqual(fetch.call_count, 2)

    def test_empty_or_invalid_playlist_is_not_healthy(self):
        with patch("check_sources.fetch", return_value=(200, b"<html>not hls</html>", "https://media.example/live.m3u8", 5)):
            with self.assertRaisesRegex(RuntimeError, "HLS_MANIFEST_INVALID"):
                probe_hls("https://media.example/live.m3u8")

    def test_consecutive_failures_demote_then_offline_and_recovery_resets(self):
        candidate = self.source()
        previous = None
        states = []
        for index in range(3):
            previous = update_health(candidate, previous, None, f"2026-10-09T0{index}:00:00Z")
            states.append(previous["status"])
        self.assertEqual(states, ["DEGRADED", "DEGRADED", "OFFLINE"])
        recovered = update_health(candidate, previous, {"httpCode": 206, "latencyMs": 30}, "2026-10-09T03:00:00Z")
        self.assertEqual(recovered["status"], "HEALTHY")
        self.assertEqual(recovered["failCount"], 0)
        self.assertEqual(recovered["successCount"], 1)

    def test_first_failure_does_not_forget_previous_health(self):
        previous = {"status": "HEALTHY", "failCount": 0, "successCount": 3, "httpCode": 206}
        row = update_health(self.source(), previous, None, "2026-10-09T03:00:00Z")
        self.assertEqual(row["status"], "HEALTHY")
        self.assertEqual(row["failCount"], 1)
        self.assertFalse(row["lastCheckSucceeded"])

    def test_check_all_records_source_failure_without_aborting_other_channels(self):
        groups = {"c1": [self.source()], "c2": [self.source("b", "c2")]}
        results = iter([RuntimeError("timeout"), {"httpCode": 206, "latencyMs": 10, "detail": "ok"}])

        def checker(_source):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        payload = check_all(groups, {}, checker, "2026-10-09T03:00:00Z")
        self.assertEqual(payload["checkedCount"], 2)
        self.assertEqual(payload["failedCount"], 1)
        self.assertEqual(payload["sources"][1]["status"], "HEALTHY")


class PromotionTests(unittest.TestCase):
    def candidate(self, source_id, priority):
        return {"id": source_id, "channelId": "c1", "protocol": "HLS", "type": "STATIC",
                "url": f"https://media.example/{source_id}.m3u8", "priority": priority,
                "quality": "720P", "enabled": True, "authorization": "license:open"}

    def test_promotes_passing_source_and_keeps_transient_failed_source_as_backup(self):
        source_a, source_b = self.candidate("a", 1), self.candidate("b", 2)
        catalog = {"channels": [{"id": "c1"}]}
        health = {"checkedAt": "now", "sources": [
            {"sourceId": "a", "channelId": "c1", "status": "HEALTHY", "lastCheckSucceeded": False, "lastCheckTime": "now", "failCount": 1},
            {"sourceId": "b", "channelId": "c1", "status": "HEALTHY", "lastCheckSucceeded": True, "lastCheckTime": "now", "latencyMs": 80},
        ]}
        previous = {"version": 4, "channels": {"c1": {"status": "AVAILABLE", "sources": [{"id": "a"}]}}}
        output = promote(catalog, {"c1": [source_a, source_b]}, health, previous)
        channel = output["channels"]["c1"]
        self.assertEqual(channel["status"], "AVAILABLE")
        self.assertEqual([source["id"] for source in channel["sources"]], ["b", "a"])
        self.assertEqual(channel["sources"][1]["health"], "DEGRADED")
        validate_sources_payload(output, {"c1"})

    def test_systemic_80_percent_failure_refuses_generation(self):
        candidates = [self.candidate(f"s{i}", i + 1) for i in range(5)]
        catalog = {"channels": [{"id": "c1"}]}
        health = {"checkedAt": "now", "sources": [
            {"sourceId": source["id"], "channelId": "c1", "status": "OFFLINE",
             "lastCheckSucceeded": index == 0, "lastCheckTime": "now"}
            for index, source in enumerate(candidates)
        ]}
        with self.assertRaisesRegex(RuntimeError, "80%"):
            promote(catalog, {"c1": candidates}, health, {"version": 7, "channels": {"c1": {"status": "AVAILABLE", "sources": []}}})


if __name__ == "__main__":
    unittest.main()
