#!/usr/bin/env python3
"""
Refresh marketplace plugins by fetching plugin.json from each source repo.

Reads .claude-plugin/marketplace.json, fetches the latest plugin config from
each plugin's source repository via the GitHub API, merges updated fields,
and bumps the marketplace version if anything changed.

Requires:
  - GITHUB_TOKEN environment variable (or GH_TOKEN)
  - requests library (`pip install requests`)
"""

import base64
import copy
import json
import os
import sys
from pathlib import Path

import requests

MARKETPLACE_PATH = Path(".claude-plugin/marketplace.json")
PLUGIN_CONFIG_PATH = ".claude-plugin/plugin.json"
MERGE_FIELDS = ("version", "description", "author", "category", "tags")
GITHUB_API = "https://api.github.com"


def get_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Error: set GH_TOKEN or GITHUB_TOKEN environment variable")
    return token


def fetch_plugin_config(repo: str, session: requests.Session) -> dict | None:
    """Fetch and decode .claude-plugin/plugin.json from a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{PLUGIN_CONFIG_PATH}"
    resp = session.get(url)
    if resp.status_code != 200:
        print(f"  ::warning::Could not fetch config from {repo} — HTTP {resp.status_code}")
        return None

    content_b64 = resp.json().get("content", "")
    try:
        raw = base64.b64decode(content_b64)
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"  ::warning::Could not decode config from {repo} — {exc}")
        return None


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

    token = get_token()
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })

    plugins = marketplace.get("plugins", [])
    print(f"Found {len(plugins)} plugin(s) in marketplace")

    for i, plugin in enumerate(plugins):
        name = plugin["name"]
        repo = plugin["source"]["repo"]
        print(f"::group::{name} ({repo})")

        config = fetch_plugin_config(repo, session)
        if config is None:
            print("::endgroup::")
            continue

        print(f"  Fetched config: {json.dumps(config, indent=2)}")
        plugins[i] = merge_plugin(plugin, config)
        print("::endgroup::")

    # Bump marketplace version only if plugin data actually changed
    updated = json.dumps(marketplace, indent=2, ensure_ascii=False)
    if updated != original:
        current = marketplace.get("metadata", {}).get("version", "0.0.0")
        next_ver = bump_version(current)
        marketplace["metadata"]["version"] = next_ver
        print(f"Marketplace version: {current} -> {next_ver}")
    else:
        print("No plugin changes detected")

    MARKETPLACE_PATH.write_text(
        json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    main()
