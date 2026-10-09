from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_sources import check_all, merge_health_snapshot, mpeg_ts_has_video, probe_hls, select_groups, update_health
from promote_sources import promote
from validate_config import validate_sources_payload


class HealthTests(unittest.TestCase):
    @staticmethod
    def ts_segment(stream_type: int) -> bytes:
        def packet(pid: int, section: bytes) -> bytes:
            header = bytes((0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10, 0))
            return (header + section).ljust(188, b"\xff")
        pat = bytes.fromhex("00b00d0001c100000001e10000000000")
        pmt = bytes.fromhex("02b0120001c10000e101f000") + bytes((stream_type,)) + bytes.fromhex("e101f00000000000")
        return packet(0, pat) + packet(0x100, pmt)

    def source(self, source_id="a", channel_id="c1"):
        return {"id": source_id, "channelId": channel_id, "protocol": "HLS", "type": "STATIC",
                "url": "https://media.example/live.m3u8", "priority": 1, "authorization": "license:open"}

    def test_hls_probe_requires_first_segment(self):
        responses = [
            (200, b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg-1.ts\n", "https://media.example/live.m3u8", 10),
            (206, self.ts_segment(0x1B), "https://media.example/seg-1.ts", 20),
        ]
        with patch("check_sources.fetch", side_effect=responses) as fetch:
            result = probe_hls("https://media.example/live.m3u8")
        self.assertEqual(result["httpCode"], 206)
        self.assertEqual(result["latencyMs"], 30)
        self.assertEqual(fetch.call_count, 2)

    def test_audio_only_hls_segment_is_not_healthy(self):
        self.assertTrue(mpeg_ts_has_video(self.ts_segment(0x1B)))
        self.assertFalse(mpeg_ts_has_video(self.ts_segment(0x0F)))
        responses = [(200, b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg-1.ts\n", "https://media.example/live.m3u8", 10),
                     (206, self.ts_segment(0x0F), "https://media.example/seg-1.ts", 20)]
        with patch("check_sources.fetch", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "HLS_VIDEO_TRACK_MISSING"):
                probe_hls("https://media.example/live.m3u8")

    def test_an_m3u_channel_list_is_not_an_hls_media_playlist(self):
        body = b"#EXTM3U\n#EXTINF:-1,Channel\nhttps://media.example/live.m3u8\n"
        with patch("check_sources.fetch", return_value=(200, body, "https://media.example/list.m3u8", 5)):
            with self.assertRaisesRegex(RuntimeError, "HLS_MANIFEST_INVALID"):
                probe_hls("https://media.example/list.m3u8")

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

    def test_scoped_selection_targets_channel_or_category(self):
        catalog = {"channels": [
            {"id": "c1", "categoryIds": ["news"]},
            {"id": "c2", "categoryIds": ["sports"]},
        ], "categories": [{"id": "news"}, {"id": "sports"}]}
        groups = {"c1": [self.source("a", "c1")], "c2": [self.source("b", "c2")]}
        self.assertEqual(set(select_groups(catalog, groups, "CHANNEL", channel_id="c1")), {"c1"})
        self.assertEqual(set(select_groups(catalog, groups, "CATEGORY", category_id="sports")), {"c2"})
        with self.assertRaisesRegex(ValueError, "known channelId"):
            select_groups(catalog, groups, "CHANNEL", channel_id="missing")

    def test_scoped_refresh_preserves_unchecked_health_rows(self):
        current = {"checkedAt": "now", "checkedCount": 1, "failedCount": 0,
                   "sources": [{"sourceId": "a", "channelId": "c1", "lastCheckTime": "now", "lastCheckSucceeded": True}]}
        previous = {"sources": [
            {"sourceId": "a", "channelId": "c1", "lastCheckTime": "old", "lastCheckSucceeded": False},
            {"sourceId": "b", "channelId": "c2", "lastCheckTime": "yesterday", "lastCheckSucceeded": True},
        ]}
        merged = merge_health_snapshot(current, previous, {"a"})
        self.assertEqual([row["sourceId"] for row in merged["sources"]], ["a", "b"])
        self.assertEqual(merged["sources"][1]["lastCheckTime"], "yesterday")
        self.assertEqual(merged["checkedCount"], 1)


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

    def test_community_source_remains_labelled_after_promotion(self):
        candidate = self.candidate("community-a", 1)
        candidate.pop("authorization")
        candidate.update({"providerId": "chinaiptv", "sourceClass": "COMMUNITY_SOURCE"})
        health = {"checkedAt": "now", "sources": [{"sourceId": candidate["id"], "channelId": "c1",
                  "status": "HEALTHY", "lastCheckSucceeded": True, "lastCheckTime": "now", "latencyMs": 50}]}
        output = promote({"channels": [{"id": "c1"}]}, {"c1": [candidate]}, health, None)
        published = output["channels"]["c1"]["sources"][0]
        self.assertEqual(published["sourceClass"], "COMMUNITY_SOURCE")
        self.assertEqual(published["providerId"], "chinaiptv")
        self.assertNotIn("authorization", published)
        validate_sources_payload(output, {"c1"})


if __name__ == "__main__":
    unittest.main()
