"""Test update-image command."""

from conftest import dkr, docker_run_cmd, _git, find_images


class TestUpdateImage:

    def test_update_adds_new_commits(self, make_repo):
        """Create image, add commit to repo, update image, verify new content."""
        repo, commits = make_repo({
            "master": [
                {"message": "initial", "files": {"README.md": "v1"}},
            ],
        })

        dkr("create-image", str(repo), "master")

        # Add a new commit directly to the repo
        (repo / "new_file.txt").write_text("added after image")
        _git(repo, "add", "new_file.txt")
        _git(repo, "commit", "-m", "post-image commit")

        dkr("update-image", str(repo), "master")

        images = find_images(repo, "master")
        assert len(images) >= 1
        img = images[0]
        assert img["labels"]["dkr.type"] == "update"

        tag = img["tags"][0]
        assert docker_run_cmd(tag, "cat", "/workspace/new_file.txt") == "added after image"
