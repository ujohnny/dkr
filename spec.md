# dkr — Docker Dev Environment Builder

## Context

CLI tool (`dkr.py`) that creates Docker images containing large git repos by cloning over SSH from the host, supports incremental updates as new layers (avoiding re-cloning ~40GB repos), and starts containers with shared Bazel cache and tmux entrypoint.

## Project Structure

```
/Users/enovozhilov/triage/docker-builder/
├── dkr.py               # Main CLI tool (executable)
├── entrypoint.sh         # tmux entrypoint with fetch + random branch
├── install-packages.sh   # Runtime package manager detection script
├── spec.md               # This file
├── requirements.txt      # pytest
├── .venv/                # Virtual environment
└── tests/
    ├── conftest.py       # Git repo DSL, fixtures, Docker cleanup
    ├── test_create_image.py
    ├── test_update_image.py
    ├── test_start_image.py
    ├── test_list_images.py
    └── test_staleness.py
```

Dockerfiles are generated dynamically at build time — no static Dockerfile files.

## .dkr.conf

Repo owners place a `.dkr.conf` in their repo root to configure the Docker image. The file is read from the target branch (dkr checks out the branch before building).

```ini
base_image = ubuntu:24.04
packages = bazel clang cmake

[pre_clone]
RUN curl -fsSL https://bazel.build/bazel-release.pub.gpg | gpg --dearmor > /usr/share/keyrings/bazel.gpg

[post_clone]
RUN cd /workspace && ./setup.sh
```

- **`base_image`** (default: `fedora:43`) — the FROM image
- **`packages`** (default: none) — space-separated extra packages. `git tmux openssh-clients` are always included.
- **`[pre_clone]`** — raw Dockerfile lines inserted before the git clone step
- **`[post_clone]`** — raw Dockerfile lines inserted after clone + checkout. Applied to both create-image and update-image.

Package installation uses `install-packages.sh` which detects the package manager at runtime (apt-get, dnf, yum, apk, pacman, zypper).

## SSH Handling

- **Build time**: Docker BuildKit SSH forwarding (`--ssh default=<key_path>` + `RUN --mount=type=ssh`). The SSH private key path is passed directly (default `~/.ssh/id_rsa`, configurable via `--ssh-key` on create/update commands). No SSH agent required, no keys baked into image layers.
- **Run time**: SSH key mounted read-only into the container (`-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro`) so `git push` works inside the container.

## Host Address (macOS vs Linux)

The Docker build container needs to reach the host's SSH server:
- **macOS** (Docker Desktop): `host.docker.internal`
- **Linux**: `::1` (IPv6 localhost, `--network=host`)

Detected via `platform.system()` and passed as `HOST_ADDR` build arg.

## Git Remote Naming

Inside the container, the clone remote is named `host` (not `origin`), so `origin` is free for the user's actual remote SSH repo.

## Branch Ref Parsing

`parse_branch_ref(branch_from, repo_path)` checks if the prefix before `/` is an actual git remote name in the repo. This distinguishes `origin/main` (remote ref) from `enovozhilov/feature-example` (local branch with `/` in name).

## Image Labeling & Discovery

All images get Docker labels:
- `dkr.repo_path` — absolute path to repo on host
- `dkr.repo_name` — short name (basename of repo path)
- `dkr.branch` — resolved branch name (e.g. `master`)
- `dkr.branch_from` — original ref as user typed (e.g. `origin/master` or `master`)
- `dkr.commit` — git commit SHA the image was built at
- `dkr.created_at` — ISO timestamp
- `dkr.type` — `base` or `update`

Tag format: `dkr:<repo_name>-<branch_sanitized>`.

Image lookup via `find_images(repo_path, branch)` matches against both `dkr.branch` and `dkr.branch_from`, so searching by `"space/main"` or `"main"` both find the image.

---

## Command: `create-image`

```
./dkr.py create-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

- `git_repo`: path to local repo (default: cwd).
- `branch_from`: branch/ref (default: HEAD). If references a remote, runs targeted `git fetch` first.

**Build flow:**
1. Validate SSH key, resolve repo, fetch if remote ref.
2. Checkout target branch in local repo.
3. Read `.dkr.conf`, generate Dockerfile dynamically.
4. Copy `entrypoint.sh` and `install-packages.sh` into build context (repo dir).
5. `docker build` with `--ssh default=<key>`, `--network=host`, build args, labels, tag.
6. Clean up temp files, restore original branch.

Shared build logic lives in `_build_image()` helper used by both create and update.

---

## Command: `update-image`

```
./dkr.py update-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

Same argument semantics as `create-image`. Finds the most recent existing image for this repo+branch, builds a thin layer on top with `git fetch + git rebase` plus any `[post_clone]` steps from `.dkr.conf`.

---

## Command: `start-image`

```
./dkr.py start-image [git_repo] [branch_from] [--name <branch_name>] [--anthropic-key <path>] [-- <cmd>]
```

- No args = find the most recently built `dkr:*` image. With args = find latest for that repo+branch.
- `--name <branch_name>` — set the working branch name instead of random adjective-noun (passed as `DKR_WORK_BRANCH` env var).
- `--anthropic-key <path>` — path to a file containing the Anthropic API key. Mounted read-only at `/run/secrets/anthropic_key`. The entrypoint creates `/root/.claude/settings.json` with `apiKeyHelper` that reads the key via `cat`.
- Extra args after `--` are passed to the container (forwarded to entrypoint as `$@`).

