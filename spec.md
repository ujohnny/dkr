# dkr — Docker Dev Environment Builder

## Context

CLI tool (`dkr.py`) that creates Docker images containing large git repos by cloning over SSH from the host, supports incremental updates as new layers (avoiding re-cloning ~40GB repos), and starts containers with an AI coding agent (Claude by default).

## Project Structure

```
├── dkr                  # Polyglot wrapper (Unix sh + Windows cmd)
├── dkr.py               # Main CLI tool (executable)
├── entrypoint.sh         # Container entrypoint: fetch, branch setup, agent launch
├── install-packages.sh   # Runtime package manager detection script
├── CLAUDE.md             # Points to this spec
├── README.md             # Quick start guide
├── spec.md               # This file
├── requirements.txt      # pytest
└── tests/
    ├── conftest.py       # Git repo DSL, fixtures, Docker cleanup
    ├── test_create_image.py
    ├── test_update_image.py
    ├── test_start_image.py
    ├── test_list_images.py
    └── test_staleness.py
```

Dockerfiles are generated dynamically at build time — no static Dockerfile files. Build context is a temp directory (not the repo) to avoid sending large repos to the Docker daemon.

## .dkr.conf

Repo owners place a `.dkr.conf` in their repo root to configure the Docker image. The file is read from the target branch (dkr checks out the branch before building).

```ini
base_image = ubuntu:24.04
packages = bazel clang cmake
volumes = bazel-cache:/bazel-cache

[pre_clone]
RUN curl -fsSL https://bazel.build/bazel-release.pub.gpg | gpg --dearmor > /usr/share/keyrings/bazel.gpg

[post_clone]
RUN cd /workspace && ./setup.sh
RUN echo "build --disk_cache=/bazel-cache" >> /root/.bazelrc
```

- **`base_image`** (default: `fedora:43`) — the FROM image
- **`packages`** (default: none) — space-separated extra packages. `git openssh-clients curl jq` are always included.
- **`volumes`** (default: none) — space-separated volume mounts for `start-container` (e.g. `bazel-cache:/bazel-cache`, `/host/path:/container/path`)
- **`[pre_clone]`** — raw Dockerfile lines inserted before the git clone step
- **`[post_clone]`** — raw Dockerfile lines inserted after clone + checkout. Applied to both create-image and update-image.

Package installation uses `install-packages.sh` which detects the package manager at runtime (apt-get, dnf, yum, apk, pacman, zypper).

## Claude Code Installation

Claude Code is installed into every image via the official installer (`curl -fsSL https://claude.ai/install.sh | bash`). The latest version is fetched from the GCS releases bucket before each build — if a new version is available, Docker cache is busted for the install step via a `CLAUDE_VERSION` build arg.

`ENV PATH=/root/.local/bin:$PATH` is set so `claude` is on PATH.

## SSH Handling

- **Build time**: Docker BuildKit SSH forwarding (`--ssh default=<key_path>` + `RUN --mount=type=ssh`). The SSH private key path is passed directly (default `~/.ssh/id_rsa`, configurable via `--ssh-key`). No SSH agent required, no keys baked into image layers.
- **Run time**: SSH key mounted read-only (`-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro`) so `git push` works inside the container.

## Host Address (macOS vs Linux)

- **macOS** (Docker Desktop): `host.docker.internal`
- **Linux**: `::1` (IPv6 localhost, `--network=host`)

Detected via `platform.system()` and passed as `HOST_ADDR` build arg.

## Git Remote Naming

Inside the container, the clone remote is named `host` (not `origin`), so `origin` is free for the user's actual remote SSH repo.

## Branch Ref Parsing

`parse_branch_ref(branch_from, repo_path)` checks if the prefix before `/` is an actual git remote name in the repo. This distinguishes `origin/main` (remote ref) from `enovozhilov/feature-example` (local branch with `/`).

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

`find_images(repo_path, branch)` matches against both `dkr.branch` and `dkr.branch_from`.

---

## Command: `create-image` (alias: `ci`)

```
dkr create-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

- `git_repo`: path to local repo (default: cwd).
- `branch_from`: branch/ref (default: current branch). If HEAD, resolves to actual branch name (or SHA if detached). If references a remote, runs targeted `git fetch` first.

**Build flow:**
1. Validate SSH key, resolve repo, resolve HEAD, fetch if remote ref.
2. Checkout target branch in local repo.
3. Read `.dkr.conf`, generate Dockerfile dynamically.
4. Build in a temp directory context (entrypoint.sh + install-packages.sh + generated Dockerfile).
5. Restore original branch.

---

## Command: `update-image` (alias: `ui`)

```
dkr update-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

Same argument semantics as `create-image`. Finds the most recent existing image for this repo+branch, builds a thin layer on top with `git fetch + git rebase` plus any `[post_clone]` steps from `.dkr.conf`.

---

## Command: `start-container` (alias: `sc`)

```
dkr start-container [git_repo] [branch_from] [--name <name>] [--agent <agent>] [--anthropic-key <path>] [-- <cmd>]
```

- No args = find the most recently built `dkr:*` image. With args = find latest for that repo+branch.
- `--name <name>` — working branch name and container hostname (default: random adjective-noun like `brave-panda`).
- `--agent <claude|codex|opencode|none>` — AI agent to run (default: `claude`). `none` drops to bash.
- `--anthropic-key <path>` — file containing the Anthropic API key, mounted read-only at `/run/secrets/anthropic_key`.
- Extra args after `--` are passed to the container (forwarded to entrypoint as `$@`).

