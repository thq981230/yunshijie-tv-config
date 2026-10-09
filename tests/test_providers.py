from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from providers.channel_matcher import ChannelMatcher
from providers.remote_subscription import RemoteSubscriptionProvider, parse_m3u, parse_subscription
from providers.registry import OfficialProviderRegistry
from providers.base import DiscoveredSource
from discover_subscriptions import merge_sources


CATALOG = {"channels": [
    {"id": "cctv1", "name": "CCTV-1 综合", "shortName": "CCTV-1", "epgId": "cctv1"},
    {"id": "sat118", "name": "湖南卫视", "shortName": "湖南卫视", "epgId": "hunan_satellite"},
]}


class ChannelMatcherTests(unittest.TestCase):
    def setUp(self):
        self.matcher = ChannelMatcher(CATALOG)

    def test_tvg_id_has_highest_priority(self):
        result = self.matcher.match({"tvg-id": "cctv1", "channelId": "sat118", "name": "湖南卫视"})
        self.assertEqual((result.channel_id, result.method), ("cctv1", "tvg-id"))

    def test_standard_channel_id_precedes_name(self):
        result = self.matcher.match({"channelId": "cctv1", "name": "湖南卫视"})
        self.assertEqual((result.channel_id, result.method), ("cctv1", "standard-id"))

    def test_standard_id_is_not_overridden_by_a_noncanonical_tvg_alias(self):
        result = self.matcher.match({"tvg-id": "Hunan TV", "channelId": "cctv1"})
        self.assertEqual((result.channel_id, result.method), ("cctv1", "standard-id"))

    def test_normalized_cctv_names_and_hunan_alias(self):
        cases = [
            ({"name": "CCTV1"}, "cctv1"),
            ({"name": "CCTV 1"}, "cctv1"),
            ({"name": "中央一套"}, "cctv1"),
            ({"name": "CCTV1高清"}, "cctv1"),
            ({"name": "湖南卫视高清"}, "sat118"),
            ({"name": "Hunan TV"}, "sat118"),
        ]
        for row, channel_id in cases:
            with self.subTest(row=row):
                self.assertEqual(self.matcher.match(row).channel_id, channel_id)

    def test_unmatched_or_ambiguous_name_is_not_guessed(self):
        self.assertIsNone(self.matcher.match({"name": "CCTV 51"}))


