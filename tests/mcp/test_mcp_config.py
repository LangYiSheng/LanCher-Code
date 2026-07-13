from pathlib import Path

import yaml

from lancher_code.mcp.config import load_mcp_config
from lancher_code.mcp.template import MCP_CONFIG_TEMPLATE, ensure_user_mcp_config


def test_load_config_merges_layers_and_project_invalid_override_does_not_fallback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lancher").mkdir(parents=True)
    (project / ".lancher").mkdir(parents=True)
    (home / ".lancher" / "mcp.yaml").write_text(
        "mcp_servers:\n  shared:\n    type: stdio\n    command: python\n  user:\n    type: stdio\n    command: python\n",
        encoding="utf-8",
    )
    (project / ".lancher" / "mcp.yaml").write_text(
        "mcp_servers:\n  shared:\n    type: http\n    url: not-a-url\n  project:\n    type: http\n    url: https://example.com/mcp\n",
        encoding="utf-8",
    )
    configs, issues = load_mcp_config(project, home_dir=home)
    assert [config.name for config in configs] == ["user", "project"]
    assert any(issue.server_name == "shared" for issue in issues)


def test_load_config_expands_only_credentials_and_never_reports_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".lancher").mkdir(parents=True)
    (home / ".lancher" / "mcp.yaml").write_text(
        "mcp_servers:\n  api:\n    type: http\n    url: https://example.com/${HOST}\n    headers:\n      Authorization: Bearer ${TOKEN}\n",
        encoding="utf-8",
    )
    configs, issues = load_mcp_config(project, home_dir=home, environ={"TOKEN": "top-secret", "HOST": "bad"})
    assert configs[0].url == "https://example.com/${HOST}"
    assert configs[0].headers["Authorization"] == "Bearer top-secret"
    assert "top-secret" not in " ".join(issue.message for issue in issues)


def test_missing_environment_variable_disables_only_server(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".lancher").mkdir(parents=True)
    (home / ".lancher" / "mcp.yaml").write_text(
        "mcp_servers:\n  bad:\n    type: http\n    url: https://example.com/mcp\n    headers:\n      Authorization: ${MISSING}\n  good:\n    type: stdio\n    command: python\n",
        encoding="utf-8",
    )
    configs, issues = load_mcp_config(tmp_path / "project", home_dir=home, environ={})
    assert [config.name for config in configs] == ["good"]
    assert "MISSING" in issues[0].message


def test_template_is_valid_and_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    assert yaml.safe_load(MCP_CONFIG_TEMPLATE) == {"mcp_servers": {}}
    path = ensure_user_mcp_config(home_dir=tmp_path)
    path.write_text("mcp_servers: {custom: {enabled: false, type: stdio}}\n", encoding="utf-8")
    ensure_user_mcp_config(home_dir=tmp_path)
    assert "custom" in path.read_text(encoding="utf-8")