**Staleness check (before starting):**
1. Read `dkr.branch_from` label (falls back to `dkr.branch`).
2. `git rev-list --count <image_commit>..<branch>` in local repo.
3. If >50 commits behind, prompt to update or proceed.

**Run flow:**
1. Locate image, run staleness check.
2. `docker run --rm -t` (`-i` added when stdin is a TTY):
   - Volumes from `.dkr.conf`
   - `-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro` — SSH key
   - `-v <anthropic_key>:/run/secrets/anthropic_key:ro` — API key (if provided)
   - `--network=host`, `--hostname <work_name>`
3. Entrypoint runs, then agent (or `<cmd>` if provided).

---

## Entrypoint Behavior

On container start, `entrypoint.sh`:

1. Creates `/root/.claude.json` with project trust for `/workspace`, onboarding skipped.
2. Creates `/root/.claude/settings.json` with `"model": "opus[1m]"`. If `/run/secrets/anthropic_key` exists, adds `"apiKeyHelper": "cat /run/secrets/anthropic_key"`.
3. Fetches the matching branch from `host` remote: `git fetch host $DKR_BRANCH`
4. Creates working branch from `DKR_WORK_BRANCH` env var: `git checkout -b <name> FETCH_HEAD`
5. Sets upstream to `host/<branch>` and push refspec to `refs/heads/<name>:refs/heads/<name>` — so `git push host` pushes to the work branch name directly.
6. If args provided, runs them instead of the agent (`exec "$@"`).
7. Otherwise: runs the agent specified by `DKR_AGENT` (default: `claude`), or `bash` if `none`.

---

## Command: `list-images` (alias: `ls`)

```
dkr list-images [git_repo] [branch_from]
```

Both args optional. Lists all `dkr`-managed images, filtered by repo and/or branch.

**Output:** table with columns: tag, repo name, branch, commit (short SHA), created at, type (base/update), image ID.

---

## Implementation Details

- **CLI framework**: `argparse` with subcommands and aliases (ci, ui, sc, ls). No external dependencies.
- **Docker calls**: `subprocess.run(["docker", ...])` — no Docker SDK.
- **Git calls**: `subprocess.run(["git", "-C", repo_path, ...])`.
- **Branch sanitization**: replace `/` with `-`, strip special chars.
- **DOCKER_BUILDKIT=1**: set in environment for BuildKit features.
- **`run_command(argv)`**: exposed for calling commands from tests without subprocess. Splits `--` to separate dkr args from container args.
- **`_build_image()`**: shared helper for create/update — handles branch checkout, config loading, Dockerfile generation, temp build context, and cleanup.
- **`random_name()`**: generates adjective-noun names (e.g. `brave-panda`) for work branches and container hostnames.
- **`get_claude_latest_version()`**: fetches latest version from GCS bucket for cache busting.
- **`ENV LANG=C.UTF-8`**: set in generated Dockerfile for correct Unicode rendering.

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
        "from": "master:0",
        "commits": [
            {"message": "feature work", "files": {"feature.py": "x = 1"}},
        ],
    },
})
```

### Clone Repo Fixture

```python
local = clone_repo(remote_path)
```

Creates a `git clone` with `origin` pointing to the source for remote tracking tests.

### Helpers

- `dkr(*args)` — calls `run_command()` directly (no subprocess).
- `docker_run_cmd(image_ref, *cmd)` — runs a command in a container with overridden entrypoint, returns stdout.
- `cleanup_dkr_images` — autouse fixture, snapshots image IDs before test, removes only new ones after.

### Test Cases

| Test | What it verifies |
|------|-----------------|
| `TestCreateImage::test_basic` | Image labels, file content, commit count |
| `TestCreateImage::test_specific_branch` | Feature branch files present, master-only files absent |
| `TestCreateImage::test_dkr_conf` | `.dkr.conf` `[post_clone]` step executes |
| `TestCreateImage::test_remote_ref` | Create from `space/main` remote, labels store `branch_from` |
| `TestCreateImage::test_head_resolves_to_branch` | No branch arg resolves HEAD to actual branch name |
| `TestCreateImage::test_head_detached_resolves_to_sha` | Detached HEAD uses commit SHA as branch label |
| `TestUpdateImage::test_update_adds_new_commits` | Update layer has new commit, `dkr.type=update` label |
| `TestStartImage::test_random_branch_on_start` | Entrypoint creates adjective-noun branch tracking `host/<branch>` |
| `TestStartImage::test_custom_branch_name` | `--name my-feature` sets the working branch name |
| `TestStartImage::test_volumes_from_dkr_conf` | Volumes from `.dkr.conf` are mounted in container |
| `TestStartImage::test_workspace_trusted` | `/root/.claude.json` has `/workspace` trust |
| `TestStartImage::test_anthropic_key_mounted` | `--anthropic-key` mounts key, creates `settings.json` |
| `TestStartImage::test_git_push_to_host` | Commit + push from container creates branch on host |
| `TestListImages::test_lists_created_images` | Both branches appear in list output |
| `TestStaleness::test_stale_image_warns_and_prompts_update` | Warning + `"update"` return on yes |
| `TestStaleness::test_stale_image_continues_on_decline` | Proceeds on no |
| `TestStaleness::test_stale_remote_tracking` | Staleness via `origin/master` remote tracking |
