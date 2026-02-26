#!/usr/bin/env python3
"""
Refresh marketplace plugins by fetching plugin.json from each source repo.

Reads .claude-plugin/marketplace.json, fetches the latest plugin config from
each plugin's source repository via the GitHub API (unauthenticated — all
source repos are public), validates each fetched config against the plugin
manifest schema, merges updated fields, validates the final marketplace
file, and bumps the marketplace version if anything changed.

Supports a ``validate`` subcommand that checks the marketplace file against
the JSON Schema.

Requires:
  - requests   (`pip install requests`)
  - jsonschema (`pip install jsonschema`)
"""

import base64
import copy
import json
import re
import sys
from pathlib import Path

import jsonschema
import requests

MARKETPLACE_PATH = Path(".claude-plugin/marketplace.json")
PLUGIN_CONFIG_PATH = ".claude-plugin/plugin.json"
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
MARKETPLACE_SCHEMA_PATH = SCHEMA_DIR / "marketplace.schema.json"
PLUGIN_SCHEMA_PATH = SCHEMA_DIR / "plugin.schema.json"

# Fields that the marketplace controls — never overwritten from source.
PROTECTED_FIELDS = frozenset({"name", "source"})

# Marketplace-specific fields: exist only in marketplace entries, never in
# plugin.json manifests (confirmed via Zod: marketplace entry is
# pluginManifest.partial().extend({source, category, tags, strict})).
# Preserved from the entry when source doesn't provide them.
MARKETPLACE_ONLY_FIELDS = frozenset({"category", "tags", "strict"})


# ── Schema loading ───────────────────────────────────────────────────────


def _load_schema(path: Path) -> dict:
    """Load a JSON Schema from a local file."""
    return json.loads(path.read_text())


# ── Validation ───────────────────────────────────────────────────────────


def validate_marketplace(marketplace: dict) -> list[str]:
    """Validate *marketplace* against the marketplace JSON Schema.

    Returns a list of human-readable error messages (empty == valid).
    Includes checks that JSON Schema cannot express (duplicate names).
    """
    schema = _load_schema(MARKETPLACE_SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)
    errors = [_format_error(e) for e in validator.iter_errors(marketplace)]

    # Duplicate plugin names (not expressible in JSON Schema)
    seen: set[str] = set()
    for i, plugin in enumerate(marketplace.get("plugins", [])):
        name = plugin.get("name")
        if name is None:
            continue
        if name in seen:
            errors.append(f'plugins.{i}.name: Duplicate plugin name "{name}"')
        seen.add(name)

    return errors


def validate_plugin_config(config: dict) -> list[str]:
    """Validate a plugin.json manifest against the plugin schema.

    Returns a list of human-readable error messages (empty == valid).
    """
    schema = _load_schema(PLUGIN_SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)
    return [_format_error(e) for e in validator.iter_errors(config)]


def _format_error(error: jsonschema.ValidationError) -> str:
    path = ".".join(str(p) for p in error.absolute_path) if error.absolute_path else "(root)"
    return f"{path}: {error.message}"


# ── Helpers ──────────────────────────────────────────────────────────────


def fetch_plugin_config(repo: str, session: requests.Session) -> dict:
    """Fetch and decode .claude-plugin/plugin.json from a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{PLUGIN_CONFIG_PATH}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    content_b64 = resp.json()["content"]
    raw = base64.b64decode(content_b64)
    return json.loads(raw)


def _documented_plugin_fields() -> frozenset[str]:
    """Return the set of allowed plugin-entry property names from the marketplace schema."""
    schema = _load_schema(MARKETPLACE_SCHEMA_PATH)
    props = schema.get("$defs", {}).get("pluginEntry", {}).get("properties", {})
    return frozenset(props.keys())


def _documented_author_fields() -> frozenset[str]:
    """Return the set of allowed author property names from the marketplace schema."""
    schema = _load_schema(MARKETPLACE_SCHEMA_PATH)
    author = schema.get("$defs", {}).get("author", {}).get("properties", {})
    return frozenset(author.keys())


def _filter_author(author: object) -> dict | None:
    """Keep only schema-documented author fields."""
    if not isinstance(author, dict):
        return None
    allowed = _documented_author_fields()
    filtered = {k: v for k, v in author.items() if k in allowed}
    return filtered if filtered else None


def merge_plugin(entry: dict, source: dict) -> dict:
    """Merge source fields into a marketplace plugin entry.

    * Protected fields (``name``, ``source``) are always kept from the
      marketplace entry.
    * Marketplace-only fields (``category``, ``tags``, ``strict``)
      are preserved from the entry when absent from source.
    * All other documented fields are taken from *source* if present;
      fields absent from source are dropped.
    """
    documented = _documented_plugin_fields()
    keep_from_entry = PROTECTED_FIELDS | MARKETPLACE_ONLY_FIELDS
    merged: dict = {}

    # Keep protected + marketplace-only fields from entry
    for field in keep_from_entry:
        if field in entry:
            merged[field] = copy.deepcopy(entry[field])

    # Copy documented fields from source (overwrites marketplace-only if
    # source happens to provide them)
    for field in documented - PROTECTED_FIELDS:
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


# Semver: digits-only core, optional pre-release/build suffix.
_SEMVER_RE = re.compile(
    r"^(?P<core>(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*)(?P<extra>[+-].+)?$"
)


def bump_version(version: str) -> str:
    """Increment the patch (last) segment of a version string.

    Pre-release and build metadata are stripped — bumping ``0.1.0-beta``
    produces ``0.1.0`` (the release that follows the pre-release), and
    bumping ``0.1.0`` produces ``0.1.1``.

    Raises ``ValueError`` with a descriptive message if *version* is not
    a valid dotted-numeric version (with optional semver suffixes).
    """
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(
            f"Cannot bump version: {version!r} is not a valid "
            f"dotted-numeric version (expected e.g. 0.1.0, 1.2.3-beta)"
        )

    core = m.group("core")
    had_prerelease = m.group("extra") is not None

    parts = core.split(".")
    if had_prerelease:
        # 0.1.0-beta → 0.1.0  (the release that follows the pre-release)
        return core
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
        source = plugin.get("source", {})

        # Only GitHub sources can be refreshed; skip all others.
        if isinstance(source, str):
            print(f"  Skipping {name}: relative-path source ({source})")
            continue
        source_type = source.get("source")
        if source_type != "github":
            label = source_type or "unknown"
            print(f"  Skipping {name}: non-GitHub source ({label})")
            continue

        repo = source["repo"]
        print(f"::group::{name} ({repo})")
        try:
            config = fetch_plugin_config(repo, session)
            print(f"  Fetched config: {json.dumps(config, indent=2)}")

            # Validate the fetched plugin.json
            plugin_errors = validate_plugin_config(config)
            if plugin_errors:
                print(f"  Plugin config validation errors:", file=sys.stderr)
                for err in plugin_errors:
                    print(f"    - {err}", file=sys.stderr)
                raise ValueError(
                    f"plugin.json from {repo} failed schema validation"
                )

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
