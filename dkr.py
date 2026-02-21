#!/usr/bin/env python3
"""dkr — Docker dev environment builder for large git repos."""

import argparse
import getpass
import json
import os
import platform
import random
import re
import urllib.request
import shutil
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


def resolve_head(repo_path):
    """Resolve HEAD to the actual branch name, or commit SHA if detached."""
    ref = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if ref == "HEAD":
        ref = git(repo_path, "rev-parse", "HEAD")
    return ref


def resolve_commit(repo_path, branch_from):
    """Resolve branch_from to a full commit SHA in the local repo."""
    return git(repo_path, "rev-parse", branch_from)


_ADJECTIVES = ["brave", "calm", "cool", "eager", "fast", "happy", "keen", "mild",
               "neat", "quick", "sharp", "warm", "bold", "dark", "fair", "glad",
               "lush", "pure", "safe", "wise"]
_NOUNS = ["panda", "tiger", "whale", "eagle", "falcon", "otter", "raven", "shark",
          "cobra", "heron", "maple", "cedar", "birch", "aspen", "coral", "frost",
          "ember", "drift", "storm"]


def random_name():
    """Generate a Docker-style adjective-noun name."""
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"


_CLAUDE_RELEASES_BUCKET = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"


def get_claude_latest_version():
    """Fetch the latest Claude Code version string. Falls back to 'latest' on error."""
    try:
        with urllib.request.urlopen(f"{_CLAUDE_RELEASES_BUCKET}/latest", timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return "latest"


def sanitize_tag(name):
    """Sanitize a string for use in a Docker tag."""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", name)


def image_tag(repo_path, branch):
    """Build the canonical Docker tag for a repo+branch."""
    repo_name = sanitize_tag(repo_path.name)
    branch_san = sanitize_tag(branch)
    return f"dkr:{repo_name}-{branch_san}"


def build_labels(repo_path, branch, commit, image_type, branch_from=None):
    """Return a dict of labels to apply to the image."""
    return {
        "dkr.repo_path": str(repo_path),
        "dkr.repo_name": repo_path.name,
        "dkr.branch": branch,
        "dkr.branch_from": branch_from or branch,
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
        if branch and branch not in (labels.get("dkr.branch"), labels.get("dkr.branch_from")):
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
# .dkr.conf support
# ---------------------------------------------------------------------------

DKR_CONF_DEFAULTS = {
    "base_image": "fedora:43",
    "packages": "",
    "volumes": "",
    "pre_clone": "",
    "post_clone": "",
}

REQUIRED_PACKAGES = ["git", "tmux", "openssh-clients", "curl"]


def load_dkr_conf(repo_path):
    """Read .dkr.conf from *repo_path* (branch should already be checked out).

    Returns a dict with keys: base_image, packages, pre_clone, post_clone.

    Format: top-level ``key = value`` lines, plus ``[pre_clone]`` and
    ``[post_clone]`` sections containing raw Dockerfile lines.
    """
    conf = dict(DKR_CONF_DEFAULTS)
    conf_file = repo_path / ".dkr.conf"
    if not conf_file.exists():
        return conf

    current_section = None
    section_lines = {"pre_clone": [], "post_clone": []}

    for line in conf_file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Section header
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            continue
        if current_section in section_lines:
            # Raw Dockerfile line — preserve as-is
            section_lines[current_section].append(line)
        elif "=" in stripped:
            key, _, value = stripped.partition("=")
            conf[key.strip()] = value.strip()

    conf["pre_clone"] = "\n".join(section_lines["pre_clone"])
    conf["post_clone"] = "\n".join(section_lines["post_clone"])

    return conf


def generate_dockerfile_create(conf):
    """Generate the Dockerfile.create content from a parsed .dkr.conf dict."""
    base_image = conf["base_image"]

    # Merge required + user packages
    user_pkgs = conf["packages"].split() if conf["packages"] else []
    all_pkgs = REQUIRED_PACKAGES + [p for p in user_pkgs if p not in REQUIRED_PACKAGES]
    pkg_list = " ".join(all_pkgs)

    pre_clone = conf["pre_clone"]
    post_clone = conf["post_clone"]

    lines = [
        "# syntax=docker/dockerfile:1",
        f"FROM {base_image}",
        "",
        "ENV LANG=C.UTF-8",
        "",
        "COPY .dkr-install-packages.sh /tmp/install-packages.sh",
        "RUN chmod +x /tmp/install-packages.sh && \\",
        f"    /tmp/install-packages.sh {pkg_list} && \\",
        "    rm /tmp/install-packages.sh",
        "",
        "ARG CLAUDE_VERSION=latest",
        "RUN curl -fsSL https://claude.ai/install.sh | bash",
        "ENV PATH=/root/.local/bin:$PATH",
        "",
        "ARG REPO_PATH",
        "ARG BRANCH",
        "ARG GIT_USER",
        "ARG HOST_ADDR=host.docker.internal",
        "",
        "RUN mkdir -p /root/.ssh && \\",
        "    ssh-keyscan -H ${HOST_ADDR} >> /root/.ssh/known_hosts 2>/dev/null || true",
        "",
    ]

    if pre_clone:
        lines += [pre_clone, ""]

    lines += [
        "RUN --mount=type=ssh \\",
        "    git clone ${GIT_USER}@${HOST_ADDR}:${REPO_PATH} /workspace",
        "",
        "RUN cd /workspace && git remote rename origin host && git checkout ${BRANCH}",
        "",
        "ENV DKR_BRANCH=${BRANCH}",
        "",
    ]

    if post_clone:
        lines += [post_clone, ""]

    lines += [
        "COPY .dkr-entrypoint.sh /entrypoint.sh",
        "RUN chmod +x /entrypoint.sh",
        "",
        "WORKDIR /workspace",
        'ENTRYPOINT ["/entrypoint.sh"]',
        "",
    ]

    return "\n".join(lines)


def generate_dockerfile_update(conf):
    """Generate the Dockerfile.update content from a parsed .dkr.conf dict."""
    post_clone = conf["post_clone"]

    lines = [
        "# syntax=docker/dockerfile:1",
        "ARG BASE_IMAGE=scratch",
        "FROM ${BASE_IMAGE}",
        "",
        "ARG GIT_USER",
        "ARG REPO_PATH",
        "ARG BRANCH",
        "ARG HOST_ADDR=host.docker.internal",
        "",
        "RUN --mount=type=ssh \\",
        "    cd /workspace && \\",
        "    git fetch ${GIT_USER}@${HOST_ADDR}:${REPO_PATH} ${BRANCH} && \\",
        "    git rebase FETCH_HEAD",
        "",
    ]

    if post_clone:
        lines += [post_clone, ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def resolve_ssh_key(args):
    """Return the absolute path to the SSH key, validated."""
    key = Path(args.ssh_key).expanduser().resolve()
    if not key.exists():
        sys.exit(f"Error: SSH key not found: {key}")
    return key


def _build_image(*, ssh_key, repo_path, checkout_branch, tag, labels,
                  dockerfile_generator, extra_build_args=None, message_prefix="Building"):
    """Shared build logic for create-image and update-image.

    Checks out *checkout_branch*, reads .dkr.conf, generates a Dockerfile
    via *dockerfile_generator(conf)*, builds, and cleans up.
    """
    user = getpass.getuser()

    original_ref = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if original_ref == "HEAD":
        original_ref = git(repo_path, "rev-parse", "HEAD")

    need_checkout = checkout_branch != "HEAD" and checkout_branch != original_ref
    if need_checkout:
        git(repo_path, "checkout", checkout_branch)

    dockerfile_path = repo_path / ".dkr-Dockerfile"
    entrypoint_path = repo_path / ".dkr-entrypoint.sh"
    install_pkg_path = repo_path / ".dkr-install-packages.sh"
    try:
        conf = load_dkr_conf(repo_path)
        dockerfile_path.write_text(dockerfile_generator(conf))

        # Copy support scripts into the build context
        shutil.copy2(SCRIPT_DIR / "entrypoint.sh", entrypoint_path)
        shutil.copy2(SCRIPT_DIR / "install-packages.sh", install_pkg_path)

        claude_ver = get_claude_latest_version()
        print(f"{message_prefix} image {tag} (claude {claude_ver})")

        cmd = [
            "docker", "build",
            "--ssh", f"default={ssh_key}",
            "--network=host",
            "--build-arg", f"REPO_PATH={repo_path}",
            "--build-arg", f"BRANCH={checkout_branch}",
            "--build-arg", f"GIT_USER={user}",
            "--build-arg", f"HOST_ADDR={HOST_ADDR}",
            "--build-arg", f"CLAUDE_VERSION={claude_ver}",
            "--tag", tag,
            "-f", str(dockerfile_path),
        ]
        for k, v in (extra_build_args or {}).items():
            cmd += ["--build-arg", f"{k}={v}"]
        cmd += label_args(labels) + [str(repo_path)]

        env = {**os.environ, "DOCKER_BUILDKIT": "1"}
        subprocess.run(cmd, check=True, env=env)
    finally:
        dockerfile_path.unlink(missing_ok=True)
        entrypoint_path.unlink(missing_ok=True)
        install_pkg_path.unlink(missing_ok=True)
        if need_checkout:
            git(repo_path, "checkout", original_ref)


def cmd_create_image(args):
    ssh_key = resolve_ssh_key(args)
    repo_path = resolve_repo(args.git_repo)
    branch_from = args.branch_from or resolve_head(repo_path)

    remote, branch = fetch_if_remote(repo_path, branch_from)
    commit = resolve_commit(repo_path, branch_from)
    checkout_branch = branch if remote else branch_from

    tag = image_tag(repo_path, checkout_branch)
    labels = build_labels(repo_path, checkout_branch, commit, "base", branch_from)

    _build_image(
        ssh_key=ssh_key, repo_path=repo_path, checkout_branch=checkout_branch,
        tag=tag, labels=labels, dockerfile_generator=generate_dockerfile_create,
        message_prefix=f"Building from {repo_path} @ {branch_from} ({commit[:12]}),",
    )
    print(f"Image built: {tag}")


def cmd_update_image(args):
    ssh_key = resolve_ssh_key(args)
    repo_path = resolve_repo(args.git_repo)
    branch_from = args.branch_from or resolve_head(repo_path)

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
    labels = build_labels(repo_path, checkout_branch, commit, "update", branch_from)

    _build_image(
        ssh_key=ssh_key, repo_path=repo_path, checkout_branch=checkout_branch,
        tag=tag, labels=labels, dockerfile_generator=generate_dockerfile_update,
        extra_build_args={"BASE_IMAGE": base_ref},
        message_prefix=f"Updating from {base_ref} -> {branch_from} ({commit[:12]}),",
    )
    print(f"Image updated: {tag}")


def staleness_check(image, repo_path):
    """Check if the image is stale vs its branch. Returns True if we should proceed."""
    labels = image["labels"]
    image_commit = labels.get("dkr.commit")
    branch = labels.get("dkr.branch_from") or labels.get("dkr.branch")
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

    # Load volumes from .dkr.conf
    conf = load_dkr_conf(repo_path) if repo_path else DKR_CONF_DEFAULTS

    print(f"Starting container from {tag_str}")

    cmd = ["docker", "run", "--rm"]
    if sys.stdin.isatty():
        cmd += ["-it"]
    cmd += ["--network=host"]

    for vol in conf["volumes"].split():
        cmd += ["-v", vol]

    if ssh_key.exists():
        cmd += ["-v", f"{ssh_key}:/root/.ssh/id_rsa:ro"]

    work_name = args.name if hasattr(args, "name") and args.name else random_name()
    cmd += ["-e", f"DKR_WORK_BRANCH={work_name}", "--hostname", work_name]

    agent = getattr(args, "agent", "claude")
    cmd += ["-e", f"DKR_AGENT={agent}"]

    # Mount Anthropic API key file read-only
    anthropic_key = getattr(args, "anthropic_key", None)
    if anthropic_key:
        key_path = Path(anthropic_key).expanduser().resolve()
        if not key_path.exists():
            sys.exit(f"Error: Anthropic API key file not found: {key_path}")
        cmd += ["-v", f"{key_path}:/run/secrets/anthropic_key:ro"]

    # Use the first tag if available, otherwise the image id
    image_ref = image["tags"][0] if image["tags"] else image["id"]
    cmd.append(image_ref)

    # Pass extra args to the container (forwarded to entrypoint as $@)
    cmd += getattr(args, "container_args", [])

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
    p.add_argument("--name", default=None, help="Working branch name (default: random adjective-noun)")
    p.add_argument("--anthropic-key", default=None,
                   help="Path to file containing Anthropic API key (mounted read-only into container)")
    p.add_argument("--agent", default="claude", choices=["claude", "codex", "opencode", "none"],
                   help="AI agent to run in first tmux window (default: claude)")

    # list-images
    p = sub.add_parser("list-images", help="List dkr-managed Docker images")
    p.add_argument("git_repo", nargs="?", default=None, help="Path to local git repo (filter)")
    p.add_argument("branch_from", nargs="?", default=None, help="Branch/ref (filter)")

    return parser


def run_command(argv):
    """Parse *argv* and run the corresponding command. Callable from tests."""
    # Split on '--' to separate dkr args from container args
    if "--" in argv:
        idx = argv.index("--")
        dkr_argv, container_args = argv[:idx], argv[idx + 1:]
    else:
        dkr_argv, container_args = argv, []

    args = _build_parser().parse_args(dkr_argv)
    args.container_args = container_args
    DISPATCH[args.command](args)


def main():
    run_command(sys.argv[1:])


if __name__ == "__main__":
    main()
