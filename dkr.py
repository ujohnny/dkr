#!/usr/bin/env python3
"""dkr — Docker dev environment builder for large git repos."""

import argparse
import getpass
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STALENESS_THRESHOLD = 50
HOST_ADDR = "host.docker.internal" if platform.system() == "Darwin" else "::1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, *, check=True, capture=True, **kwargs):
    """Run a command, return CompletedProcess."""
    if capture:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("text", True)
    return subprocess.run(cmd, check=check, **kwargs)


def git(repo_path, *args, check=True):
    """Run a git command in *repo_path* and return stdout stripped."""
    r = run(["git", "-C", str(repo_path)] + list(args), check=check)
    return r.stdout.strip() if r.stdout else ""


def is_git_repo(path):
    try:
        git(path, "rev-parse", "--git-dir")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resolve_repo(arg):
    """Return absolute path to the git repo, validated."""
    path = Path(arg or os.getcwd()).resolve()
    if not is_git_repo(path):
        sys.exit(f"Error: {path} is not a git repository")
    return path


def parse_branch_ref(branch_from, repo_path=None):
    """Return (remote_or_none, branch_name).

    If *branch_from* looks like ``origin/main`` and the prefix is an actual
    git remote, split into (remote, branch).  Otherwise treat the whole
    string as a local branch name (e.g. ``enovozhilov/feature-example``).
    """
    if not branch_from or branch_from == "HEAD":
        return None, "HEAD"
    if "/" in branch_from and repo_path:
        candidate_remote, _, branch = branch_from.partition("/")
        # Check if the prefix is a real remote in this repo
        remotes = git(repo_path, "remote", check=False).split("\n")
        if candidate_remote in remotes:
            return candidate_remote, branch
    return None, branch_from


def fetch_if_remote(repo_path, branch_from):
    """If branch_from references a remote, fetch that single branch."""
    remote, branch = parse_branch_ref(branch_from, repo_path)
    if remote:
        print(f"Fetching {branch} from {remote}...")
        git(repo_path, "fetch", remote, branch)
    return remote, branch


def resolve_commit(repo_path, branch_from):
    """Resolve branch_from to a full commit SHA in the local repo."""
    return git(repo_path, "rev-parse", branch_from)


def sanitize_tag(name):
    """Sanitize a string for use in a Docker tag."""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", name)


def image_tag(repo_path, branch):
    """Build the canonical Docker tag for a repo+branch."""
    repo_name = sanitize_tag(repo_path.name)
    branch_san = sanitize_tag(branch)
    return f"dkr:{repo_name}-{branch_san}"


def build_labels(repo_path, branch, commit, image_type):
    """Return a dict of labels to apply to the image."""
    return {
        "dkr.repo_path": str(repo_path),
        "dkr.repo_name": repo_path.name,
        "dkr.branch": branch,
        "dkr.commit": commit,
        "dkr.created_at": datetime.now(timezone.utc).isoformat(),
        "dkr.type": image_type,
    }


def label_args(labels):
    """Convert a label dict to a flat list of --label k=v args."""
    out = []
    for k, v in labels.items():
        out += ["--label", f"{k}={v}"]
    return out


