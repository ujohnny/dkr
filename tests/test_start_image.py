"""Test start-image command."""

import re

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
