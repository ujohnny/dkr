"""Fixtures and helpers for dkr integration tests."""

import getpass
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dkr import run_command, find_images, find_latest_image, staleness_check


# ---------------------------------------------------------------------------
# Git repo DSL
# ---------------------------------------------------------------------------

def _git(repo, *args, check=True):
    r = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=True, check=check,
    )
    return r.stdout.strip()


def _make_commit(repo, message, files):
    """Create/overwrite *files* and commit them."""
    for relpath, content in files.items():
        fpath = repo / relpath
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
        _git(repo, "add", str(relpath))
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _build_repo(repo_dir, spec):
    """Build a git repo from a declarative spec.

    *spec* is an **ordered** dict where keys are branch names.

    Plain list value → commits on that branch (first branch starts from init):
        ``"master": [{"message": "...", "files": {...}}, ...]``

    Dict value → branch from an existing commit, then apply commits:
        ``"feature": {"from": "master:0", "commits": [...]}``

    ``"from"`` format is ``"<branch>:<commit_index>"``.
    """
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "test@test.com")
    _git(repo_dir, "config", "user.name", "Test")

    # Track commit SHAs per branch: {"master": ["sha0", "sha1", ...]}
    branch_commits = {}

    first_branch = True
    for branch_name, branch_spec in spec.items():
        if isinstance(branch_spec, list):
            # Simple branch: list of commits
            commits_list = branch_spec
            from_ref = None
        else:
            # Branch with "from" and "commits"
            commits_list = branch_spec["commits"]
            from_ref = branch_spec.get("from")

        if first_branch:
            # First branch: we're on the initial branch after git init
            # Rename default branch to match
            _git(repo_dir, "checkout", "-b", branch_name, check=False)
            first_branch = False
        elif from_ref:
            # Parse "branch:index"
            src_branch, idx_str = from_ref.rsplit(":", 1)
            src_sha = branch_commits[src_branch][int(idx_str)]
            _git(repo_dir, "checkout", src_sha)
            _git(repo_dir, "checkout", "-b", branch_name)
        else:
            _git(repo_dir, "checkout", "-b", branch_name)

        shas = []
        for commit in commits_list:
            sha = _make_commit(repo_dir, commit["message"], commit["files"])
            shas.append(sha)
        branch_commits[branch_name] = shas

    # Checkout back to first branch
    first = next(iter(spec))
    _git(repo_dir, "checkout", first)

    return branch_commits


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def make_repo(tmp_path):
    """Factory fixture that creates git repos from a declarative spec.

    Returns a callable: ``make_repo(spec) -> (repo_path, branch_commits)``
    """
    counter = [0]

    def _factory(spec):
        repo_dir = tmp_path / f"repo_{counter[0]}"
        counter[0] += 1
        repo_dir.mkdir()
        branch_commits = _build_repo(repo_dir, spec)
        return repo_dir, branch_commits

    return _factory


def _dkr_image_ids():
    """Return the set of current dkr-managed image IDs."""
    r = subprocess.run(
        ["docker", "images", "--format", "{{.ID}}", "--filter", "label=dkr.repo_name"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    return set(r.stdout.strip().split("\n"))


@pytest.fixture(autouse=True)
def cleanup_dkr_images():
    """Remove only dkr images created during this test."""
    before = _dkr_image_ids()
    yield
    after = _dkr_image_ids()
    new_ids = after - before
    if new_ids:
        subprocess.run(["docker", "rmi", "-f"] + list(new_ids), capture_output=True, check=False)


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------

def dkr(*args):
    """Call a dkr command directly as a Python function."""
    run_command(list(args))


def docker_run_cmd(image_ref, *cmd):
    """Run a command in a container from *image_ref*, return stdout."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", cmd[0], image_ref] + list(cmd[1:]),
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()
