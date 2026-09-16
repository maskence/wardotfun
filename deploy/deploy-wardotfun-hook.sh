#!/usr/bin/env bash
set -euo pipefail

checkout=/home/ubuntu/wardotfun-repo
application_root=/home/ubuntu/wardotfun

cd "$checkout"
GIT_SSH_COMMAND="ssh -i /home/ubuntu/.ssh/wardotfun_github -o IdentitiesOnly=yes" git fetch origin
git checkout --force origin/master
tar --exclude=.git --exclude=.venv --exclude=env --exclude='*/__pycache__' -cf - . | tar -xf - -C "$application_root"
exec "$application_root/deploy/deploy-production.sh"
