#!/usr/bin/env python3
"""
Refresh marketplace plugins by fetching plugin.json from each source repo.

Reads .claude-plugin/marketplace.json, fetches the latest plugin config from
each plugin's source repository via the GitHub API (unauthenticated — all
source repos are public), merges updated fields, and bumps the marketplace
version if anything changed.

Supports a ``validate`` subcommand that checks the marketplace file for
structural correctness and undocumented fields.

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
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10

# ── Documented field sets (from Claude Code plugin marketplace spec) ─────

DOCUMENTED_TOP_LEVEL_FIELDS = frozenset(
    {"$schema", "name", "owner", "plugins", "metadata"}
)

DOCUMENTED_OWNER_FIELDS = frozenset({"name", "email"})

DOCUMENTED_METADATA_FIELDS = frozenset({"description", "version", "pluginRoot"})

DOCUMENTED_PLUGIN_FIELDS = frozenset(
    {
        "name",
        "source",
        "description",
        "version",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "category",
        "tags",
        "strict",
        "commands",
        "agents",
        "skills",
        "hooks",
        "mcpServers",
        "lspServers",
        "outputStyles",
    }
)

DOCUMENTED_AUTHOR_FIELDS = frozenset({"name", "email"})

# Fields that the marketplace controls — never overwritten from source.
PROTECTED_FIELDS = frozenset({"name", "source"})


# ── Validation ───────────────────────────────────────────────────────────


def validate_marketplace(marketplace: dict) -> list[str]:
    """Return a list of validation errors (empty means valid)."""
    errors: list[str] = []

    # Top-level required fields
    for field in ("name", "owner", "plugins"):
        if field not in marketplace:
            errors.append(f"Missing required top-level field: {field}")

    # Top-level unknown fields
    for key in marketplace:
        if key not in DOCUMENTED_TOP_LEVEL_FIELDS:
            errors.append(f"Undocumented top-level field: {key}")

    # Owner
    owner = marketplace.get("owner", {})
    if isinstance(owner, dict):
        if "name" not in owner:
            errors.append("Missing required field: owner.name")
        for key in owner:
            if key not in DOCUMENTED_OWNER_FIELDS:
                errors.append(f"Undocumented owner field: {key}")

    # Metadata
    metadata = marketplace.get("metadata", {})
    if isinstance(metadata, dict):
        for key in metadata:
            if key not in DOCUMENTED_METADATA_FIELDS:
                errors.append(f"Undocumented metadata field: {key}")

    # Plugins
    plugins = marketplace.get("plugins", [])
    if not isinstance(plugins, list):
        errors.append("plugins must be an array")
        return errors

    seen_names: set[str] = set()
    for i, plugin in enumerate(plugins):
        label = plugin.get("name", f"plugins[{i}]")
        if "name" not in plugin:
            errors.append(f"{label}: Missing required field: name")
        else:
            if plugin["name"] in seen_names:
                errors.append(f'{label}: Duplicate plugin name "{plugin["name"]}"')
            seen_names.add(plugin["name"])
        if "source" not in plugin:
            errors.append(f"{label}: Missing required field: source")

        for key in plugin:
            if key not in DOCUMENTED_PLUGIN_FIELDS:
                errors.append(f"{label}: Undocumented plugin field: {key}")

        author = plugin.get("author")
        if isinstance(author, dict):
            for key in author:
                if key not in DOCUMENTED_AUTHOR_FIELDS:
                    errors.append(f"{label}: Undocumented author field: {key}")

    return errors


# ── Helpers ──────────────────────────────────────────────────────────────


def fetch_plugin_config(repo: str, session: requests.Session) -> dict:
    """Fetch and decode .claude-plugin/plugin.json from a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{PLUGIN_CONFIG_PATH}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    content_b64 = resp.json()["content"]
    raw = base64.b64decode(content_b64)
    return json.loads(raw)


def _filter_author(author: object) -> dict | None:
    """Keep only documented author fields."""
    if not isinstance(author, dict):
        return None
    filtered = {k: v for k, v in author.items() if k in DOCUMENTED_AUTHOR_FIELDS}
    return filtered if filtered else None


def merge_plugin(entry: dict, source: dict) -> dict:
    """Merge source fields into a marketplace plugin entry.

    * Protected fields (``name``, ``source``) are kept from the marketplace
      entry.
    * All other documented fields are taken from *source* if present.
    * Fields present in the entry but absent from source (except protected)
      are dropped.
    """
    merged: dict = {}

    # Keep protected fields from marketplace entry
    for field in PROTECTED_FIELDS:
        if field in entry:
            merged[field] = copy.deepcopy(entry[field])

    # Copy documented fields from source
    for field in DOCUMENTED_PLUGIN_FIELDS - PROTECTED_FIELDS:
        if field in source:
            merged[field] = copy.deepcopy(source[field])

    # Sanitise author sub-object
    if "author" in merged:
        author = _filter_author(merged["author"])
        if author:
            merged["author"] = author
        else:
            del merged["author"]

    return merged


def bump_version(version: str) -> str:
    """Increment the last segment of a dotted version string."""
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _serialize(marketplace: dict) -> str:
    return json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n"


# ── Commands ─────────────────────────────────────────────────────────────


def cmd_validate() -> None:
    """Validate the marketplace file and exit."""
    if not MARKETPLACE_PATH.exists():
        sys.exit(f"Error: {MARKETPLACE_PATH} not found")

    marketplace = json.loads(MARKETPLACE_PATH.read_text())
    errors = validate_marketplace(marketplace)
    if errors:
        print("Marketplace validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print(f"Marketplace {MARKETPLACE_PATH} is valid")


def cmd_refresh() -> None:
    """Refresh plugins from their source repos."""
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

    # Validate before writing
    errors = validate_marketplace(marketplace)
    if errors:
        print("Marketplace validation failed after refresh:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    MARKETPLACE_PATH.write_text(_serialize(marketplace))


# ── Entry point ──────────────────────────────────────────────────────────


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        cmd_validate()
    else:
        cmd_refresh()


if __name__ == "__main__":
    main()
