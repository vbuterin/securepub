#!/usr/bin/env python3
"""Host-approved GitHub publication from a checkout directory or Git bundle."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
OID = re.compile(r"[0-9a-f]{40}\Z")
OUTPUT_LIMIT = 2 * 1024 * 1024
METADATA_LIMIT = 8192
# Nix packaging replaces these with immutable executable paths. Source-mode
# execution trusts the host's PATH; never run a guest-modified source copy.
GIT_EXECUTABLE = "git"
SSH_EXECUTABLE = "ssh"
BWRAP_EXECUTABLE = "bwrap"
# No general-purpose mbox from PATH: only the dedicated packaged launcher.
MONKEYBOX_EXECUTABLE = "@securepub-monkeybox@"


class Failure(Exception):
    pass


def branch(value):
    if (not BRANCH.fullmatch(value) or ".." in value or "//" in value
            or any(p.endswith((".lock", ".")) or p.startswith(".")
                   for p in value.split("/")) or value.endswith("/")):
        raise Failure("Invalid branch: use a conventional ASCII branch name.")
    return value


def vm_limits():
    # libkrun needs sparse disk files and a VM-sized virtual address space.
    # Do not impose Git-parser FSIZE/AS/NOFILE caps on the VM supervisor.
    # Keep inherited host limits; the launcher still has a wall-clock timeout
    # and the dedicated mbox configuration bounds guest memory/vCPUs.
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def limits():
    vm_limits()
    resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def clean_environment(home, *, network=False):
    # No caller GIT_*, shell startup, alternate object directories or pager.
    result = {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
    }
    if network and os.environ.get("SSH_AUTH_SOCK"):
        result["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return result


class Git:
    def __init__(self, root, executable, ssh=None, identity=None):
        self.root = Path(root)
        self.executable = executable
        self.ssh = ssh
        self.identity = identity

    def run(self, *args, network=False, timeout=180):
        env = clean_environment(self.root, network=network)
        if network:
            import shlex
            command = [self.ssh, "-F", "/dev/null", "-o", "BatchMode=yes",
                       "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no",
                       "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=15"]
            if self.identity:
                command += ["-o", "IdentitiesOnly=yes", "-i", self.identity]
            env["GIT_SSH_COMMAND"] = shlex.join(command)
            env["GIT_SSH_VARIANT"] = "ssh"
        command = [self.executable, "-c", "core.hooksPath=/dev/null",
                   "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
                   "-c", "maintenance.auto=false", "-c", "protocol.allow=never",
                   "-c", "protocol.file.allow=always",
                   "-c", "protocol.ssh.allow=" + ("always" if network else "never"),
                   "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
                   "-c", "fetch.unpackLimit=1", "-c", "fetch.writeCommitGraph=false",
                   "-c", "core.commitGraph=false", *args]
        # Spool rather than accumulating untrusted output in memory. RLIMIT_FSIZE
        # bounds individual files; a timeout bounds wall time. This is not a
        # complete disk quota or an OS sandbox for the Git parser.
        with tempfile.TemporaryFile(dir=self.root) as out, tempfile.TemporaryFile(dir=self.root) as err:
            child = subprocess.Popen(command, cwd=self.root, env=env,
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     preexec_fn=limits)
            try:
                child.wait(timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise
            out.seek(0)
            err.seek(0)
            stdout = out.read(OUTPUT_LIMIT + 1)
            stderr = err.read(OUTPUT_LIMIT + 1)
            if len(stdout) > OUTPUT_LIMIT or len(stderr) > OUTPUT_LIMIT:
                raise Failure("Git output exceeded the limit.")
            if child.returncode:
                # JSON escaping prevents terminal control-sequence injection.
                diagnostic = stderr[:4096].decode("utf-8", "replace")
                raise Failure("Git operation failed: " + json.dumps(diagnostic))
            return stdout


def copy_bundle(source, destination, max_bytes):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(source, flags)
    with os.fdopen(fd, "rb") as src:
        before = os.fstat(src.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise Failure("Bundle must be a regular file within the size limit.")
        digest = hashlib.sha256()
        total = 0
        with open(destination, "xb") as dst:
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise Failure("Bundle exceeded the size limit while reading.")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(src.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or total != before.st_size:
            raise Failure("Bundle changed while being copied; export it again.")
    os.chmod(destination, 0o400)
    return digest.hexdigest()


def prepare(git, source, source_branch, max_bytes):
    digest = copy_bundle(source, git.root / "proposal.bundle", max_bytes)
    (git.root / "empty-template").mkdir()
    git.run("init", "--bare", "--object-format=sha1", "--template=" + str(git.root / "empty-template"), ".")
    git.run("bundle", "verify", str(git.root / "proposal.bundle"))
    ref = "refs/heads/" + branch(source_branch)
    listing = git.run("bundle", "list-heads", str(git.root / "proposal.bundle"), ref)
    entries = listing.decode("ascii", "strict").splitlines()
    if len(entries) != 1:
        raise Failure("Bundle must advertise the selected source branch exactly once.")
    fields = entries[0].split(" ")
    if len(fields) != 2 or not OID.fullmatch(fields[0]) or fields[1] != ref:
        raise Failure("Malformed or unsupported bundle branch advertisement.")
    oid = fields[0]
    git.run("fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
            "--no-write-fetch-head", str(git.root / "proposal.bundle"),
            ref + ":refs/heads/proposal")
    actual = git.run("rev-parse", "--verify", "refs/heads/proposal^{commit}").decode().strip()
    if oid != actual:
        raise Failure("Selected branch must point directly to a commit.")
    git.run("fsck", "--full", "--strict", "--no-reflogs")
    return oid, digest


def remote_tip(git, url, ref):
    data = git.run("ls-remote", "--refs", url, ref, network=True).decode("ascii", "strict")
    if not data:
        return None
    lines = data.splitlines()
    if len(lines) != 1:
        raise Failure("Unexpected remote ref response.")
    fields = lines[0].split("\t")
    if len(fields) != 2 or not OID.fullmatch(fields[0]) or fields[1] != ref:
        raise Failure("Malformed remote ref response.")
    return fields[0]


def check_update(git, url, ref, oid):
    # Use the live destination, not potentially stale local tracking refs. A
    # remote ancestor is already published work; review/push only what remains.
    old = remote_tip(git, url, ref)
    if old == oid:
        return old
    if old:
        git.run("fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
                "--no-write-fetch-head", url, ref + ":refs/heads/remote-base", network=True)
        fetched = git.run("rev-parse", "refs/heads/remote-base").decode().strip()
        if fetched != old:
            raise Failure('Remote moved during inspection. Fetch and merge inside the development sandbox, then retry.')
        try:
            git.run("merge-base", "--is-ancestor", old, oid)
        except Failure as error:
            raise Failure("Non-fast-forward update refused. Merge inside the sandbox first.") from error
    return old


def push(git, url, ref, oid, old):
    # Lease pins the *exact* previously reviewed remote state, including absence.
    # Explicit ancestry checking above means the lease never authorizes a rewrite.
    if remote_tip(git, url, ref) != old:
        raise Failure('Remote changed during review. Fetch and merge inside the development sandbox, then retry.')
    git.run("push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
            "--force-with-lease=" + ref + ":" + (old or ""),
            url, oid + ":" + ref, network=True)


def terminal_text(data, *, multiline=False):
    """Escape control/bidi/non-ASCII bytes; never interpret repository terminal codes."""
    text = data.decode('utf-8', 'surrogateescape')
    return ''.join(c if ' ' <= c <= '~' or (multiline and c in '\n\t')
                   else c.encode('unicode_escape').decode('ascii') for c in text)


def proposal_summary(git, oid, old):
    count = git.run('rev-list', '--count', (old + '..' if old else '') + oid).decode().strip()
    if not count.isdecimal():
        raise Failure('Invalid commit count.')
    # A new branch has no prior destination tree: show all proposed files.
    base = old or git.run('hash-object', '-w', '-t', 'tree', '/dev/null').decode().strip()
    if not OID.fullmatch(base):
        raise Failure('Invalid diff base.')
    options = ('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--color=never')
    names = git.run(*options, '--name-only', '-z', base, oid, '--')
    if names and not names.endswith(b'\0'):
        raise Failure('Invalid changed-file listing.')
    files = names[:-1].split(b'\0') if names else []
    return count, base, options, files


def confirm(git, repo, ref, oid, old):
    count, base, options, files = proposal_summary(git, oid, old)
    # Approval is still bound to the frozen OID and lease, not a mutable branch.
    try:
        with open('/dev/tty', 'r', encoding='utf-8') as reader, open(
                '/dev/tty', 'w', encoding='utf-8', buffering=1) as tty:
            tty.write('\nRequesting to publish ' + count + (' commit' if count == '1' else ' commits') +
                      ' of branch ' + ref.removeprefix('refs/heads/') + ' to ' + repo + '\n\n' +
                      'Total ' + str(len(files)) + ' files changed:\n\n')
            for name in files:
                tty.write('./' + terminal_text(name) + '\n')
            while True:
                tty.write('\nPlease type Y to accept, V to view a full diff, and N to refuse\n> ')
                answer = reader.readline(1024)
                # EOF, blank input and incomplete/overlong lines never approve.
                if not answer or not answer.endswith('\n') or not answer.strip():
                    raise Failure('Publication cancelled.')
                answer = answer.strip().casefold()
                if answer in ('y', 'yes'):
                    return
                if answer in ('n', 'no'):
                    raise Failure('Publication cancelled.')
                if answer in ('v', 'view'):
                    patch = git.run(*options, '--binary', '--full-index', base, oid, '--')
                    tty.write('\n' + (terminal_text(patch, multiline=True) if patch else '(No net changes.)\n'))
                else:
                    tty.write('Please enter yes, view, or no.\n')
    except OSError as error:
        raise Failure('An interactive host terminal is required for approval.') from error


def executable(name):
    path = shutil.which(name)
    if not path:
        raise Failure("Required host executable is missing: " + name)
    return os.path.abspath(path)


def github_origin(value):
    """Normalize only literal GitHub URLs; never evaluate Git URL rewrites."""
    prefixes = ('git@github.com:', 'ssh://git@github.com/', 'https://github.com/')
    for prefix in prefixes:
        if value.startswith(prefix):
            repo = value[len(prefix):]
            if repo.endswith('.git'):
                repo = repo[:-4]
            if REPO.fullmatch(repo) and repo.split('/')[1] not in ('.', '..'):
                return repo
    raise Failure('Unsupported origin: use a literal github.com SSH or HTTPS repository URL.')


def confirm_fallback():
    """Downgrading containment is separate from authorizing publication."""
    try:
        with open('/dev/tty', 'r', encoding='utf-8') as reader, open(
                '/dev/tty', 'w', encoding='utf-8', buffering=1) as tty:
            tty.write('\nMonkeybox export failed. Retry with Bubblewrap?\n'
                      'WARNING: Bubblewrap shares the host kernel; it is not a microVM.\n'
                      'This authorizes export only, NOT publication.\n'
                      'Type use bubblewrap to continue; anything else cancels:\n> ')
            if reader.readline(1024).rstrip('\n') != 'use bubblewrap':
                raise Failure('Bubblewrap fallback cancelled.')
    except OSError as error:
        raise Failure('A host terminal is required to approve Bubblewrap fallback.') from error


def export_checkout(directory, output, source=None):
    # Never reuse a failed export's files for a different containment backend.
    try:
        with tempfile.TemporaryDirectory(prefix='monkeybox-', dir=output) as attempt:
            metadata = export_backend(directory, Path(attempt), source, backend='monkeybox')
            copy_bundle(Path(attempt) / 'checkout.bundle', output / 'checkout.bundle', 2 * 1024**3)
            return metadata
    except (Failure, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('Monkeybox export failed: ' + json.dumps(str(error)), file=sys.stderr)
    confirm_fallback()
    with tempfile.TemporaryDirectory(prefix='bubblewrap-', dir=output) as attempt:
        metadata = export_backend(directory, Path(attempt), source, backend='bubblewrap')
        copy_bundle(Path(attempt) / 'checkout.bundle', output / 'checkout.bundle', 2 * 1024**3)
        return metadata


def export_backend(directory, output, source=None, *, backend):
    """Keep VM runtime paths short, private, and outside the export grants."""
    if backend == 'monkeybox':
        # Unix socket paths are limited to 108 bytes. Do not inherit TMPDIR or
        # derive MSB_HOME from potentially long checkout/user-state paths.
        with tempfile.TemporaryDirectory(prefix='sp-', dir='/tmp') as runtime:
            root = Path(runtime)
            for grant in (directory, output):
                if root.resolve().is_relative_to(grant.resolve()):
                    raise Failure('Monkeybox runtime must be outside checkout/export grants.')
            return _export_backend(directory, output, source, backend=backend, runtime=root)
    return _export_backend(directory, output, source, backend=backend)


def _export_backend(directory, output, source=None, *, backend, runtime=None):
    """Only this credential-free process may run Git against checkout config."""
    directory = directory.resolve(strict=True)
    git = str(Path(executable(GIT_EXECUTABLE)).resolve())
    python = str(Path(sys.executable).resolve())
    helper = Path(__file__).with_name('export_checkout.py').resolve()
    # The packaged runtime is entirely in the immutable Nix store. Do not mount
    # /usr, /run, /home or the host root as a source-mode convenience fallback.
    if not all(p.startswith('/nix/store/') for p in (git, python)):
        raise Failure('Directory mode requires Nix-store Python and Git; use the Nix package or bundle mode.')
    command = [BWRAP_EXECUTABLE, '--unshare-all', '--die-with-parent', '--new-session',
               '--cap-drop', 'ALL', '--clearenv',
               '--ro-bind', '/nix/store', '/nix/store',
               '--ro-bind', str(directory), '/repo',
               '--bind', str(output), '/out',
               '--ro-bind', str(helper), '/export.py',
               '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
               '--dir', '/home', '--chdir', '/repo']
    for key, value in clean_environment('/home').items():
        command += ['--setenv', key, value]
    command += [python, '-I', '/export.py', git, source or '']
    env = clean_environment('/nonexistent')
    if backend == 'monkeybox':
        # mbox's configured same-path RO binding expands this one trusted
        # environment value. Reject mount-syntax delimiters, not spaces.
        if any(c in str(directory) for c in (':', '\n', '\r')):
            raise Failure('Monkeybox cannot share a path containing a colon or newline.')
        launcher = executable(MONKEYBOX_EXECUTABLE)
        if not str(helper).startswith('/nix/store/'):
            raise Failure('Monkeybox requires the installed immutable export helper.')
        # The project mount is ONLY the output directory. Checkout is a separate
        # read-only grant. Engine state is outside every guest grant.
        home = runtime / 'h'
        home.mkdir(mode=0o700)
        env = clean_environment(home)
        env['SECUREPUB_CHECKOUT'] = str(directory)
        env['MBOX_STATE_ROOT'] = str(runtime / 'm')
        env['MSB_HOME'] = str(runtime / 'e')
        command = [launcher, str(output), '--ephemeral', '--network', 'blocked',
                   '--no-visual', '--no-audio', '--no-microphone', '--no-camera', '--',
                   python, '-I', str(helper), git, source or '', str(directory), str(output)]
    elif backend == 'bubblewrap':
        command[0] = executable(BWRAP_EXECUTABLE)
    else:
        raise Failure('Unknown export backend')
    with tempfile.TemporaryFile() as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, env=env,
                                 preexec_fn=vm_limits if backend == 'monkeybox' else limits)
        try:
            child.wait(timeout=180)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
        if child.returncode:
            log.seek(0)
            raise Failure('Sandboxed export failed (no unsandboxed fallback): ' +
                          json.dumps(log.read(4096).decode('utf-8', 'replace')))
    fd = os.open(output / 'metadata.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise Failure('Invalid export metadata file')
        encoded = file.read(METADATA_LIMIT + 1)
        if len(encoded) > METADATA_LIMIT:
            raise Failure('Export metadata exceeded the size limit.')
        # Validate the whole bounded document, never a truncated JSON prefix.
        metadata = json.loads(encoded)
    if not isinstance(metadata, dict) or set(metadata) != {'origin', 'branch', 'oid', 'dirty'}:
        raise Failure('Invalid export metadata')
    if not all(isinstance(metadata[k], str) for k in ('origin', 'branch', 'oid')):
        raise Failure('Invalid export metadata types')
    github_origin(metadata['origin'])
    branch(metadata['branch'])
    if not OID.fullmatch(metadata['oid']) or type(metadata['dirty']) is not bool:
        raise Failure('Invalid export commit or dirty status')
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="checkout directory, or self-contained Git bundle")
    parser.add_argument("--source", help="source branch (directory: current branch; bundle: main)")
    parser.add_argument("--repo", help="GitHub owner/repository (directory: origin)")
    parser.add_argument("--branch", help="destination branch (directory: source branch)")
    parser.add_argument("--inspect", action="store_true", help="validate and display proposal locally; no authentication/network/push")
    parser.add_argument("--identity", type=Path, help="optional host SSH identity path (no SSH config is read)")
    parser.add_argument("--max-bundle-mib", type=int, default=512)
    args = parser.parse_args(argv)
    if args.source:
        branch(args.source)
    if not 1 <= args.max_bundle_mib <= 2048:
        raise Failure("Bundle limit must be between 1 and 2048 MiB.")
    os.umask(0o077)
    # This directory and the installed executable MUST be outside guest grants.
    state = Path.home() / ".local/state/securepub"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Failure("Host securepub state must be an owned private directory, not a symlink.")
    with tempfile.TemporaryDirectory(prefix="publish-", dir=state) as temporary:
        git = Git(temporary, executable(GIT_EXECUTABLE), None if args.inspect else executable(SSH_EXECUTABLE),
                  str(args.identity.absolute()) if args.identity else None)
        metadata = None
        # Export output is separate from the bare publisher repository. No host
        # staging/configuration/credentials are writable by the export process.
        with tempfile.TemporaryDirectory(prefix='export-', dir=state) as export_dir:
            bundle = args.path.absolute()
            if args.path.is_dir():
                metadata = export_checkout(args.path, Path(export_dir), args.source)
                args.repo = args.repo or github_origin(metadata['origin'])
                args.source = metadata['branch']
                args.branch = args.branch or args.source
                bundle = Path(export_dir) / 'checkout.bundle'
            if not args.repo or not args.branch:
                raise Failure('Bundle mode requires --repo and --branch.')
            if not REPO.fullmatch(args.repo) or args.repo.split('/')[1] in ('.', '..'):
                raise Failure('Destination must be a conventional GitHub owner/repository.')
            ref = 'refs/heads/' + branch(args.branch)
            oid, digest = prepare(git, bundle, args.source or 'main', args.max_bundle_mib * 1024**2)
        if metadata and oid != metadata['oid']:
            raise Failure('Exported branch moved during preparation; retry.')
        if metadata and metadata['dirty']:
            print('WARNING: uncommitted/untracked changes are NOT included. Publishing commits only.')
        if args.inspect:
            print('Destination: ' + args.repo + ' ' + ref)
            print('Staged bundle SHA256: ' + digest)
            print('Proposed commit: ' + oid)
            print("Local inspection completed. No remote contacted; nothing published.")
            return 0
        url = "git@github.com:" + args.repo + ".git"
        old = check_update(git, url, ref, oid)
        if old == oid:
            print("Remote branch already points to this commit; nothing published.")
            return 0
        confirm(git, args.repo, ref, oid, old)
        try:
            push(git, url, ref, oid, old)
        except (Failure, OSError, subprocess.TimeoutExpired) as error:
            raise Failure("Push did not report success; its remote outcome may be uncertain. "
                          "Check the destination before retrying. " + str(error)) from error
        print('Published branch ' + args.branch + ' to ' + args.repo + '.')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Failure, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("securepub: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("securepub: cancelled; a push interrupted after approval may have completed. Check the remote.", file=sys.stderr)
        sys.exit(130)
