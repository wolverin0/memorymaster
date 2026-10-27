import os
import subprocess
import sys
from pathlib import Path

# Python 3.12's Windows 0o700 ACL excludes this sandbox's restricted token.
# Match the ordinary workspace ACL only inside this run's disposable fixtures.
fixture_root = (Path.cwd() / '.tmp_tests' / 't1715-r2-fixtures').resolve()
original_mkdir = os.mkdir

def workspace_mkdir(path, mode=0o777, *, dir_fd=None):
    if mode == 0o700 and Path(path).resolve().is_relative_to(fixture_root):
        mode = 0o777
    return original_mkdir(path, mode, dir_fd=dir_fd)

os.mkdir = workspace_mkdir
sys.path.insert(0, str(Path.cwd()))
sys.stderr = sys.stdout
print('Command: pytest ' + ' '.join(sys.argv[1:]))
print('HOME and USERPROFILE redirected before imports; fixtures use disposable workspace paths.')
if os.environ.get('T1715_ROUND1_RUNTIME') == '1':
    import memorymaster.surfaces.backfill_cli as runtime
    source = subprocess.check_output([
        'git', 'show', '6acc9b6:memorymaster/surfaces/backfill_cli.py'
    ], text=True)
    exec(compile(source, 'round1_backfill_cli.py', 'exec'), runtime.__dict__)
    print('Regression check: round-one runtime loaded in this process only.')
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))

