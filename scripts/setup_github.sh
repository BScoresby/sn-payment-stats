#!/usr/bin/env bash
set -euo pipefail

repo_name="${1:-sn-payment-stats}"

if ! command -v git >/dev/null 2>&1; then
  echo "Git is required. Install it with: sudo apt install -y git" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI is required. Install it with: sudo apt install -y gh" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

if ! gh auth status >/dev/null 2>&1; then
  echo "Opening GitHub authentication..."
  gh auth login
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "This folder already has an origin remote; no repository was created." >&2
  echo "Existing origin: $(git remote get-url origin)" >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  git init -b main
fi

github_login="$(gh api user --jq .login)"
if ! git config user.name >/dev/null; then
  git config user.name "$github_login"
fi
if ! git config user.email >/dev/null; then
  git config user.email "${github_login}@users.noreply.github.com"
fi

git add .
if ! git diff --cached --quiet; then
  git commit -m "Add Stacker News stats collector"
fi
git branch -M main

echo "Creating public GitHub repository: ${github_login}/${repo_name}"
gh repo create "$repo_name" --public --source=. --remote=origin --push

echo
echo "Setup complete. The collector will run automatically every day."
echo "You can also run it manually with: python3 scripts/run_pipeline.py"
