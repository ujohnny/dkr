#!/bin/bash
# Update working copy to latest before starting tmux

cd /workspace
BRANCH="${DKR_BRANCH:-master}"
WORK_BRANCH="${DKR_WORK_BRANCH:-work}"

if git fetch host "$BRANCH"; then
    git checkout -b "$WORK_BRANCH" FETCH_HEAD
    git branch --set-upstream-to="host/$BRANCH" "$WORK_BRANCH"
    git config "remote.host.push" "refs/heads/$WORK_BRANCH:refs/heads/$WORK_BRANCH"
    echo "Working copy updated to $(git rev-parse --short HEAD) on branch $WORK_BRANCH (tracking host/$BRANCH)"
    echo "git push will push to host $WORK_BRANCH"
else
    echo "Warning: failed to fetch from host, using image state"
fi

if [ $# -gt 0 ]; then
    exec "$@"
fi

AGENT="${DKR_AGENT:-claude}"
if [ "$AGENT" = "none" ]; then
    exec tmux new-session -s main
else
    exec tmux new-session -s main -n agent "$AGENT" \; new-window -n shell
fi
