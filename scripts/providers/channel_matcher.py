from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


DEFAULT_ALIASES = {
    "cctv1": ["CCTV1", "CCTV-1", "CCTV 1", "CCTV-1 综合", "中央一套", "央视一套"],
    "cctv2": ["CCTV2", "CCTV-2", "CCTV 2", "中央二套", "央视二套"],
    "cctv3": ["CCTV3", "CCTV-3", "CCTV 3", "中央三套", "央视三套"],
    "cctv4": ["CCTV4", "CCTV-4", "CCTV 4", "中文国际", "中央四套"],
    "cctv5": ["CCTV5", "CCTV-5", "CCTV 5", "中央五套"],
    "cctv6": ["CCTV6", "CCTV-6", "CCTV 6", "中央六套"],
    "cctv7": ["CCTV7", "CCTV-7", "CCTV 7", "中央七套"],
    "cctv8": ["CCTV8", "CCTV-8", "CCTV 8", "中央八套"],
    "cctv9": ["CCTV9", "CCTV-9", "CCTV 9", "中央九套"],
    "cctv10": ["CCTV10", "CCTV-10", "CCTV 10", "中央十套"],
    "cctv11": ["CCTV11", "CCTV-11", "CCTV 11", "中央十一套"],
    "cctv12": ["CCTV12", "CCTV-12", "CCTV 12", "中央十二套"],
    "cctv13": ["CCTV13", "CCTV-13", "CCTV 13", "中央十三套"],
    "cctv14": ["CCTV14", "CCTV-14", "CCTV 14", "中央十四套"],
    "cctv15": ["CCTV15", "CCTV-15", "CCTV 15", "中央十五套"],
    "cctv16": ["CCTV16", "CCTV-16", "CCTV 16", "中央十六套"],
    "cctv17": ["CCTV17", "CCTV-17", "CCTV 17", "中央十七套"],
    "cctv18": ["CCTV4K", "CCTV-4K", "CCTV 4K"],
    "cctv19": ["CCTV8K", "CCTV-8K", "CCTV 8K"],
    "sat118": ["湖南卫视", "湖南卫视高清", "Hunan TV", "Hunan Satellite TV", "Mango TV Hunan"],
    "sat109": ["东方卫视", "上海卫视", "Dragon TV", "Shanghai TV"],
    "sat110": ["江苏卫视", "Jiangsu TV", "Jiangsu Satellite TV"],
    "sat111": ["浙江卫视", "Zhejiang TV", "Zhejiang Satellite TV"],
    "sat119": ["广东卫视", "Guangdong TV", "Guangdong Satellite TV"],
    "sat101": ["北京卫视", "Beijing TV", "BTV"],
    "sat122": ["重庆卫视", "Chongqing TV"],
    "sat123": ["四川卫视", "Sichuan TV"],
    "sat115": ["山东卫视", "Shandong TV"],
    "sat116": ["河南卫视", "Henan TV"],
}

QUALITY_SUFFIXES = ("超高清", "高清晰", "高清", "超清", "标清", "流畅", "uhd", "fhd", "hd", "sd")


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    # Remove common feed quality suffixes while preserving channel identifiers such as CCTV-4K.
    changed = True
    while changed:
        changed = False
        for suffix in QUALITY_SUFFIXES:
            if text.endswith(suffix):
                text = text[:-len(suffix)].strip()
                changed = True
                break
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


@dataclass(frozen=True)
class ChannelMatch:
    channel_id: str
    method: str
    confidence: float


class ChannelMatcher:
    """Matches subscription entries by stable ids first, then names and curated aliases."""

    def __init__(self, catalog: dict, aliases: dict[str, list[str]] | None = None):
        self.channels = {str(row["id"]): row for row in catalog.get("channels", []) if row.get("id")}
        self.aliases = {key: list(values) for key, values in DEFAULT_ALIASES.items()}
        for key, values in (aliases or {}).items():
            self.aliases.setdefault(key, []).extend(values)
        self.aliases = {channel_id: values for channel_id, values in self.aliases.items()
                        if channel_id in self.channels}
        self._alias_ids: dict[str, str] = {}
        self._canonical_names: dict[str, str] = {}
        for channel_id, row in self.channels.items():
            for name in (row.get("name"), row.get("shortName"), row.get("epgId")):
                normalized = normalize_name(name)
                if normalized:
                    self._canonical_names.setdefault(normalized, channel_id)
            for name in self.aliases.get(channel_id, []):
                normalized = normalize_name(name)
                if normalized:
                    self._alias_ids.setdefault(normalized, channel_id)

    def match(self, entry: dict) -> ChannelMatch | None:
        tvg_ids = [str(entry.get(key) or "").strip() for key in ("tvg-id", "tvgId", "tvg_id", "tvgid")]
        for value in tvg_ids:
            direct = self._direct_id(value)
            if direct:
                return ChannelMatch(direct, "tvg-id", 1.0)

        for key in ("channelId", "channel_id", "standardChannelId", "standard_channel_id", "id"):
            value = str(entry.get(key) or "").strip()
            direct = self._direct_id(value)
            if direct:
                return ChannelMatch(direct, "standard-id", 1.0)

        names = [entry.get(key) for key in ("tvg-name", "tvgName", "tvg_name", "name", "title", "channelName", "channel_name")]
        normalized_names = [normalize_name(value) for value in names if value]
        for normalized in normalized_names:
            if normalized in self._canonical_names:
                return ChannelMatch(self._canonical_names[normalized], "normalized-name", 1.0)
        for value in tvg_ids:
            alias_id = self._alias_ids.get(normalize_name(value))
            if alias_id:
                return ChannelMatch(alias_id, "alias", 0.98)
        for normalized in normalized_names:
            if normalized in self._alias_ids:
                return ChannelMatch(self._alias_ids[normalized], "alias", 0.98)

        # Fuzzy matching is deliberately conservative: automatic matching must not cross channel numbers.
        scores_by_id: dict[str, float] = {}
        aliases_by_id: dict[str, set[str]] = {}
        for channel_id, row in self.channels.items():
            aliases_by_id[channel_id] = {normalize_name(row.get("name")), normalize_name(row.get("shortName"))}
            aliases_by_id[channel_id].update(normalize_name(value) for value in self.aliases.get(channel_id, []))
        for normalized in normalized_names:
            for channel_id, candidates in aliases_by_id.items():
                for candidate in candidates:
                    if not candidate:
                        continue
                    score = SequenceMatcher(None, normalized, candidate).ratio()
                    scores_by_id[channel_id] = max(scores_by_id.get(channel_id, 0.0), score)
        ranked = sorted(scores_by_id.items(), key=lambda row: row[1], reverse=True)
        if ranked and ranked[0][1] >= 0.94 and ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0) >= 0.06:
            return ChannelMatch(ranked[0][0], "fuzzy-name", ranked[0][1])
        return None

    def _direct_id(self, value: str) -> str | None:
        key = value.casefold()
        return key if key in self.channels else None