class SubscriptionTests(unittest.TestCase):
    def test_source_merger_keeps_other_feeds_when_one_has_many_lines(self):
        rows = [DiscoveredSource("cctv1", {"providerId": "chinaiptv", "url": f"https://a.example/{index}.m3u8"},
                                 "remote-subscription", "feed:chinaiptv") for index in range(6)]
        rows.extend([
            DiscoveredSource("cctv1", {"providerId": "fanmingming", "url": "https://b.example/live.m3u8"},
                             "remote-subscription", "feed:fanmingming"),
            DiscoveredSource("cctv1", {"providerId": "iptv_org", "url": "https://c.example/live.m3u8"},
                             "remote-subscription", "feed:iptv_org"),
        ])
        merged = merge_sources(rows, ["cctv1"], limit=5)["cctv1"]
        self.assertEqual(len(merged), 5)
        self.assertEqual({row["providerId"] for row in merged}, {"chinaiptv", "fanmingming", "iptv_org"})

    def test_m3u_parser_retains_channel_metadata(self):
        rows = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="cctv1" tvg-name="CCTV-1 综合" group-title="央视",CCTV1高清\nhttps://cdn.example/cctv1.m3u8\n')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tvg-id"], "cctv1")
        self.assertEqual(rows[0]["name"], "CCTV1高清")
        self.assertEqual(rows[0]["url"], "https://cdn.example/cctv1.m3u8")

    def test_json_parser_supports_channels_streams_and_top_level_list(self):
        for body, expected in [
            ('{"channels":[{"name":"A","url":"https://a.example/a.m3u8"}]}', 1),
            ('{"streams":[{"name":"A","url":"https://a.example/a.m3u8"}]}', 1),
            ('[{"name":"A","url":"https://a.example/a.m3u8"}]', 1),
        ]:
            self.assertEqual(len(parse_subscription(body)), expected)

    def test_remote_provider_matches_and_returns_multiple_publishable_sources(self):
        feed = {
            "providerId": "authorized-feed",
            "url": "https://subscription.example/list.m3u",
            "redistributable": True,
            "authorization": "contract-ref-2026-01",
        }
        body = ('#EXTM3U\n'
                '#EXTINF:-1 tvg-id="cctv1",CCTV1\nhttps://cdn-a.example/cctv1.m3u8\n'
                '#EXTINF:-1 tvg-name="CCTV-1 综合",CCTV-1 综合高清\nhttps://cdn-b.example/cctv1.m3u8\n')
        provider = RemoteSubscriptionProvider([feed], fetcher=lambda _: body.encode())
        registry = OfficialProviderRegistry([provider])
        found = registry.discover(CATALOG)
        self.assertEqual([row.channel_id for row in found], ["cctv1", "cctv1"])
        self.assertEqual([row.source["priority"] for row in found], [100, 100])
        self.assertTrue(all(row.source["authorization"] == feed["authorization"] for row in found))
        self.assertEqual(provider.last_stats["published"], 2)

    def test_unapproved_feed_is_not_downloaded_or_published(self):
        fetch_calls = []
        provider = RemoteSubscriptionProvider(
            [{"providerId": "private", "url": "https://subscription.example/list.m3u"}],
            fetcher=lambda url: fetch_calls.append(url) or b"#EXTM3U",
        )
        self.assertEqual(provider.discover(CATALOG), [])
        self.assertEqual(fetch_calls, [])
        self.assertEqual(provider.last_stats["withoutPublicationPermission"], 1)

    def test_tokenized_stream_urls_are_never_published(self):
        feed = {"providerId": "authorized-feed", "url": "https://subscription.example/list.json",
                "redistributable": True, "authorization": "permission-ref-001"}
        body = json.dumps({"channels": [
            {"tvg-id": "cctv1", "url": "https://cdn.example/live.m3u8?token=private"},
            {"tvg-id": "cctv1", "url": "https://cdn.example/live-ok.m3u8"},
        ]}).encode()
        provider = RemoteSubscriptionProvider([feed], fetcher=lambda _: body)
        sources = provider.discover(CATALOG)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source["url"], "https://cdn.example/live-ok.m3u8")
        self.assertEqual(provider.last_stats["rejected"], 1)

    def test_community_test_feed_keeps_provenance_and_rejects_multicast(self):
        feeds = [
            {"providerId": "chinaiptv", "url": "https://feed.example/one.m3u", "communityTest": True},
            {"providerId": "fanmingming", "url": "https://feed.example/two.m3u", "communityTest": True},
            {"providerId": "iptv_org", "url": "https://feed.example/three.m3u", "communityTest": True},
        ]
        bodies = {
            "one.m3u": '#EXTM3U\n#EXTINF:-1 tvg-id="cctv1",CCTV-1\nhttps://a.example/live.m3u8\n',
            "two.m3u": '#EXTM3U\n#EXTINF:-1 tvg-id="cctv1",CCTV-1\nhttps://b.example/live.m3u8\n',
            "three.m3u": ('#EXTM3U\n#EXTINF:-1 tvg-id="cctv1",CCTV-1\nrtp://239.1.1.1:1234\n'
                          '#EXTINF:-1 tvg-id="cctv1",CCTV-1\nhttps://c.example/live.m3u8?auth_key=secret\n'),
        }
        provider = RemoteSubscriptionProvider(feeds, fetcher=lambda url: bodies[url.rsplit("/", 1)[-1]].encode())
        found = provider.discover(CATALOG)
        self.assertEqual([row.source["providerId"] for row in found], ["chinaiptv", "fanmingming"])
        self.assertTrue(all(row.source["sourceClass"] == "COMMUNITY_SOURCE" for row in found))
        self.assertTrue(all("authorization" not in row.source for row in found))
        self.assertEqual(provider.last_stats["feeds"], 3)
        self.assertEqual(provider.last_stats["rejected"], 2)

    def test_https_feed_and_stream_rules_reject_local_and_http_urls(self):
        provider = RemoteSubscriptionProvider(
            [{"providerId": "bad", "url": "http://subscription.example/list.m3u8",
              "redistributable": True, "authorization": "permission-ref-001"}],
            fetcher=lambda _: b"#EXTM3U",
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            provider.discover(CATALOG)


if __name__ == "__main__":
    unittest.main()
