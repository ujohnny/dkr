# dkr

Docker dev environments from local git repos. Clones over SSH, supports incremental updates, shared Bazel cache, and tmux.

## Prerequisites

- Docker with BuildKit
- SSH server running on the host (`ssh localhost` must work)
- SSH key at `~/.ssh/id_rsa` (or pass `--ssh-key`)

## Quick Start

Run from inside your repo:

### Create an image

```bash
dkr.py create-image
```

Clones the current repo at HEAD into a Docker image via SSH. First build pulls the base image and installs packages — subsequent builds reuse cached layers.

If the repo contains a `.dkr.conf`, it controls the base image and extra setup steps (see `spec.md`).

### Update an image

```bash
dkr.py update-image
```

Adds a thin layer on top of the existing image with `git fetch + rebase`. Much faster than recreating when the repo is large.

### Start a container

```bash
dkr.py start-image
```

Starts a container with tmux. The entrypoint fetches the latest code and creates a working branch (random name like `brave-panda`, or use `--name my-feature`).

Inside the container:
- `/workspace` — the repo checkout
- `git push` pushes to `$HOSTNAME/<branch>` on the host
- Bazel cache is shared across all containers via a named volume

If the image is more than 50 commits behind, you'll be prompted to update before starting.

### Other commands

```bash
dkr.py list-images       # list all dkr images
```

All commands accept optional `[repo_path] [branch]` to override the defaults (cwd and HEAD).
