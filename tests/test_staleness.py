"""Test staleness detection."""

from conftest import dkr, _git, find_latest_image, staleness_check


def _make_stale(make_repo, clone_repo=None, n=60):
    """Create image then advance the branch by *n* commits.

    If *clone_repo* is provided, creates a remote+local pair and builds
    from ``origin/master``.  Otherwise builds from local ``master``.

    Returns ``(image, repo_to_check)``.
    """
    remote, _ = make_repo({
        "master": [
            {"message": "initial", "files": {"README.md": "v1"}},
        ],
    })

    if clone_repo:
        local = clone_repo(remote)
        dkr("create-image", str(local), "origin/master")
        image = find_latest_image(local, "origin/master")
        commit_to = remote
    else:
        local = remote
        dkr("create-image", str(local), "master")
        image = find_latest_image(local, "master")
        commit_to = local

    for i in range(n):
        (commit_to / f"file_{i}.txt").write_text(f"content {i}")
        _git(commit_to, "add", f"file_{i}.txt")
        _git(commit_to, "commit", "-m", f"commit {i}")

    if clone_repo:
        _git(local, "fetch", "origin")

    return image, local


class TestStaleness:

    def test_stale_image_warns_and_prompts_update(self, make_repo, monkeypatch, capsys):
        """Stale image prints warning and returns 'update' when user says yes."""
        image, repo = _make_stale(make_repo)

        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: (prompts.append(p), "y")[1])
        result = staleness_check(image, repo)

        assert result == "update"

        output = capsys.readouterr().out
        assert "60 commits behind" in output
        assert any("update" in p.lower() for p in prompts)

    def test_stale_image_continues_on_decline(self, make_repo, monkeypatch, capsys):
        """Stale image returns True (proceed) when user declines update."""
        image, repo = _make_stale(make_repo)

        monkeypatch.setattr("builtins.input", lambda _: "n")
        result = staleness_check(image, repo)

        assert result is True

        output = capsys.readouterr().out
        assert "60 commits behind" in output

    def test_stale_remote_tracking(self, make_repo, clone_repo, monkeypatch, capsys):
        """Image created from origin/master detects staleness via remote tracking branch."""
        image, local = _make_stale(make_repo, clone_repo)

        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: (prompts.append(p), "y")[1])
        result = staleness_check(image, local)

        assert result == "update"

        output = capsys.readouterr().out
        assert "60 commits behind" in output
