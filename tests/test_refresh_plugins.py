"""Tests for scripts/refresh_plugins.py."""

import base64
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

# Ensure the module can be imported from the repo root.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import refresh_plugins as rp


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    """Clear the LRU cache between tests so schema mutations don't leak."""
    rp._load_schema.cache_clear()
    yield
    rp._load_schema.cache_clear()


def _make_marketplace(plugins=None, **overrides):
    """Build a minimal valid marketplace dict."""
    data = {
        "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
        "name": "test-marketplace",
        "owner": {"name": "tester"},
        "plugins": plugins or [],
    }
    data.update(overrides)
    return data


def _github_plugin(name="myplugin", repo="owner/repo", **extra):
    """Build a minimal valid GitHub-sourced plugin entry."""
    entry = {
        "name": name,
        "source": {"source": "github", "repo": repo},
    }
    entry.update(extra)
    return entry


def _make_github_api_response(plugin_json: dict) -> dict:
    """Simulate a GitHub Contents API response for a JSON file."""
    raw = json.dumps(plugin_json).encode()
    return {
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
    }


# ═════════════════════════════════════════════════════════════════════════
# bump_version
# ═════════════════════════════════════════════════════════════════════════


class TestBumpVersion:
    def test_patch_bump(self):
        assert rp.bump_version("0.0.0") == "0.0.1"
        assert rp.bump_version("1.2.3") == "1.2.4"
        assert rp.bump_version("0.1.9") == "0.1.10"

    def test_prerelease_finalized(self):
        assert rp.bump_version("0.1.0-beta") == "0.1.0"
        assert rp.bump_version("1.0.0-rc.1") == "1.0.0"

    def test_build_metadata_stripped(self):
        assert rp.bump_version("1.2.3+build.42") == "1.2.4"
        assert rp.bump_version("1.0.0-beta+sha.abc") == "1.0.0"

    @pytest.mark.parametrize("bad", ["", "1", "1.2", "01.2.3", "v1.2.3", "1.0.0-01"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            rp.bump_version(bad)


# ═════════════════════════════════════════════════════════════════════════
# validate_plugin_config
# ═════════════════════════════════════════════════════════════════════════


class TestValidatePluginConfig:
    def test_valid_minimal(self):
        assert rp.validate_plugin_config({"name": "x"}) == []

    def test_valid_full(self):
        config = {
            "name": "x",
            "version": "1.0.0",
            "description": "A plugin",
            "author": {"name": "dev"},
            "homepage": "https://example.com",
            "license": "MIT",
        }
        assert rp.validate_plugin_config(config) == []

    def test_missing_name(self):
        errs = rp.validate_plugin_config({"version": "1.0.0"})
        assert any("name" in e for e in errs)

    def test_extra_field_rejected(self):
        errs = rp.validate_plugin_config({"name": "x", "bogus": True})
        assert any("bogus" in e for e in errs)

    def test_bad_uri_format(self):
        errs = rp.validate_plugin_config({"name": "x", "homepage": "not a uri"})
        assert any("format" in e.lower() or "uri" in e.lower() for e in errs)

    def test_good_uri_format(self):
        errs = rp.validate_plugin_config({"name": "x", "homepage": "https://example.com"})
        assert not any("homepage" in e for e in errs)


# ═════════════════════════════════════════════════════════════════════════
# validate_marketplace
# ═════════════════════════════════════════════════════════════════════════


class TestValidateMarketplace:
    def test_valid_empty_plugins(self):
        assert rp.validate_marketplace(_make_marketplace()) == []

    def test_valid_with_plugin(self):
        mkt = _make_marketplace([_github_plugin()])
        assert rp.validate_marketplace(mkt) == []

    def test_live_marketplace_file(self):
        mkt = json.loads(rp.MARKETPLACE_PATH.read_text())
        assert rp.validate_marketplace(mkt) == []

    def test_duplicate_names(self):
        mkt = _make_marketplace([
            _github_plugin("dup", "a/b"),
            _github_plugin("dup", "c/d"),
        ])
        errs = rp.validate_marketplace(mkt)
        assert any("Duplicate" in e for e in errs)

    def test_missing_required_fields(self):
        errs = rp.validate_marketplace({})
        assert len(errs) > 0

    def test_plugins_not_a_list(self):
        mkt = _make_marketplace()
        mkt["plugins"] = "not-a-list"
        errs = rp.validate_marketplace(mkt)
        assert any("array" in e for e in errs)

    def test_plugin_not_a_dict(self):
        mkt = _make_marketplace()
        mkt["plugins"] = [42, "string"]
        errs = rp.validate_marketplace(mkt)
        assert len(errs) > 0
        # Should not crash — schema errors reported, no AttributeError
        assert not any("AttributeError" in e for e in errs)

    def test_missing_plugin_name(self):
        mkt = _make_marketplace()
        mkt["plugins"] = [{"source": {"source": "github", "repo": "a/b"}}]
        errs = rp.validate_marketplace(mkt)
        assert any("name" in e for e in errs)


# ═════════════════════════════════════════════════════════════════════════
# _filter_author
# ═════════════════════════════════════════════════════════════════════════


class TestFilterAuthor:
    def test_keeps_documented_fields(self):
        result = rp._filter_author({"name": "A", "email": "a@b", "url": "https://x.com"})
        assert result == {"name": "A", "email": "a@b", "url": "https://x.com"}

    def test_strips_unknown_fields(self):
        result = rp._filter_author({"name": "A", "bogus": True, "extra": 1})
        assert result == {"name": "A"}

    def test_returns_none_for_non_dict(self):
        assert rp._filter_author(None) is None
        assert rp._filter_author("string") is None
        assert rp._filter_author(42) is None

    def test_returns_none_for_empty_dict(self):
        assert rp._filter_author({}) is None

    def test_returns_none_when_all_fields_unknown(self):
        assert rp._filter_author({"bogus": True}) is None


# ═════════════════════════════════════════════════════════════════════════
# _ordered_dict
# ═════════════════════════════════════════════════════════════════════════


class TestOrderedDict:
    def test_canonical_order(self):
        d = {"description": "d", "name": "n", "version": "v", "source": "s"}
        keys = list(rp._ordered_dict(d).keys())
        assert keys == ["name", "source", "version", "description"]

    def test_unknown_keys_sorted_alphabetically_after_known(self):
        d = {"zzz": 1, "aaa": 2, "name": "n"}
        keys = list(rp._ordered_dict(d).keys())
        assert keys == ["name", "aaa", "zzz"]

    def test_preserves_values(self):
        d = {"name": "n", "version": "v"}
        assert rp._ordered_dict(d) == d

    def test_stable_across_runs(self):
        d = {"tags": ["a"], "name": "n", "source": "s", "description": "d", "author": {"name": "x"}}
        results = [json.dumps(rp._ordered_dict(d)) for _ in range(50)]
        assert len(set(results)) == 1


# ═════════════════════════════════════════════════════════════════════════
# merge_plugin
# ═════════════════════════════════════════════════════════════════════════


class TestMergePlugin:
    def test_protected_fields_kept_from_entry(self):
        entry = _github_plugin("kept-name", "kept/repo")
        source = {"name": "overwritten", "version": "2.0.0"}
        merged = rp.merge_plugin(entry, source)
        assert merged["name"] == "kept-name"
        assert merged["source"] == {"source": "github", "repo": "kept/repo"}

    def test_source_fields_override(self):
        entry = _github_plugin(version="1.0.0", description="old")
        source = {"version": "2.0.0", "description": "new"}
        merged = rp.merge_plugin(entry, source)
        assert merged["version"] == "2.0.0"
        assert merged["description"] == "new"

    def test_marketplace_only_preserved_when_absent_from_source(self):
        entry = _github_plugin(category="tools", tags=["t1"])
        source = {"version": "1.0.0"}
        merged = rp.merge_plugin(entry, source)
        assert merged["category"] == "tools"
        assert merged["tags"] == ["t1"]

    def test_marketplace_only_overwritten_when_in_source(self):
        entry = _github_plugin(category="old")
        source = {"category": "new"}
        merged = rp.merge_plugin(entry, source)
        assert merged["category"] == "new"

    def test_absent_source_fields_dropped(self):
        entry = _github_plugin(description="old", version="1.0.0")
        source = {"version": "2.0.0"}  # no description
        merged = rp.merge_plugin(entry, source)
        assert "description" not in merged

    def test_author_sanitised(self):
        entry = _github_plugin()
        source = {"author": {"name": "dev", "bogus": True}}
        merged = rp.merge_plugin(entry, source)
        assert merged["author"] == {"name": "dev"}

    def test_author_removed_when_empty(self):
        entry = _github_plugin()
        source = {"author": {"bogus": True}}
        merged = rp.merge_plugin(entry, source)
        assert "author" not in merged

    def test_author_removed_when_non_dict(self):
        entry = _github_plugin()
        source = {"author": "just a string"}
        merged = rp.merge_plugin(entry, source)
        assert "author" not in merged

    def test_deep_copy_isolation(self):
        entry = _github_plugin(tags=["a", "b"])
        source = {"keywords": ["k1"]}
        merged = rp.merge_plugin(entry, source)
        # Mutating merged should not affect originals
        merged["tags"].append("c")
        merged["keywords"].append("k2")
        assert entry["tags"] == ["a", "b"]
        assert source["keywords"] == ["k1"]

    def test_key_order_is_canonical(self):
        entry = _github_plugin(category="tools", tags=["t"])
        source = {
            "version": "1.0.0",
            "description": "desc",
            "author": {"name": "dev"},
            "license": "MIT",
        }
        merged = rp.merge_plugin(entry, source)
        keys = list(merged.keys())
        assert keys.index("name") < keys.index("source")
        assert keys.index("source") < keys.index("version")
        assert keys.index("version") < keys.index("description")
        assert keys.index("description") < keys.index("author")
        assert keys.index("category") < keys.index("tags")

    def test_key_order_stable_across_runs(self):
        entry = _github_plugin(category="tools", tags=["t"])
        source = {"version": "1.0.0", "description": "d", "license": "MIT"}
        results = [json.dumps(rp.merge_plugin(entry, source)) for _ in range(50)]
        assert len(set(results)) == 1


# ═════════════════════════════════════════════════════════════════════════
# fetch_plugin_config
# ═════════════════════════════════════════════════════════════════════════


class TestFetchPluginConfig:
    def test_success(self):
        plugin_json = {"name": "fetched", "version": "1.0.0"}
        api_resp = _make_github_api_response(plugin_json)
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_resp
        session = MagicMock()
        session.get.return_value = mock_resp

        result = rp.fetch_plugin_config("owner/repo", session)
        assert result == plugin_json
        session.get.assert_called_once()
        mock_resp.raise_for_status.assert_called_once()

    def test_unexpected_encoding_raises(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"encoding": "none", "content": ""}
        session = MagicMock()
        session.get.return_value = mock_resp

        with pytest.raises(ValueError, match="Unexpected encoding"):
            rp.fetch_plugin_config("owner/repo", session)

    def test_missing_encoding_raises(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": "abc"}
        session = MagicMock()
        session.get.return_value = mock_resp

        with pytest.raises(ValueError, match="Unexpected encoding"):
            rp.fetch_plugin_config("owner/repo", session)

    def test_http_error_propagates(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("404")
        session = MagicMock()
        session.get.return_value = mock_resp

        with pytest.raises(requests.HTTPError):
            rp.fetch_plugin_config("owner/repo", session)


# ═════════════════════════════════════════════════════════════════════════
# _serialize
# ═════════════════════════════════════════════════════════════════════════


class TestSerialize:
    def test_trailing_newline(self):
        assert rp._serialize({}).endswith("\n")

    def test_indented(self):
        result = rp._serialize({"a": 1})
        assert "  " in result

    def test_no_ascii_escaping(self):
        result = rp._serialize({"name": "café"})
        assert "café" in result
        assert "\\u" not in result


# ═════════════════════════════════════════════════════════════════════════
# _load_schema (caching)
# ═════════════════════════════════════════════════════════════════════════


class TestLoadSchema:
    def test_returns_dict(self):
        schema = rp._load_schema(str(rp.MARKETPLACE_SCHEMA_PATH))
        assert isinstance(schema, dict)
        assert "$defs" in schema

    def test_caching(self):
        rp._load_schema.cache_clear()
        rp._load_schema(str(rp.MARKETPLACE_SCHEMA_PATH))
        rp._load_schema(str(rp.MARKETPLACE_SCHEMA_PATH))
        info = rp._load_schema.cache_info()
        assert info.hits >= 1
        assert info.misses == 1


# ═════════════════════════════════════════════════════════════════════════
# cmd_validate
# ═════════════════════════════════════════════════════════════════════════


class TestCmdValidate:
    def test_valid_file_succeeds(self, capsys):
        rp.cmd_validate()
        out = capsys.readouterr().out
        assert "is valid" in out

    def test_missing_file_exits(self, tmp_path):
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = tmp_path / "nonexistent.json"
        try:
            with pytest.raises(SystemExit):
                rp.cmd_validate()
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_invalid_file_exits(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"not": "valid"}))
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = bad
        try:
            with pytest.raises(SystemExit) as exc_info:
                rp.cmd_validate()
            assert exc_info.value.code == 1
        finally:
            rp.MARKETPLACE_PATH = orig


# ═════════════════════════════════════════════════════════════════════════
# cmd_refresh
# ═════════════════════════════════════════════════════════════════════════


class TestCmdRefresh:
    def test_missing_file_exits(self, tmp_path):
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = tmp_path / "nonexistent.json"
        try:
            with pytest.raises(SystemExit):
                rp.cmd_refresh()
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_invalid_marketplace_exits_before_fetch(self, tmp_path):
        bad = tmp_path / "mkt.json"
        bad.write_text(json.dumps({"not": "valid"}))
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = bad
        try:
            with pytest.raises(SystemExit) as exc_info:
                rp.cmd_refresh()
            assert exc_info.value.code == 1
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_skips_relative_path_source(self, tmp_path, capsys):
        mkt = _make_marketplace([{"name": "local", "source": "./path"}])
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            rp.cmd_refresh()
            out = capsys.readouterr().out
            assert "Skipping local: relative-path" in out
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_skips_non_github_source(self, tmp_path, capsys):
        mkt = _make_marketplace([
            {"name": "npm-thing", "source": {"source": "npm", "package": "pkg"}},
        ])
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            rp.cmd_refresh()
            out = capsys.readouterr().out
            assert "Skipping npm-thing: non-GitHub source (npm)" in out
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_successful_refresh_bumps_version(self, tmp_path):
        plugin = _github_plugin("p", "owner/repo", version="1.0.0")
        mkt = _make_marketplace([plugin], metadata={"version": "0.0.1"})
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))

        source_config = {"name": "p", "version": "2.0.0", "description": "new"}
        api_resp = _make_github_api_response(source_config)

        mock_resp = MagicMock()
        mock_resp.json.return_value = api_resp
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            with patch("refresh_plugins.requests.Session", return_value=mock_session):
                rp.cmd_refresh()

            result = json.loads(f.read_text())
            assert result["metadata"]["version"] == "0.0.2"
            updated_plugin = result["plugins"][0]
            assert updated_plugin["version"] == "2.0.0"
            assert updated_plugin["description"] == "new"
            assert updated_plugin["name"] == "p"  # protected
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_no_change_no_version_bump(self, tmp_path):
        plugin = _github_plugin("p", "owner/repo", version="1.0.0", description="same")
        mkt = _make_marketplace([plugin], metadata={"version": "0.0.1"})
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))

        # Source returns exactly the same data
        source_config = {"name": "p", "version": "1.0.0", "description": "same"}
        api_resp = _make_github_api_response(source_config)

        mock_resp = MagicMock()
        mock_resp.json.return_value = api_resp
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            with patch("refresh_plugins.requests.Session", return_value=mock_session):
                rp.cmd_refresh()

            result = json.loads(f.read_text())
            assert result["metadata"]["version"] == "0.0.1"  # unchanged
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_fetch_error_exits_nonzero(self, tmp_path):
        plugin = _github_plugin("p", "owner/repo")
        mkt = _make_marketplace([plugin])
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("404")
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            with patch("refresh_plugins.requests.Session", return_value=mock_session):
                with pytest.raises(SystemExit) as exc_info:
                    rp.cmd_refresh()
            assert exc_info.value.code == 1
        finally:
            rp.MARKETPLACE_PATH = orig

    def test_invalid_fetched_config_exits_nonzero(self, tmp_path):
        plugin = _github_plugin("p", "owner/repo")
        mkt = _make_marketplace([plugin])
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))

        # Source config that fails validation (missing name, has extra field)
        bad_config = {"bogus": True}
        api_resp = _make_github_api_response(bad_config)

        mock_resp = MagicMock()
        mock_resp.json.return_value = api_resp
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            with patch("refresh_plugins.requests.Session", return_value=mock_session):
                with pytest.raises(SystemExit) as exc_info:
                    rp.cmd_refresh()
            assert exc_info.value.code == 1
        finally:
            rp.MARKETPLACE_PATH = orig


# ═════════════════════════════════════════════════════════════════════════
# main (entry point routing)
# ═════════════════════════════════════════════════════════════════════════


class TestMain:
    def test_validate_subcommand(self, capsys):
        with patch("sys.argv", ["refresh_plugins.py", "validate"]):
            rp.main()
        assert "is valid" in capsys.readouterr().out

    def test_default_runs_refresh(self, tmp_path):
        mkt = _make_marketplace()
        f = tmp_path / "mkt.json"
        f.write_text(json.dumps(mkt))
        orig = rp.MARKETPLACE_PATH
        rp.MARKETPLACE_PATH = f
        try:
            with patch("sys.argv", ["refresh_plugins.py"]):
                rp.main()
            # Refresh ran and wrote the file
            assert f.exists()
        finally:
            rp.MARKETPLACE_PATH = orig
