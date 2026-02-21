"""Test create-image command."""

import subprocess

from conftest import dkr, docker_run_cmd, _git, find_images


class TestCreateImage:

    def test_basic(self, make_repo):
        """Create an image from master with 2 commits, verify image and content."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello world"}},
                {"message": "add source", "files": {"src/main.py": "print('hi')"}},
            ],
        })

        dkr("create-image", str(repo), "master")

        # Image exists with correct labels
        images = find_images(repo, "master")
        assert len(images) >= 1
        img = images[0]
        assert img["labels"]["dkr.repo_path"] == str(repo)
        assert img["labels"]["dkr.branch"] == "master"
        assert img["labels"]["dkr.commit"] == commits["master"][-1]
        assert img["labels"]["dkr.type"] == "base"

        tag = img["tags"][0]

        # Verify file content inside container
        assert docker_run_cmd(tag, "cat", "/workspace/README.md") == "hello world"
        assert docker_run_cmd(tag, "cat", "/workspace/src/main.py") == "print('hi')"

        # Verify commit history
        log = docker_run_cmd(tag, "git", "-C", "/workspace", "log", "--oneline")
        assert len(log.strip().split("\n")) == 2

    def test_specific_branch(self, make_repo):
        """Create an image from a feature branch, verify branch-specific files."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "hello"}},
                {"message": "second", "files": {"common.txt": "shared"}},
            ],
            "feature": {
                "from": "master:0",
                "commits": [
                    {"message": "feature work", "files": {"feature.py": "x = 1"}},
                ],
            },
        })

        dkr("create-image", str(repo), "feature")

        images = find_images(repo, "feature")
        assert len(images) >= 1
        tag = images[0]["tags"][0]

        # Feature branch has README (from master:0) and feature.py
        assert docker_run_cmd(tag, "cat", "/workspace/README.md") == "hello"
        assert docker_run_cmd(tag, "cat", "/workspace/feature.py") == "x = 1"

        # Feature branch should NOT have common.txt (added after branch point)
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "cat", tag, "/workspace/common.txt"],
            capture_output=True, text=True, check=False,
        )
        assert r.returncode != 0

    def test_dkr_conf(self, make_repo):
        """Create an image with .dkr.conf that adds a post_clone step."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {
                    "README.md": "hello",
                    ".dkr.conf": """\
base_image = fedora:43

[post_clone]
RUN echo post_clone_marker > /tmp/marker.txt
""",
                }},
            ],
        })

        dkr("create-image", str(repo), "master")

        images = find_images(repo, "master")
        assert len(images) >= 1
        tag = images[0]["tags"][0]

        # Verify post_clone step ran
        assert docker_run_cmd(tag, "cat", "/tmp/marker.txt") == "post_clone_marker"

    def test_remote_ref(self, make_repo, clone_repo):
        """Create an image from a remote ref with unusual remote name (space/main)."""
        remote, _ = make_repo({
            "main": [
                {"message": "initial", "files": {"README.md": "from remote"}},
                {"message": "second", "files": {"remote.txt": "remote content"}},
            ],
        })

        local = clone_repo(remote)
        # Add a second remote with an unusual name
        _git(local, "remote", "add", "space", str(remote))
        _git(local, "fetch", "space")

        dkr("create-image", str(local), "space/main")

        images = find_images(local, "space/main")
        assert len(images) >= 1
        img = images[0]
        assert img["labels"]["dkr.branch"] == "main"
        assert img["labels"]["dkr.branch_from"] == "space/main"

        tag = img["tags"][0]
        assert docker_run_cmd(tag, "cat", "/workspace/README.md") == "from remote"
        assert docker_run_cmd(tag, "cat", "/workspace/remote.txt") == "remote content"
