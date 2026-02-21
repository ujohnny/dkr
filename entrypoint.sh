#!/bin/bash
# Update working copy to latest before starting tmux

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
