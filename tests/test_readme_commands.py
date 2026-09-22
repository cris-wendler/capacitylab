# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every `capacitylab` command in README.md, run as written, in README order, in an empty directory.

The README is the source of truth: a command added there is run here without editing this file. Commands that
are expected to stop with an error (no API key in a clean environment) are listed in EXPECTED_EXIT.
"""

import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"

# A clean environment has no API key, so a model run must stop before any call, with exit code 2.
EXPECTED_EXIT = {"capacitylab run campaign-overlap --provider anthropic": 2}


def readme_commands() -> list[str]:
    commands = []
    for block in re.findall(r"```bash\n(.*?)```", README.read_text(), flags=re.S):
        for line in block.replace("\\\n", " ").splitlines():
            line = line.split(" #", 1)[0].strip()
            if line.startswith("capacitylab "):
                commands.append(" ".join(line.split()))
    return commands


def clean_env(tmp_path: Path) -> dict[str, str]:
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "LANG")}
    return {**keep, "PYTHONUNBUFFERED": "1", "HOME": str(tmp_path / "home")}


def run(command: str, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "capacitylab", *shlex.split(command)[1:]]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)


def serve_and_fetch(command: str, cwd: Path, env: dict) -> None:
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            pytest.skip("port 8765 is already in use")
    argv = [sys.executable, "-m", "capacitylab", *shlex.split(command)[1:]]
    server = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8765/", timeout=2) as page:
                    assert page.status == 200 and "CapacityLab" in page.read().decode()
                    return
            except OSError:
                time.sleep(0.2)
        raise AssertionError("the web UI did not answer on http://127.0.0.1:8765")
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_readme_names_the_offline_quickstart():
    commands = readme_commands()
    assert "capacitylab run campaign-overlap --out runs/demo.json" in commands
    assert set(EXPECTED_EXIT) <= set(commands), "EXPECTED_EXIT names a command the README no longer has"


def test_every_readme_command_runs(tmp_path):
    env = clean_env(tmp_path)
    commands = readme_commands()
    servers = [c for c in commands if c.startswith("capacitylab serve")]
    for command in (c for c in commands if c not in servers):
        result = run(command, tmp_path, env)
        expected = EXPECTED_EXIT.get(command, 0)
        assert result.returncode == expected, f"{command}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        assert "Traceback" not in result.stderr, f"{command}\n{result.stderr[-2000:]}"
    assert (tmp_path / "runs" / "demo.json").is_file()
    for command in servers:  # last, because it can skip when the port is taken
        serve_and_fetch(command, tmp_path, env)
