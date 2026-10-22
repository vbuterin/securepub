#!/usr/bin/env python3
"""Host-side, exact-commit GitHub publication. No development Git operations."""
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
# Nix packaging replaces these with immutable executable paths. Source-mode
# execution trusts the host's PATH; never run a guest-modified source copy.
GIT_EXECUTABLE = "git"
SSH_EXECUTABLE = "ssh"


class Failure(Exception):
    pass


def branch(value):
    if (not BRANCH.fullmatch(value) or ".." in value or "//" in value
            or any(p.endswith((".lock", ".")) or p.startswith(".")
                   for p in value.split("/")) or value.endswith("/")):
        raise Failure("Invalid branch: use a conventional ASCII branch name.")
    return value


def limits():
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
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
    old = remote_tip(git, url, ref)
    if old:
        git.run("fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
                "--no-write-fetch-head", url, ref + ":refs/heads/remote-base", network=True)
        fetched = git.run("rev-parse", "refs/heads/remote-base").decode().strip()
        if fetched != old:
            raise Failure("Remote moved during inspection. Retry publication.")
        try:
            git.run("merge-base", "--is-ancestor", old, oid)
        except Failure as error:
            raise Failure("Non-fast-forward update refused. Merge inside the sandbox first.") from error
    return old


def push(git, url, ref, oid, old):
    # Lease pins the *exact* previously reviewed remote state, including absence.
    # Explicit ancestry checking above means the lease never authorizes a rewrite.
    git.run("push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
            "--force-with-lease=" + ref + ":" + (old or ""),
            url, oid + ":" + ref, network=True)


def confirm(repo, ref, oid, old):
    phrase = "publish " + repo + " " + ref + " " + oid
    # No --yes, stdin approval, or approval supplied by the development guest.
    try:
        with open("/dev/tty", "r", encoding="utf-8") as reader, open(
                "/dev/tty", "w", encoding="utf-8", buffering=1) as tty:
            tty.write("\nDestination: git@github.com:" + repo + ".git\n"
                      "Branch: " + ref + "\nCommit: " + oid + "\n"
                      "Previous: " + (old or "<new branch>") + "\n\n"
                      "Pushing may trigger CI/workflows or publish secrets in history.\n"
                      "Type exactly to approve (anything else cancels):\n" + phrase + "\n> ")
            if reader.readline(1024).rstrip("\n") != phrase:
                raise Failure("Publication cancelled.")
    except OSError as error:
        raise Failure("An interactive host terminal is required for approval.") from error


def executable(name):
    path = shutil.which(name)
    if not path:
        raise Failure("Required host executable is missing: " + name)
    return os.path.abspath(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="self-contained Git bundle exported inside the sandbox")
    parser.add_argument("--source", default="main", help="branch inside the bundle (default: main)")
    parser.add_argument("--repo", required=True, help="explicit GitHub owner/repository")
    parser.add_argument("--branch", required=True, help="destination branch; no implicit main")
    parser.add_argument("--inspect", action="store_true", help="validate and display proposal locally; no authentication/network/push")
    parser.add_argument("--identity", type=Path, help="optional host SSH identity path (no SSH config is read)")
    parser.add_argument("--max-bundle-mib", type=int, default=512)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo) or args.repo.split("/")[1] in (".", ".."):
        raise Failure("Destination must be a conventional GitHub owner/repository.")
    branch(args.source)
    ref = "refs/heads/" + branch(args.branch)
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
        oid, digest = prepare(git, args.bundle.absolute(), args.source, args.max_bundle_mib * 1024**2)
        print("Staged bundle SHA256: " + digest)
        print("Proposed commit: " + oid)
        # No checkout, external diff, textconv, filenames or commit messages rendered.
        print("Inspect this exact proposal in a sandbox before approving publication.")
        if args.inspect:
            print("Local inspection completed. No remote contacted; nothing published.")
            return 0
        url = "git@github.com:" + args.repo + ".git"
        old = check_update(git, url, ref, oid)
        if old == oid:
            print("Remote branch already points to this commit; nothing published.")
            return 0
        count = git.run("rev-list", "--count", (old + ".." if old else "") + oid).decode().strip()
        if not count.isdecimal():
            raise Failure("Invalid commit count.")
        print("Commits introduced: " + count)
        confirm(args.repo, ref, oid, old)
        try:
            push(git, url, ref, oid, old)
        except (Failure, OSError, subprocess.TimeoutExpired) as error:
            raise Failure("Push did not report success; its remote outcome may be uncertain. "
                          "Check the destination before retrying. " + str(error)) from error
        print("Published " + oid + " to " + args.repo + " " + ref)
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
