from __future__ import annotations

import json
from pathlib import Path

from validate_config import ROOT, read_json, validate_repository


def main() -> None:
    validate_repository(include_published=False)
    catalog = read_json(ROOT / "catalog" / "channels.json")
    output = ROOT / "public" / "channels.json"
    temp = output.with_name("channels.new.json")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(catalog, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    validated = read_json(temp)
    if validated.get("version") != catalog.get("version") or validated.get("channels") != catalog.get("channels"):
        raise ValueError("temporary published catalog does not match catalog input")
    temp.replace(output)
    print(f"published catalog version={catalog['version']} channels={len(catalog['channels'])}")


if __name__ == "__main__":
    main()
