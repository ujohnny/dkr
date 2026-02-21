# dkr — Docker Dev Environment Builder

## Context

CLI tool (`dkr.py`) that creates Docker images containing large git repos by cloning over SSH from the host, supports incremental updates as new layers (avoiding re-cloning ~40GB repos), and starts containers with shared Bazel cache and tmux entrypoint.

## Project Structure

```
/Users/enovozhilov/triage/docker-builder/
├── dkr.py               # Main CLI tool (executable)
├── Dockerfile.create     # Dockerfile for create-image (full clone)
├── Dockerfile.update     # Dockerfile for update-image (fetch+rebase layer)
├── entrypoint.sh         # tmux entrypoint with fetch + random branch
├── spec.md               # This file
├── requirements.txt      # pytest
├── .venv/                # Virtual environment
└── tests/
    ├── conftest.py       # Git repo DSL, fixtures, Docker cleanup
    └── test_dkr.py       # Integration tests
```

## SSH Handling

- **Build time**: Docker BuildKit SSH forwarding (`--ssh default=<key_path>` + `RUN --mount=type=ssh`). The SSH private key path is passed directly (default `~/.ssh/id_rsa`, configurable via `--ssh-key` on create/update commands). No SSH agent required, no keys baked into image layers.
- **Run time**: SSH key mounted read-only into the container (`-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro`) so `git push` works inside the container.

## Host Address (macOS vs Linux)

The Docker build container needs to reach the host's SSH server. The address differs by platform:
- **macOS** (Docker Desktop): `host.docker.internal`
- **Linux**: `::1` (IPv6 localhost, `--network=host`)

`dkr.py` detects the platform via `platform.system()` and passes `HOST_ADDR` as a build arg. Dockerfiles default to `host.docker.internal`.

## Git Remote Naming

Inside the container, the clone remote is named `host` (not `origin`), so `origin` is free for the user's actual remote SSH repo.

## Image Labeling & Discovery

All images get Docker labels for later lookup:
- `dkr.repo_path` — absolute path to repo on host
- `dkr.repo_name` — short name (basename of repo path)
- `dkr.branch` — branch name
- `dkr.commit` — git commit SHA the image was built at
- `dkr.created_at` — ISO timestamp
- `dkr.type` — `base` or `update`

Tag format: `dkr:<repo_name>-<branch_sanitized>` (always points to latest for that repo+branch).

Image lookup: `docker images --filter label=dkr.repo_name` → `docker inspect` → filter by `dkr.repo_path` and `dkr.branch` → sort by `dkr.created_at` descending.

---

## Command: `create-image`

```
./dkr.py create-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

- `git_repo`: path to local repo (default: cwd). Resolved to absolute path.
- `branch_from`: branch/ref (default: HEAD). If contains a remote prefix (e.g. `origin/main`), runs `git fetch <remote> <branch>` locally first (fetching only that branch).

**Build flow:**
1. Validate SSH key exists, resolve repo path, validate it's a git repo.
2. If remote branch ref, do targeted local fetch.
3. Resolve commit SHA for the label.
4. `docker build` with `--ssh default=<key>`, `--network=host`, build args (`REPO_PATH`, `BRANCH`, `GIT_USER`, `HOST_ADDR`), labels, tag.
5. Dockerfile: install git/tmux/openssh, `ssh-keyscan`, `git clone` via SSH mount, rename remote to `host`, checkout branch, set `DKR_BRANCH` env, write `.bazelrc`, copy entrypoint.

**Dockerfile.create:**
```dockerfile
# syntax=docker/dockerfile:1
FROM fedora:43
RUN dnf install -y git tmux openssh-clients && dnf clean all
ARG REPO_PATH
ARG BRANCH
ARG GIT_USER
ARG HOST_ADDR=host.docker.internal
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -H ${HOST_ADDR} >> /root/.ssh/known_hosts 2>/dev/null || true
RUN --mount=type=ssh \
    git clone ${GIT_USER}@${HOST_ADDR}:${REPO_PATH} /workspace
RUN cd /workspace && git remote rename origin host && git checkout ${BRANCH}
ENV DKR_BRANCH=${BRANCH}
RUN echo "build --disk_cache=/bazel-cache" >> /root/.bazelrc
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
```

---

## Command: `update-image`

```
./dkr.py update-image [git_repo] [branch_from] [--ssh-key ~/.ssh/id_rsa]
```

Same argument semantics as `create-image`. Finds the most recent existing image for this repo+branch combo via label filtering, then builds a thin layer on top with `git fetch + git rebase`.

**Build flow:**
1. Resolve args, find latest matching image (error if none).
2. `docker build` with `--ssh default=<key>`, `FROM <base_image>`, fetch+rebase.
3. Tag result, overwriting the previous tag.

**Dockerfile.update:**
```dockerfile
# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG GIT_USER
ARG REPO_PATH
ARG BRANCH
ARG HOST_ADDR=host.docker.internal
RUN --mount=type=ssh \
    cd /workspace && \
    git fetch ${GIT_USER}@${HOST_ADDR}:${REPO_PATH} ${BRANCH} && \
    git rebase FETCH_HEAD
