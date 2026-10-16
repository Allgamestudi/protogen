#!/usr/bin/env bash
# Turn an installed copy of the project (copied to the Pi by hand) into a git
# clone, so that "Check for updates" in the web UI works.
#
#   bash scripts/enable_updates.sh https://github.com/<user>/<repo>.git [branch]
#
# Run as the user who owns the folder (not root). Run once per device.
#
# What happens to the files:
#   * a backup of the whole folder (without virtualenvs) is written to
#     ~/protogen-backup-<date>.tar.gz first
#   * files that the repository tracks are replaced by the repository version
#   * everything else stays: saves/ (settings), uploads, compiled tools, venv
set -eu

URL="${1:?usage: enable_updates.sh <repository url> [branch]}"
BRANCH="${2:-main}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" = "0" ]; then
  echo "run as the owner of $REPO, without sudo" >&2
  exit 1
fi
command -v git >/dev/null 2>&1 || { echo "git is missing: sudo apt-get install -y git" >&2; exit 1; }

cd "$REPO"
BACKUP="$HOME/protogen-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
echo "backup -> $BACKUP"
tar --exclude='./venv' --exclude='./.venv' --exclude='./.git' -czf "$BACKUP" .

[ -d .git ] || git init -q -b "$BRANCH"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$URL"
else
  git remote add origin "$URL"
fi

echo "downloading $URL ($BRANCH)"
git fetch --prune origin
git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
  || { echo "branch $BRANCH not found in $URL" >&2; exit 1; }

# Tracked files -> repository version; untracked files are left alone.
git checkout -q -B "$BRANCH"
git reset -q --hard "origin/$BRANCH"
git branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH"

echo
echo "this copy is now at $(git log -1 --format='%h  %ad  %s' --date=short)"
echo "restart to run it:  sudo systemctl restart protogen"
