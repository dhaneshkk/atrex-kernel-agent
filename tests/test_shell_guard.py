"""Behavioural tests for the coding-agent BASH_ENV guard (both packaged copies)."""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARDS = [
    ROOT / "src/aka/light/agent/session_shell_guard.sh",
    ROOT / "src/aka/legacy/execution/runtime_tools/session_shell_guard.sh",
]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


@pytest.fixture()
def fake_module(tmp_path):
    package = tmp_path / "vllm"
    package.mkdir()
    (package / "__init__.py").write_text("print('IMPORTED')\n")
    (package / "__main__.py").write_text("print('IMPORTED')\n")
    return tmp_path


def run(guard, script, cwd, stdin=""):
    env = dict(os.environ, PYTHONPATH=str(cwd))
    return subprocess.run(["bash", "-c", f"set -u; source {guard}\n" + textwrap.dedent(script)],
                          cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("guard", GUARDS, ids=["light", "legacy"])
@pytest.mark.parametrize("command", [
    "python3 -c 'import vllm'",
    "python3 -W ignore -c 'import vllm'",
    "python3 -X dev -c 'import vllm'",
    "python3 -Bc 'import vllm'",
    "python3 -m vllm",
    "python3 -m vllm.entrypoints",
    "python3 -c 'import importlib; importlib.import_module(\"vllm\")'",
])
def test_blocked_imports(guard, command, fake_module):
    result = run(guard, command + "\necho rc=$?", fake_module)
    assert "rc=126" in result.stdout
    assert "IMPORTED" not in result.stdout


@pytest.mark.parametrize("guard", GUARDS, ids=["light", "legacy"])
def test_snippets_keep_stdin_argv_and_exit_code(guard, tmp_path):
    result = run(guard, """
        python3 -c 'import sys; print(sys.argv); print(repr(sys.stdin.read()))' a b
        python3 -B -c 'import sys; print(sys.flags.dont_write_bytecode)'
        python3 -c 'import os; print("_ATREX_GUARDED_CODE" in os.environ)'
        python3 -c 's = "import vllm is prose"; print(len(s))'
        python3 -c 'raise SystemExit(7)'; echo rc=$?
    """, tmp_path, stdin="hello\n")
    lines = result.stdout.splitlines()
    assert lines[0] == "['-c', 'a', 'b']"
    assert lines[1] == "'hello\\n'"
    assert lines[2] == "1"
    assert lines[3] == "False"
    assert lines[4] == "20"
    assert lines[5] == "rc=7"


@pytest.mark.parametrize("guard", GUARDS, ids=["light", "legacy"])
def test_scripts_and_allowed_modules_run(guard, tmp_path):
    (tmp_path / "s.py").write_text("print('script ok')\n")
    result = run(guard, "python3 s.py\necho '{\"a\": 1}' | python3 -m json.tool --compact", tmp_path)
    assert result.stdout.splitlines() == ["script ok", '{"a":1}']


def test_guard_copies_differ_only_in_messages():
    def normalize(path):
        text = path.read_text()
        return [line for line in text.splitlines()
                if "must run through" not in line and "first argument is" not in line]
    light, legacy = (normalize(path) for path in GUARDS)
    assert light == legacy
