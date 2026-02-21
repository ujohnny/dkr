"""Test staleness detection."""

from conftest import dkr, _git, find_latest_image, staleness_check


def _make_stale_local(make_repo):
    """Create a repo where local master is 60 commits ahead of the image."""
    repo, _ = make_repo({
        "master": [
            {"message": "initial", "files": {"README.md": "v1"}},
        ],
    })

    dkr("create-image", str(repo), "master")
    image = find_latest_image(repo, "master")

    for i in range(60):
        (repo / f"file_{i}.txt").write_text(f"content {i}")
        _git(repo, "add", f"file_{i}.txt")
        _git(repo, "commit", "-m", f"commit {i}")

    return image, repo


def _make_stale_remote(make_repo, clone_repo):
    """Create a remote+local pair where origin/master is 60 commits ahead of the image."""
    remote, _ = make_repo({
        "master": [
            {"message": "initial", "files": {"README.md": "v1"}},
        ],
    })

    local = clone_repo(remote)

    dkr("create-image", str(local), "origin/master")
    image = find_latest_image(local, "origin/master")

    for i in range(60):
        (remote / f"file_{i}.txt").write_text(f"content {i}")
        _git(remote, "add", f"file_{i}.txt")
        _git(remote, "commit", "-m", f"commit {i}")

    _git(local, "fetch", "origin")

    return image, local


class TestStaleness:

    def test_stale_image_warns_and_prompts_update(self, make_repo, monkeypatch, capsys):
        """Stale image prints warning and returns 'update' when user says yes."""
        image, repo = _make_stale_local(make_repo)

        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: (prompts.append(p), "y")[1])
        result = staleness_check(image, repo)

        assert result == "update"

        output = capsys.readouterr().out
        assert "60 commits behind" in output
        assert any("update" in p.lower() for p in prompts)

    def test_stale_image_continues_on_decline(self, make_repo, monkeypatch, capsys):
        """Stale image returns True (proceed) when user declines update."""
        image, repo = _make_stale_local(make_repo)

        monkeypatch.setattr("builtins.input", lambda _: "n")
        result = staleness_check(image, repo)

        assert result is True

        output = capsys.readouterr().out
        assert "60 commits behind" in output

    def test_stale_remote_tracking(self, make_repo, clone_repo, monkeypatch, capsys):
        """Image created from origin/master detects staleness via remote tracking branch."""
        image, local = _make_stale_remote(make_repo, clone_repo)

        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: (prompts.append(p), "y")[1])
        result = staleness_check(image, local)

        assert result == "update"

        output = capsys.readouterr().out
        assert "60 commits behind" in output
