#!/usr/bin/env python3
"""
Refresh marketplace plugins by fetching plugin.json from each source repo.

Reads .claude-plugin/marketplace.json, fetches the latest plugin config from
each plugin's source repository via the GitHub API (unauthenticated — all
source repos are public), merges updated fields, and bumps the marketplace
version if anything changed.

Requires:
  - requests library (`pip install requests`)
"""

import base64
import copy
import json
import sys
from pathlib import Path

import requests

MARKETPLACE_PATH = Path(".claude-plugin/marketplace.json")
PLUGIN_CONFIG_PATH = ".claude-plugin/plugin.json"
MERGE_FIELDS = ("version", "description", "author", "category", "tags")
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10


def fetch_plugin_config(repo: str, session: requests.Session) -> dict:
    """Fetch and decode .claude-plugin/plugin.json from a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{PLUGIN_CONFIG_PATH}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    content_b64 = resp.json()["content"]
    raw = base64.b64decode(content_b64)
    return json.loads(raw)


def merge_plugin(entry: dict, source: dict) -> dict:
    """Merge source fields into a marketplace plugin entry (source wins)."""
    merged = copy.deepcopy(entry)
    for field in MERGE_FIELDS:
        if field in source:
            merged[field] = source[field]
    return merged


def bump_version(version: str) -> str:
    """Increment the last segment of a dotted version string."""
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def main() -> None:
    if not MARKETPLACE_PATH.exists():
        sys.exit(f"Error: {MARKETPLACE_PATH} not found")

    marketplace = json.loads(MARKETPLACE_PATH.read_text())
    original = json.dumps(marketplace, indent=2, ensure_ascii=False)

    session = requests.Session()
    session.headers["Accept"] = "application/vnd.github+json"

    plugins = marketplace.get("plugins", [])
    print(f"Found {len(plugins)} plugin(s) in marketplace")

    for i, plugin in enumerate(plugins):
        name = plugin["name"]
        repo = plugin["source"]["repo"]
        print(f"::group::{name} ({repo})")
        try:
            config = fetch_plugin_config(repo, session)
            print(f"  Fetched config: {json.dumps(config, indent=2)}")
            plugins[i] = merge_plugin(plugin, config)
        except Exception as e:
            print(
                f"  Error refreshing plugin {name} from {repo}: {e}",
                file=sys.stderr,
            )
        finally:
            print("::endgroup::")
    # Bump marketplace version only if plugin data actually changed
    updated = json.dumps(marketplace, indent=2, ensure_ascii=False)
    if updated != original:
        current = marketplace.get("metadata", {}).get("version", "0.0.0")
        next_ver = bump_version(current)
        marketplace.setdefault("metadata", {})["version"] = next_ver
        print(f"Marketplace version: {current} -> {next_ver}")
    else:
        print("No plugin changes detected")

    MARKETPLACE_PATH.write_text(
        json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    main()