**Staleness check (before starting):**
1. Read `dkr.branch_from` label (falls back to `dkr.branch`).
2. `git rev-list --count <image_commit>..<branch>` in local repo.
3. If >50 commits behind, prompt to update or proceed.
4. If commit not an ancestor (force-push), warn and suggest recreating.

**Run flow:**
1. Locate image, run staleness check.
2. `docker run --rm` (`-it` only when stdin is a TTY):
   - `-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro` — SSH key for push/fetch
   - `-v <anthropic_key>:/run/secrets/anthropic_key:ro` — API key file (if `--anthropic-key` provided)
   - `--network=host` — reach host SSH
3. Entrypoint runs, then tmux (or `<cmd>` if provided).

---

## Entrypoint Behavior

On container start, `entrypoint.sh`:
1. Creates `/root/.claude.json` with `/workspace` project trust
2. If `/run/secrets/anthropic_key` exists, creates `/root/.claude/settings.json` with `apiKeyHelper` pointing to it
3. Fetches the matching branch from `host` remote: `git fetch host $DKR_BRANCH`
4. Uses `DKR_WORK_BRANCH` if set (from `--name`), otherwise generates a random adjective-noun name (e.g. `brave-panda`)
5. Checks out that branch: `git checkout -b <random_name> FETCH_HEAD`
6. Sets upstream: `git branch --set-upstream-to=host/<branch>`
7. Configures push refspec: `remote.host.push refs/heads/<name>:refs/heads/$HOSTNAME/<name>` — so `git push` creates `$HOSTNAME/<random_name>` on the host repo
8. If args provided (`docker run <image> <cmd>`), runs `<cmd>` instead of tmux
9. Otherwise: `exec tmux new-session -s main`

---

## Command: `list-images`

```
./dkr.py list-images [git_repo] [branch_from]
```

Both args optional. Lists all `dkr`-managed images, filtered by repo and/or branch.

**Output:** table with columns: tag, repo name, branch, commit (short SHA), created at, type (base/update), image ID.

---

## Implementation Details

- **CLI framework**: `argparse` with subcommands, no external dependencies.
- **Docker calls**: `subprocess.run(["docker", ...])` — no Docker SDK.
- **Git calls**: `subprocess.run(["git", "-C", repo_path, ...])`.
- **Branch sanitization**: replace `/` with `-`, strip special chars.
- **DOCKER_BUILDKIT=1**: set in environment for BuildKit features.
- **`run_command(argv)`**: exposed for calling commands from tests without subprocess.
- **`_build_image()`**: shared helper for create/update — handles branch checkout, config loading, Dockerfile generation, build, and cleanup.

---

## Testing

### Test Framework

- **pytest** with integration tests requiring Docker + SSH to host.
- Run: `.venv/bin/python -m pytest tests/ -v`

### Git Repo DSL (`tests/conftest.py`)

Declarative fixture to create repos with specific branch/commit structures:

```python
repo, commits = make_repo({
    "master": [
        {"message": "initial", "files": {"README.md": "hello"}},
        {"message": "second", "files": {"src/main.py": "print('hi')"}},
    ],
    "feature": {
        "from": "master:0",   # branch from commit index 0 on master
        "commits": [
            {"message": "feature work", "files": {"feature.py": "x = 1"}},
        ],
    },
})
```

- Plain list = commits on branch (first branch starts from `git init`).
- Dict with `"from": "<branch>:<index>"` = branch off that commit.
- Returns `(repo_path, branch_commits_dict)`.

### Clone Repo Fixture

```python
local = clone_repo(remote_path)
```

Creates a `git clone` of an existing repo. The clone has `origin` pointing to the source, so `origin/master` etc. are available for remote tracking tests.

### Helpers

- `dkr(*args)` — calls `run_command()` directly (no subprocess).
- `docker_run_cmd(image_ref, *cmd)` — runs a command in a container with overridden entrypoint, returns stdout.
- `cleanup_dkr_images` — autouse fixture, snapshots image IDs before test, removes only new ones after.

### Test Cases

| Test | What it verifies |
|------|-----------------|
| `TestCreateImage::test_basic` | Image labels, file content, commit count, `.bazelrc` |
| `TestCreateImage::test_specific_branch` | Feature branch files present, master-only files absent |
| `TestCreateImage::test_dkr_conf` | `.dkr.conf` `[post_clone]` step executes |
| `TestCreateImage::test_remote_ref` | Create from `space/main` remote, labels store `branch_from` |
| `TestUpdateImage::test_update_adds_new_commits` | Update layer has new commit, `dkr.type=update` label |
| `TestStartImage::test_random_branch_on_start` | Entrypoint creates adjective-noun branch tracking `host/<branch>` |
| `TestStartImage::test_custom_branch_name` | `--name my-feature` sets the working branch name |
| `TestStartImage::test_workspace_trusted` | Entrypoint creates `.claude.json` with `/workspace` trust |
| `TestStartImage::test_anthropic_key_mounted` | `--anthropic-key` mounts key file, creates `settings.json` with `apiKeyHelper` |
| `TestListImages::test_lists_created_images` | Both branches appear in list output |
| `TestStaleness::test_stale_image_warns_and_prompts_update` | Warning + `"update"` return on yes |
| `TestStaleness::test_stale_image_continues_on_decline` | Proceeds on no |
| `TestStaleness::test_stale_remote_tracking` | Staleness via `origin/master` remote tracking |
