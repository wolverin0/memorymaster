#!/bin/bash
# Post-commit hook: re-analyze project with GitNexus after each commit
# Install: cp scripts/post-commit-gitnexus.sh .git/hooks/post-commit
#
# Wrapped in a subshell that records last_indexed_commit + last_indexed_at
# into .gitnexus/meta.json after analyze completes. GitNexus itself doesn't
# write these fields, so without this wrapper there's no way to tell the
# index is stale without running git diff against the filesystem.
{
    HEAD_COMMIT=$(git rev-parse HEAD 2>/dev/null)
    if npx -y gitnexus analyze --quiet >/tmp/gitnexus-postcommit.log 2>&1; then
        META=".gitnexus/meta.json"
        if [ -f "$META" ] && command -v python >/dev/null 2>&1; then
            python -c "
import json, sys, datetime
try:
    with open('$META', encoding='utf-8') as f: d = json.load(f)
    d['last_indexed_commit'] = '$HEAD_COMMIT'
    d['last_indexed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open('$META', 'w', encoding='utf-8') as f: json.dump(d, f, indent=2)
except Exception as e:
    print(f'[post-commit] meta patch failed: {e}', file=sys.stderr)
" 2>>/tmp/gitnexus-postcommit.log
        fi
    fi
} &
