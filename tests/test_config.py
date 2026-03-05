"""Tests for configuration loading."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from blink_sync_sentry.config import AppConfig, build_config, load_yaml


@pytest.fixture()
def yaml_file(tmp_path: Path) -> Path:
    """Write a sample single-account YAML config and return its path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent("""\
            network_name: MyHome
            sync_module_name: MySyncModule
            token_file: /tmp/test_token.json
            retention:
              retention_days: 14
              max_usage_gb: 4.0
            watch:
              interval_minutes: 30
            archive_dir: /tmp/archive
            request_delay_seconds: 1.5
        """)
    )
    return cfg


@pytest.fixture()
def multi_account_yaml(tmp_path: Path) -> Path:
    """Write a multi-account YAML config and return its path."""
    cfg = tmp_path / "multi.yaml"
    cfg.write_text(
        textwrap.dedent("""\
            retention:
              retention_days: 30
            archive_dir: /tmp/global_archive
            request_delay_seconds: 1.0
            watch:
              interval_minutes: 45
            accounts:
              - name: home
                username: home@example.com
                password: pass1
                token_file: /tmp/home_token.json
                sync_module_names:
                  - FrontYard
                  - BackYard
                retention:
                  retention_days: 7
                archive_dir: /tmp/home_archive
              - name: office
                username: office@example.com
                password: pass2
                sync_module_name: Lobby
                retention:
                  max_usage_gb: 2.0
        """)
    )
    return cfg


class TestLoadYaml:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_yaml(tmp_path / "nope.yaml") == {}

    def test_valid_file(self, yaml_file: Path) -> None:
        data = load_yaml(yaml_file)
        assert data["network_name"] == "MyHome"
        assert data["retention"]["retention_days"] == 14

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        assert load_yaml(empty) == {}


class TestBuildConfigSingleAccount:
    """Test backward-compatible single-account (flat) config layout."""

    def test_defaults_without_file(self, tmp_path: Path) -> None:
        cfg = build_config(config_path=tmp_path / "missing.yaml")
        assert isinstance(cfg, AppConfig)
        assert len(cfg.accounts) == 1
        assert cfg.accounts[0].name == "default"
        assert cfg.retention.retention_days is None
        assert cfg.watch.interval_minutes == 60
        assert cfg.request_delay_seconds == 2.0

    def test_yaml_values(self, yaml_file: Path) -> None:
        cfg = build_config(config_path=yaml_file)
        assert len(cfg.accounts) == 1
        acct = cfg.accounts[0]
        assert acct.sync_module_names == ["MySyncModule"]
        assert acct.token_file == "/tmp/test_token.json"
        # Global retention from YAML
        assert cfg.retention.retention_days == 14
        assert cfg.retention.max_usage_gb == 4.0
        # Account-level retention mirrors global for flat layout
        assert acct.retention.retention_days == 14
        assert cfg.watch.interval_minutes == 30
        assert cfg.archive_dir == "/tmp/archive"
        assert cfg.request_delay_seconds == 1.5

    def test_env_overrides_yaml(self, yaml_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLINK_USERNAME", "env_user")
        monkeypatch.setenv("BLINK_PASSWORD", "env_pass")
        cfg = build_config(config_path=yaml_file)
        acct = cfg.accounts[0]
        assert acct.username == "env_user"
        assert acct.password == "env_pass"
        # YAML values should still be present
        assert acct.sync_module_names == ["MySyncModule"]

    def test_cli_overrides_all(self, yaml_file: Path) -> None:
        cli = {
            "retention": {"retention_days": 7},
            "sync_module_name": "OtherModule",
        }
        cfg = build_config(config_path=yaml_file, cli_overrides=cli)
        # Global retention overridden by CLI
        assert cfg.retention.retention_days == 7
        # Account-level sync_module_name overridden
        acct = cfg.accounts[0]
        assert acct.sync_module_names == ["OtherModule"]
        # max_usage_gb should still come from YAML
        assert cfg.retention.max_usage_gb == 4.0

    def test_no_prompt_default_false(self, tmp_path: Path) -> None:
        cfg = build_config(config_path=tmp_path / "missing.yaml")
        assert cfg.no_prompt is False
        assert cfg.accounts[0].no_prompt is False

    def test_no_prompt_cli_override(self, tmp_path: Path) -> None:
        cfg = build_config(
            config_path=tmp_path / "missing.yaml",
            cli_overrides={"no_prompt": True},
        )
        assert cfg.no_prompt is True
        assert cfg.accounts[0].no_prompt is True


class TestBuildConfigMultiAccount:
    """Test multi-account config layout."""

    def test_parses_multiple_accounts(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        assert len(cfg.accounts) == 2

    def test_account_names(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        names = [a.name for a in cfg.accounts]
        assert names == ["home", "office"]

    def test_account_credentials(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        assert home.username == "home@example.com"
        assert home.password == "pass1"
        assert office.username == "office@example.com"
        assert office.password == "pass2"

    def test_account_sync_module_names(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        assert home.sync_module_names == ["FrontYard", "BackYard"]
        # Single string → list
        assert office.sync_module_names == ["Lobby"]

    def test_per_account_retention(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        assert home.retention.retention_days == 7
        assert office.retention.max_usage_gb == 2.0

    def test_effective_retention_fallback(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        # home has retention_days=7, but no max_usage_gb → falls back to global (None)
        eff_home = cfg.effective_retention(home)
        assert eff_home.retention_days == 7
        assert eff_home.max_usage_gb is None
        # office has max_usage_gb=2.0 but no retention_days → falls back to global 30
        eff_office = cfg.effective_retention(office)
        assert eff_office.retention_days == 30
        assert eff_office.max_usage_gb == 2.0

    def test_effective_archive_dir_fallback(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        # home overrides archive_dir
        assert cfg.effective_archive_dir(home) == "/tmp/home_archive"
        # office has no archive_dir → falls back to global
        assert cfg.effective_archive_dir(office) == "/tmp/global_archive"

    def test_per_account_token_file(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        home = cfg.accounts[0]
        office = cfg.accounts[1]
        assert home.token_file == "/tmp/home_token.json"
        # office has no explicit token_file → gets default with account name
        assert "token_office.json" in office.token_file

    def test_per_account_env_override(
        self, multi_account_yaml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BLINK_OFFICE_USERNAME", "env_office@example.com")
        monkeypatch.setenv("BLINK_OFFICE_PASSWORD", "env_secret")
        cfg = build_config(config_path=multi_account_yaml)
        office = cfg.accounts[1]
        assert office.username == "env_office@example.com"
        assert office.password == "env_secret"
        # home should be unaffected
        home = cfg.accounts[0]
        assert home.username == "home@example.com"

    def test_global_settings(self, multi_account_yaml: Path) -> None:
        cfg = build_config(config_path=multi_account_yaml)
        assert cfg.retention.retention_days == 30
        assert cfg.archive_dir == "/tmp/global_archive"
        assert cfg.request_delay_seconds == 1.0
        assert cfg.watch.interval_minutes == 45
