"""Agent isolation selection: auto mode, fail-closed option, import safety."""
import importlib
import subprocess
import sys

import pytest

from aka.light.agent import sandbox


@pytest.fixture(autouse=True)
def reset_warning(monkeypatch):
    monkeypatch.setattr(sandbox, "_WARNED_UNISOLATED", False)


def test_import_does_not_require_repo_root_env(monkeypatch, tmp_path):
    code = "import aka.light.agent.sandbox as s; print(callable(s.resolve_sandbox_mode))"
    env = {"PATH": "/usr/bin:/bin", "AKA_LEGACY_REPO_ROOT": str(tmp_path)}  # invalid root
    result = subprocess.run([sys.executable, "-c", code], env={**env, "PYTHONPATH": ":".join(sys.path)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_auto_falls_back_and_warns_once_at_launch(monkeypatch, capsys):
    monkeypatch.setattr(sandbox.shutil, "which", lambda *a, **k: None)
    monkeypatch.delenv("ATREX_AGENT_REQUIRE_SANDBOX", raising=False)
    assert sandbox.resolve_sandbox_mode("auto", environment={}) == "none"
    assert capsys.readouterr().err == ""          # resolving alone is silent
    sandbox.enforce_isolation_policy("none", {})
    sandbox.enforce_isolation_policy("none", {})
    sandbox.enforce_isolation_policy("bwrap", {})
    assert capsys.readouterr().err.count("WITHOUT isolation") == 1


def test_native_launch_enforces_policy(monkeypatch):
    with pytest.raises(RuntimeError):
        sandbox.wrap_agent_command(["true"], sandbox.Path("/tmp"),
                                   {"ATREX_AGENT_SANDBOX": "none", "ATREX_AGENT_REQUIRE_SANDBOX": "1"})


def test_auto_selects_bwrap_when_usable(monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_bwrap_usable", lambda executable: True)
    assert sandbox.resolve_sandbox_mode("auto", environment={}) == "bwrap"
    monkeypatch.setattr(sandbox, "_bwrap_usable", lambda executable: False)
    assert sandbox.resolve_sandbox_mode("auto", environment={}) == "none"


def test_require_sandbox_fails_closed(monkeypatch):
    monkeypatch.delenv("ATREX_AGENT_REQUIRE_SANDBOX", raising=False)
    with pytest.raises(RuntimeError):
        sandbox.enforce_isolation_policy("none", {"ATREX_AGENT_REQUIRE_SANDBOX": "1"})
    monkeypatch.setenv("ATREX_AGENT_REQUIRE_SANDBOX", "true")
    with pytest.raises(RuntimeError):
        sandbox.enforce_isolation_policy("none", {})
    sandbox.enforce_isolation_policy("bwrap", {})  # isolated launches are fine


def test_explicit_bwrap_is_not_downgraded():
    assert sandbox.resolve_sandbox_mode("bwrap", environment={}) == "bwrap"
    with pytest.raises(ValueError):
        sandbox.resolve_sandbox_mode("docker", environment={})


def test_agent_options_resolve_auto(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda *a, **k: None)
    provider = importlib.import_module("aka.light.agent.provider")
    options = provider.AgentOptions(sandbox="auto")
    assert options.sandbox == "none"
    assert options.environment()["ATREX_AGENT_SANDBOX"] == "none"


def test_plugin_default_is_auto():
    plugin = importlib.import_module("aka.light.agent.plugin")
    assert plugin.Defaults["sandbox"] == "auto"
    assert "auto" in plugin.Config["properties"]["sandbox"]["enum"]


def test_bwrap_command_line_is_built(tmp_path, monkeypatch):
    """Exercise the full Bubblewrap argv construction with a stub executable."""
    from aka.light.agent.home import HOST_HOME_ENV, PREPARED_ENV
    if sandbox.platform.system() != "Linux":
        pytest.skip("Bubblewrap path is Linux-only")
    stub = tmp_path / "bin" / "bwrap"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    workspace = tmp_path / "campaign" / "ws"
    workspace.mkdir(parents=True)
    home = tmp_path / "homes" / "session"
    home.mkdir(parents=True)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    monkeypatch.setenv("AKA_LEGACY_REPO_ROOT", str(sandbox.Path(__file__).resolve().parents[1]))
    sandbox._repository_root.cache_clear()
    environment = {
        "ATREX_AGENT_SANDBOX": "bwrap",
        "ATREX_BWRAP_EXECUTABLE": str(stub),
        "PATH": f"{stub.parent}:/usr/bin:/bin",
        PREPARED_ENV: str(home),
        HOST_HOME_ENV: str(host_home),
    }
    launch, view = sandbox.wrap_agent_command(["true"], workspace, environment)
    argv = launch.command
    assert argv[0] == str(stub)
    assert "--unshare-pid" in argv and "--cap-drop" in argv
    assert ["--bind", str(workspace), str(workspace)] == argv[argv.index(str(workspace)) - 1:][:3]
    assert "/usr" in argv  # SYSTEM_PATHS allowlist mounted
    assert view is None
