"""Test start-image command."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import dkr


class TestStartImage:

    def test_random_branch_on_start(self, make_repo, capfd):
        """Container entrypoint creates a random adjective-noun branch tracking host/<branch>."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--", "true")

        output = capfd.readouterr().out

        # Entrypoint prints: "Working copy updated to <sha> on branch <name> (tracking host/<branch>)"
        m = re.search(r"on branch (\S+) \(tracking (host/\S+)\)", output)
        assert m, f"Expected branch info in entrypoint output, got:\n{output}"

        branch = m.group(1)
        upstream = m.group(2)

        # Branch name should be adjective-noun format
        assert "-" in branch, f"Expected adjective-noun branch, got: {branch}"
        assert branch != "master", "Branch should not be master"
        # Upstream should track host/master
        assert upstream == "host/master"

    def test_custom_branch_name(self, make_repo, capfd):
        """--name sets the working branch name instead of random."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--name", "my-feature", "--", "true")

        output = capfd.readouterr().out

        m = re.search(r"on branch (\S+) \(tracking (host/\S+)\)", output)
        assert m, f"Expected branch info in entrypoint output, got:\n{output}"

        assert m.group(1) == "my-feature"
        assert m.group(2) == "host/master"

    def test_volumes_from_dkr_conf(self, make_repo, tmp_path, capfd):
        """Volumes defined in .dkr.conf are mounted in the container."""
        host_dir = tmp_path / "shared"
        host_dir.mkdir()
        (host_dir / "marker.txt").write_text("from-host")

        repo, _ = make_repo({
            "master": [
                {"message": "initial", "files": {
                    "README.md": "hello",
                    ".dkr.conf": f"""\
volumes = {host_dir}:/mnt/shared
""",
                }},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--",
            "cat", "/mnt/shared/marker.txt")

        output = capfd.readouterr().out
        assert "from-host" in output

    def test_claude_json_mounted(self, make_repo, capfd):
        """~/.claude.json from host is available inside the container."""
        host_claude_json = Path.home() / ".claude.json"
        if not host_claude_json.exists():
            pytest.skip("~/.claude.json not found on host")

        host_data = json.loads(host_claude_json.read_text())
        host_uuid = host_data["oauthAccount"]["accountUuid"]

        repo, _ = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--agent", "none", "--",
            "bash", "-c",
            "jq -r '.oauthAccount.accountUuid' /root/.claude.json && "
            "jq -r '.projects[\"/workspace\"].hasTrustDialogAccepted' /root/.claude.json")

        output = capfd.readouterr().out
        assert host_uuid in output
        assert "true" in output  # /workspace trusted
