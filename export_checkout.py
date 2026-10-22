#!/usr/bin/env python3
"""Internal helper. Runs ONLY inside securepub's credential-free sandbox."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    git = sys.argv[1]
    requested = sys.argv[2]
    repo = Path(sys.argv[3]) if len(sys.argv) > 3 else Path('/repo')
    output = Path(sys.argv[4]) if len(sys.argv) > 4 else Path('/out')
    os.chdir(repo)
    # mbox constructs its own guest environment; sanitize again inside BOTH
    # backends rather than assuming the host launch environment is propagated.
    os.environ.clear()
    os.environ.update({
        'HOME': '/nonexistent', 'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
        'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_OPTIONAL_LOCKS': '0',
    })
    # No repository discovery into parents, separate worktree gitdirs or bare repos.
    if not (repo / '.git').is_dir() or (repo / '.git').is_symlink():
        raise RuntimeError('Directory mode requires a checkout with its own .git directory; use bundle mode for linked worktrees.')
    command = [git, '--git-dir=' + str(repo / '.git'), '--work-tree=' + str(repo),
               '-c', 'safe.directory=' + str(repo), '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0',
               '-c', 'maintenance.auto=false', '-c', 'protocol.allow=never',
               '-c', 'core.commitGraph=false']

    def run(*args):
        return subprocess.check_output(command + list(args), stdin=subprocess.DEVNULL)

    def config(key):
        p = subprocess.run(command + ['config', '--local', '--no-includes', '--null', '--get-all', key],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, check=False)
        if p.returncode == 1:
            return []
        if p.returncode:
            raise RuntimeError('Cannot read repository configuration')
        return p.stdout.decode('utf-8').rstrip('\0').split('\0')

    urls = config('remote.origin.url')
    if len(urls) != 1:
        raise RuntimeError('Expected exactly one explicit remote.origin.url (includes are not read)')
    if config('remote.origin.pushurl'):
        raise RuntimeError('origin.pushurl is not supported; use explicit bundle mode to select a destination')
    ref = run('symbolic-ref', '--quiet', 'HEAD').decode('ascii').strip()
    if not ref.startswith('refs/heads/'):
        raise RuntimeError('HEAD must be on a local branch')
    selected = requested or ref[len('refs/heads/'):]
    # Validation happens in the helper as well as on the host; never interpret a
    # branch as an option, revision expression or wildcard.
    run('check-ref-format', 'refs/heads/' + selected)
    if not selected or selected.startswith('-'):
        raise RuntimeError('Invalid source branch')
    source = 'refs/heads/' + selected
    oid = run('rev-parse', '--verify', source + '^{commit}').decode('ascii').strip()
    # Read-only: never stage or commit user files. Dirty state is informational.
    dirty = bool(run('status', '--porcelain', '--untracked-files=normal', '--ignore-submodules=all'))
    run('bundle', 'create', str(output / 'checkout.bundle'), source)
    after = run('rev-parse', '--verify', source + '^{commit}').decode('ascii').strip()
    if after != oid or run('symbolic-ref', '--quiet', 'HEAD').decode('ascii').strip() != ref:
        raise RuntimeError('Branch changed while exporting; retry')
    (output / 'metadata.json').write_text(json.dumps({
        'origin': urls[0], 'branch': selected, 'oid': oid, 'dirty': dirty,
    }))


if __name__ == '__main__':
    main()
