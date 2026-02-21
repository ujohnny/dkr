#!/bin/sh
set -e

if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y "$@"
    rm -rf /var/lib/apt/lists/*
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y "$@"
    dnf clean all
elif command -v yum >/dev/null 2>&1; then
    yum install -y "$@"
    yum clean all
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache "$@"
elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm "$@"
elif command -v zypper >/dev/null 2>&1; then
    zypper install -y "$@"
else
    echo "Error: no supported package manager found" >&2
    exit 1
fi
