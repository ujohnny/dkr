"""Test staleness detection."""

from conftest import dkr, _git, find_latest_image, staleness_check


def _make_stale_setup(make_repo):
    """Create an image then add 60 commits to simulate staleness. Returns (image, repo)."""
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


class TestStaleness:

    def test_stale_image_warns_and_prompts_update(self, make_repo, monkeypatch, capsys):
        """Stale image prints warning and returns 'update' when user says yes."""
        image, repo = _make_stale_setup(make_repo)

        prompts = []
        monkeypatch.setattr("builtins.input", lambda p: (prompts.append(p), "y")[1])
        result = staleness_check(image, repo)

        assert result == "update"

        output = capsys.readouterr().out
        assert "60 commits behind" in output
        assert any("update" in p.lower() for p in prompts)

    def test_stale_image_continues_on_decline(self, make_repo, monkeypatch, capsys):
        """Stale image returns True (proceed) when user declines update."""
        image, repo = _make_stale_setup(make_repo)

        monkeypatch.setattr("builtins.input", lambda _: "n")
        result = staleness_check(image, repo)

        assert result is True

        output = capsys.readouterr().out
        assert "60 commits behind" in output
