"""Test list-images command."""

from conftest import dkr


class TestListImages:

    def test_lists_created_images(self, make_repo, capsys):
        """Create images for two branches, verify both appear in list output."""
        repo, _ = make_repo({
            "master": [
                {"message": "init", "files": {"README.md": "hi"}},
            ],
            "develop": {
                "from": "master:0",
                "commits": [
                    {"message": "dev work", "files": {"dev.txt": "dev"}},
                ],
            },
        })

        dkr("create-image", str(repo), "master")
        dkr("create-image", str(repo), "develop")

        dkr("list-images", str(repo))
        output = capsys.readouterr().out
        assert "master" in output
        assert "develop" in output