def find_images(repo_path=None, branch=None):
    """Query Docker for dkr-managed images, optionally filtered.

    Returns a list of dicts sorted by created_at descending.
    """
    cmd = ["docker", "images", "--format", "{{json .}}"]
    # We can't filter by arbitrary labels with --filter in docker images,
    # so we inspect all images with our label prefix.
    cmd_all = ["docker", "images", "--format", "{{.ID}}", "--filter", "label=dkr.repo_name"]
    r = run(cmd_all, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return []

    image_ids = list(dict.fromkeys(r.stdout.strip().split("\n")))  # dedupe, preserve order
    if not image_ids:
        return []

    r = run(["docker", "inspect"] + image_ids, check=False)
    if r.returncode != 0:
        return []

    images = json.loads(r.stdout)
    results = []
    for img in images:
        labels = img.get("Config", {}).get("Labels") or {}
        if "dkr.repo_name" not in labels:
            continue
        if repo_path and labels.get("dkr.repo_path") != str(repo_path):
            continue
        if branch and labels.get("dkr.branch") != branch:
            continue
        # Collect tags
        tags = img.get("RepoTags") or []
        results.append({
            "id": img["Id"],
            "tags": tags,
            "labels": labels,
        })

    results.sort(key=lambda x: x["labels"].get("dkr.created_at", ""), reverse=True)
    return results


def find_latest_image(repo_path=None, branch=None):
    """Return the most recent dkr image matching the filters, or None."""
    imgs = find_images(repo_path, branch)
    return imgs[0] if imgs else None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def resolve_ssh_key(args):
    """Return the absolute path to the SSH key, validated."""
    key = Path(args.ssh_key).expanduser().resolve()
    if not key.exists():
        sys.exit(f"Error: SSH key not found: {key}")
    return key


def cmd_create_image(args):
    ssh_key = resolve_ssh_key(args)
    repo_path = resolve_repo(args.git_repo)
    branch_from = args.branch_from or "HEAD"

    remote, branch = fetch_if_remote(repo_path, branch_from)
    commit = resolve_commit(repo_path, branch_from)
    # For checkout inside container: use the plain branch name
    checkout_branch = branch if remote else branch_from

    tag = image_tag(repo_path, checkout_branch)
    labels = build_labels(repo_path, checkout_branch, commit, "base")
    user = getpass.getuser()

    print(f"Building image {tag} from {repo_path} @ {branch_from} ({commit[:12]})")

    cmd = [
        "docker", "build",
        "--ssh", f"default={ssh_key}",
        "--network=host",
        "--build-arg", f"REPO_PATH={repo_path}",
        "--build-arg", f"BRANCH={checkout_branch}",
        "--build-arg", f"GIT_USER={user}",
        "--build-arg", f"HOST_ADDR={HOST_ADDR}",
        "--tag", tag,
        "-f", str(SCRIPT_DIR / "Dockerfile.create"),
    ] + label_args(labels) + [str(SCRIPT_DIR)]

    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    subprocess.run(cmd, check=True, env=env)

    print(f"Image built: {tag}")


def cmd_update_image(args):
    ssh_key = resolve_ssh_key(args)
    repo_path = resolve_repo(args.git_repo)
    branch_from = args.branch_from or "HEAD"

    remote, branch = fetch_if_remote(repo_path, branch_from)
    checkout_branch = branch if remote else branch_from

    base = find_latest_image(repo_path, checkout_branch)
    if not base:
        sys.exit(
            f"Error: no existing image found for {repo_path.name}/{checkout_branch}. "
            "Run create-image first."
        )

    base_ref = base["tags"][0] if base["tags"] else base["id"]
    commit = resolve_commit(repo_path, branch_from)
    tag = image_tag(repo_path, checkout_branch)
    labels = build_labels(repo_path, checkout_branch, commit, "update")
    user = getpass.getuser()

    print(f"Updating image from {base_ref} -> {repo_path} @ {branch_from} ({commit[:12]})")

    cmd = [
        "docker", "build",
        "--ssh", f"default={ssh_key}",
        "--network=host",
        "--build-arg", f"BASE_IMAGE={base_ref}",
        "--build-arg", f"REPO_PATH={repo_path}",
        "--build-arg", f"BRANCH={checkout_branch}",
        "--build-arg", f"GIT_USER={user}",
        "--build-arg", f"HOST_ADDR={HOST_ADDR}",
        "--tag", tag,
        "-f", str(SCRIPT_DIR / "Dockerfile.update"),
    ] + label_args(labels) + [str(SCRIPT_DIR)]

    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    subprocess.run(cmd, check=True, env=env)

    print(f"Image updated: {tag}")


def staleness_check(image, repo_path):
    """Check if the image is stale vs its branch. Returns True if we should proceed."""
    labels = image["labels"]
    image_commit = labels.get("dkr.commit")
    branch = labels.get("dkr.branch")
    if not image_commit or not branch:
        return True

    # Check if the branch ref exists in the local repo
    if git(repo_path, "rev-parse", "--verify", branch, check=False) == "":
        return True

    # Count commits between image and current branch tip
    try:
        count_str = git(repo_path, "rev-list", "--count", f"{image_commit}..{branch}")
        behind = int(count_str)
    except (subprocess.CalledProcessError, ValueError):
        # Can't compare — maybe commit was rebased away
        tag_str = ", ".join(image["tags"]) or image["id"]
        print(f"Warning: cannot verify image {tag_str} against {branch}.")
        print("The image commit may have been force-pushed away. Consider running create-image.")
        resp = input("Start anyway? [y/N] ").strip().lower()
        return resp in ("y", "yes")

    if behind > STALENESS_THRESHOLD:
        tag_str = ", ".join(image["tags"]) or image["id"]
        print(f"Warning: image {tag_str} is {behind} commits behind {branch}.")
        resp = input("Do you want to update the image before starting? [y/N] ").strip().lower()
        if resp in ("y", "yes"):
            return "update"

    return True


def cmd_start_image(args):
    repo_path = resolve_repo(args.git_repo) if args.git_repo else None
    branch_from = args.branch_from

    if branch_from:
        _, branch = parse_branch_ref(branch_from)
    else:
        branch = None

    image = find_latest_image(repo_path, branch)
    if not image:
        label = ""
        if repo_path:
            label += f" for {repo_path.name}"
        if branch:
            label += f"/{branch}"
        sys.exit(f"Error: no dkr image found{label}. Run create-image first.")

    # Resolve repo_path from image labels if not provided
    if not repo_path:
        rp = image["labels"].get("dkr.repo_path")
        if rp:
            repo_path = Path(rp)

    # Staleness check
    if repo_path and is_git_repo(repo_path):
        result = staleness_check(image, repo_path)
        if result == "update":
            # Build a fake args namespace for update
            class UpdateArgs:
                pass
            ua = UpdateArgs()
            ua.git_repo = str(repo_path)
            ua.branch_from = args.branch_from
            cmd_update_image(ua)
            # Re-find the image after update
            image = find_latest_image(repo_path, branch)

    tag_str = ", ".join(image["tags"]) or image["id"]
    ssh_key = Path.home() / ".ssh" / "id_rsa"

    print(f"Starting container from {tag_str}")

    cmd = ["docker", "run", "--rm"]
    if sys.stdin.isatty():
        cmd += ["-it"]
    cmd += [
        "-v", "bazel-cache:/bazel-cache",
        "--network=host",
    ]

    if ssh_key.exists():
        cmd += ["-v", f"{ssh_key}:/root/.ssh/id_rsa:ro"]

    # Use the first tag if available, otherwise the image id
    image_ref = image["tags"][0] if image["tags"] else image["id"]
    cmd.append(image_ref)

    # Pass extra args to the container (forwarded to entrypoint as $@)
    extra = args.container_args if hasattr(args, "container_args") else []
    if extra and extra[0] == "--":
        extra = extra[1:]
    cmd += extra

    subprocess.run(cmd)


def cmd_list_images(args):
    repo_path = resolve_repo(args.git_repo) if args.git_repo else None
    branch_from = args.branch_from

    if branch_from:
        _, branch = parse_branch_ref(branch_from)
    else:
        branch = None

    images = find_images(repo_path, branch)
    if not images:
        print("No dkr images found.")
        return

    # Print header
    fmt = "{:<30} {:<20} {:<15} {:<12} {:<24} {:<8} {:<19}"
    print(fmt.format("TAG", "REPO", "BRANCH", "COMMIT", "CREATED", "TYPE", "IMAGE ID"))
    print("-" * 130)

    for img in images:
        labels = img["labels"]
        tag = ", ".join(img["tags"]) if img["tags"] else "<none>"
        print(fmt.format(
            tag[:30],
            labels.get("dkr.repo_name", "")[:20],
            labels.get("dkr.branch", "")[:15],
            labels.get("dkr.commit", "")[:12],
            labels.get("dkr.created_at", "")[:24],
            labels.get("dkr.type", "")[:8],
            img["id"][:19],
        ))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DISPATCH = {
    "create-image": cmd_create_image,
    "update-image": cmd_update_image,
    "start-image": cmd_start_image,
    "list-images": cmd_list_images,
}


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="dkr",
        description="Docker dev environment builder for large git repos",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ssh_key_default = "~/.ssh/id_rsa"

    # create-image
    p = sub.add_parser("create-image", help="Create a new Docker image with a git repo clone")
    p.add_argument("git_repo", nargs="?", default=None, help="Path to local git repo (default: cwd)")
    p.add_argument("branch_from", nargs="?", default=None, help="Branch/ref to checkout (default: HEAD)")
    p.add_argument("--ssh-key", default=ssh_key_default, help="SSH private key path (default: ~/.ssh/id_rsa)")

    # update-image
    p = sub.add_parser("update-image", help="Update an existing image with git fetch + rebase")
    p.add_argument("git_repo", nargs="?", default=None, help="Path to local git repo (default: cwd)")
    p.add_argument("branch_from", nargs="?", default=None, help="Branch/ref (default: HEAD)")
    p.add_argument("--ssh-key", default=ssh_key_default, help="SSH private key path (default: ~/.ssh/id_rsa)")

    # start-image
    p = sub.add_parser("start-image", help="Start a container from a dkr image")
    p.add_argument("git_repo", nargs="?", default=None, help="Path to local git repo (default: latest image)")
    p.add_argument("branch_from", nargs="?", default=None, help="Branch/ref (default: latest image)")
    p.add_argument("container_args", nargs=argparse.REMAINDER, default=[], help="Extra args passed to container (after --)")

    # list-images
    p = sub.add_parser("list-images", help="List dkr-managed Docker images")
    p.add_argument("git_repo", nargs="?", default=None, help="Path to local git repo (filter)")
    p.add_argument("branch_from", nargs="?", default=None, help="Branch/ref (filter)")

    return parser


def run_command(argv):
    """Parse *argv* and run the corresponding command. Callable from tests."""
    args = _build_parser().parse_args(argv)
    DISPATCH[args.command](args)


def main():
    run_command(sys.argv[1:])


if __name__ == "__main__":
    main()
