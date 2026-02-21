"""Test start-image command."""

import json
import re

from conftest import dkr, _git


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

    def test_workspace_trusted(self, make_repo, capfd):
        """Entrypoint creates .claude.json with /workspace trust."""
        repo, _ = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--agent", "none", "--",
            "bash", "-c", "cat /root/.claude.json")

        output = capfd.readouterr().out
        data = json.loads(output.split("\n")[-2])  # last non-empty line
        assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True

    def test_anthropic_key_mounted(self, make_repo, tmp_path, capfd):
        """--anthropic-key mounts the key file and creates settings.json with apiKeyHelper."""
        key_file = tmp_path / "api_key"
        key_file.write_text("sk-test-key-12345")

        repo, _ = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master",
            "--anthropic-key", str(key_file), "--agent", "none", "--",
            "bash", "-c",
            "cat /root/.claude/settings.json && echo '---' && cat /run/secrets/anthropic_key")

        output = capfd.readouterr().out
        # settings.json should have apiKeyHelper
        settings_line = [l for l in output.split("\n") if "apiKeyHelper" in l]
        assert settings_line, f"Expected apiKeyHelper in output:\n{output}"
        settings = json.loads(settings_line[0])
        assert settings["apiKeyHelper"] == "cat /run/secrets/anthropic_key"
        # Key file content should be readable
        assert "sk-test-key-12345" in output

    def test_git_push_to_host(self, make_repo, capfd):
        """git push from container creates a branch on the host repo."""
        repo, _ = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
            ],
        })

        dkr("create-image", str(repo), "master")
        dkr("start-image", str(repo), "master", "--name", "push-test", "--",
            "bash", "-c",
            "git config user.email test@test.com && "
            "git config user.name Test && "
            "echo pushed > pushed.txt && "
            "git add pushed.txt && "
            "git commit -m 'from container' && "
            "git push host")

        # Verify branch exists on host
        branches = _git(repo, "branch", "--list", "push-test")
        assert "push-test" in branches

        # Verify pushed content
        content = _git(repo, "show", "push-test:pushed.txt")
        assert content == "pushed"
