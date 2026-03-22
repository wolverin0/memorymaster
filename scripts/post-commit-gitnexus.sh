#!/bin/bash
# Post-commit hook: re-analyze project with GitNexus after each commit
# Install: cp scripts/post-commit-gitnexus.sh .git/hooks/post-commit
npx -y gitnexus analyze --quiet 2>/dev/null &
