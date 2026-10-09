from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from validate_config import ROOT, read_json, validate_repository


def build_manifest() -> dict:
    catalog = read_json(ROOT / "catalog" / "channels.json")
    sources = read_json(ROOT / "public" / "sources.json")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    if repository and server == "https://github.com":
        raw_base = f"https://raw.githubusercontent.com/{repository}/{branch}/public/"
        channel_url = raw_base + "channels.json"
        source_url = raw_base + "sources.json"
    else:
        # Relative paths are resolved against tvGithubManifestUrl in the app build.
        channel_url = "channels.json"
        source_url = "sources.json"
    return {
        "schemaVersion": 1,
        "catalogVersion": int(catalog["version"]),
        "sourceVersion": int(sources["version"]),
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "channelUrl": channel_url,
        "sourceUrl": source_url,
    }


def main() -> int:
    validate_repository()
    output = ROOT / "public" / "manifest.json"
    temp = output.with_name("manifest.new.json")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(build_manifest(), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    manifest = json.loads(temp.read_text(encoding="utf-8"))
    if not manifest["channelUrl"] or not manifest["sourceUrl"]:
        raise ValueError("manifest URLs cannot be empty")
    temp.replace(output)
    print(f"manifest catalog={manifest['catalogVersion']} sources={manifest['sourceVersion']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"manifest generation failed; previous manifest preserved: {error}", file=sys.stderr)
        sys.exit(2)