```

---

## Command: `start-image`

```
./dkr.py start-image [git_repo] [branch_from]
```

No args = find the most recently built `dkr:*` image overall. With args = find the latest image for that repo+branch.

**Staleness check (before starting):**
1. Read `dkr.commit` label from the image.
2. In the local repo, `git rev-list <image_commit>..origin/master --count`.
3. If >50 commits behind, prompt:
   ```
   Warning: image dkr:foo-master is 127 commits behind origin/master.
   Do you want to update the image before starting? [y/N]
   ```
   - Yes → run `update-image` automatically, then start the updated image.
   - No → proceed with stale image.
4. If commit not an ancestor (force-push), warn and suggest recreating.

**Run flow:**
1. Locate image, run staleness check.
2. `docker run -it --rm`:
   - `-v bazel-cache:/bazel-cache` — named volume shared across all containers
   - `-v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro` — SSH key for push/fetch
   - `--network=host` — reach host SSH
3. Entrypoint runs, then tmux.

---

## Entrypoint Behavior

On container start, `entrypoint.sh`:
1. Fetches the matching branch from `host` remote: `git fetch host $DKR_BRANCH`
2. Generates a random Docker-style branch name from adjective-noun pairs (e.g. `brave-panda`, `cool-falcon`)
3. Checks out that branch: `git checkout -b <random_name> FETCH_HEAD`
4. Sets upstream: `git branch --set-upstream-to=host/<branch>`
5. Configures push refspec: `git config remote.host.push refs/heads/<random_name>:refs/heads/$HOSTNAME/<random_name>` — so `git push` creates `$HOSTNAME/<random_name>` on the host repo
6. If args are provided (`docker run <image> <cmd>`), runs `<cmd>` instead of tmux
7. Otherwise: `exec tmux new-session -s main`

```bash
#!/bin/bash
ADJECTIVES=(brave calm cool eager fast happy keen mild neat quick sharp warm bold dark fair glad keen lush pure safe wise)
NOUNS=(panda tiger whale eagle falcon otter raven shark cobra heron maple cedar birch aspen coral frost ember drift storm)

random_name() {
    local adj=${ADJECTIVES[$((RANDOM % ${#ADJECTIVES[@]}))]}
    local noun=${NOUNS[$((RANDOM % ${#NOUNS[@]}))]}
    echo "${adj}-${noun}"
}

cd /workspace
BRANCH="${DKR_BRANCH:-master}"
if git fetch host "$BRANCH"; then
    WORK_BRANCH=$(random_name)
    git checkout -b "$WORK_BRANCH" FETCH_HEAD
    git branch --set-upstream-to="host/$BRANCH" "$WORK_BRANCH"
    git config "remote.host.push" "refs/heads/$WORK_BRANCH:refs/heads/$HOSTNAME/$WORK_BRANCH"
    echo "Working copy updated to $(git rev-parse --short HEAD) on branch $WORK_BRANCH (tracking host/$BRANCH)"
    echo "git push will push to host $HOSTNAME/$WORK_BRANCH"
else
    echo "Warning: failed to fetch from host, using image state"
fi
if [ $# -gt 0 ]; then
    exec "$@"
else
    exec tmux new-session -s main
fi
```

---

## Command: `list-images`

```
./dkr.py list-images [git_repo] [branch_from]
```

Both args optional. Lists all `dkr`-managed images, filtered by repo and/or branch if provided.

**Output:** table with columns: tag, repo name, branch, commit (short SHA), created at, type (base/update), image ID.

---

## Implementation Details

- **CLI framework**: `argparse` with subcommands, no external dependencies.
- **Docker calls**: `subprocess.run(["docker", ...])` — no Docker SDK.
- **Git calls**: `subprocess.run(["git", "-C", repo_path, ...])`.
- **Branch sanitization**: replace `/` with `-`, strip special chars.
- **DOCKER_BUILDKIT=1**: set in environment for BuildKit features.
- **`run_command(argv)`**: exposed for calling commands from tests without subprocess.

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

### Helpers

- `dkr(*args)` — calls `run_command()` directly (no subprocess).
- `docker_run_cmd(image_ref, *cmd)` — runs a command in a container with overridden entrypoint, returns stdout.
- `cleanup_dkr_images` — autouse fixture, snapshots image IDs before test, removes only new ones after.

### Test Cases

| Test | What it verifies |
|------|-----------------|
| `TestCreateImage::test_basic` | Image labels, file content, commit count, `.bazelrc` |
| `TestCreateImage::test_specific_branch` | Feature branch files present, master-only files absent |
| `TestStartImage::test_random_branch_on_start` | Entrypoint creates adjective-noun branch tracking `host/<branch>` |
| `TestUpdateImage::test_update_adds_new_commits` | Update layer has new commit, `dkr.type=update` label |
| `TestListImages::test_lists_created_images` | Both branches appear in list output |
| `TestStaleness::test_detects_stale_image` | 60 commits behind detected via `rev-list --count` |
