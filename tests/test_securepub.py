#!/usr/bin/env python3
"""Disposable local Git tests: no account, SSH, credentials, or network."""
import importlib.util
import os
import pty
import select
import time
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("securepub", Path(__file__).resolve().parents[1] / "securepub.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
GIT = pub.executable("git")


def run(cwd, *args):
    env = pub.clean_environment(cwd)
    return subprocess.check_output([GIT, "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=env, stderr=subprocess.PIPE).decode().strip()


class LocalGit(pub.Git):
    """Network calls redirected to a test-controlled file repository only."""
    def run(self, *args, network=False, **kwargs):
        return super().run(*args, network=False, **kwargs)


class Publication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "source"
        self.src.mkdir()
        run(self.src, "init", "-b", "main", "--template=")
        run(self.src, "config", "user.name", "Fixture")
        run(self.src, "config", "user.email", "fixture@example.invalid")
        self.commit("initial")
        self.remote = self.root / "remote.git"
        run(self.root, "init", "--bare", "--template=", str(self.remote))
        self.ref = "refs/heads/main"

    def commit(self, text):
        (self.src / "file").write_text(text)
        run(self.src, "add", "file")
        run(self.src, "commit", "-m", text)
        return run(self.src, "rev-parse", "HEAD")

    def stage(self, name="staged"):
        bundle = self.root / (name + ".bundle")
        run(self.src, "bundle", "create", str(bundle), "main")
        root = self.root / name
        root.mkdir(mode=0o700)
        git = LocalGit(root, GIT)
        oid, digest = pub.prepare(git, bundle, "main", 1024**2)
        self.assertEqual(len(digest), 64)
        return git, oid, bundle

    def test_new_then_fast_forward(self):
        git, oid, _ = self.stage()
        old = pub.check_update(git, str(self.remote), self.ref, oid)
        self.assertIsNone(old)
        pub.push(git, str(self.remote), self.ref, oid, old)
        self.assertEqual(run(self.remote, "rev-parse", self.ref), oid)
        next_oid = self.commit("second")
        git2, oid2, _ = self.stage("second")
        self.assertEqual(oid2, next_oid)
        old2 = pub.check_update(git2, str(self.remote), self.ref, oid2)
        self.assertEqual(old2, oid)
        pub.push(git2, str(self.remote), self.ref, oid2, old2)
        self.assertEqual(run(self.remote, "rev-parse", self.ref), oid2)

    def test_remote_race_rejected(self):
        git, oid, _ = self.stage()
        old = pub.check_update(git, str(self.remote), self.ref, oid)
        self.commit("concurrent remote update")
        run(self.src, "push", str(self.remote), "main")
        with self.assertRaises(pub.Failure):
            pub.push(git, str(self.remote), self.ref, oid, old)

    def test_divergence_rejected(self):
        first = run(self.src, "rev-parse", "HEAD")
        self.commit("remote work")
        run(self.src, "push", str(self.remote), "main")
        run(self.src, "reset", "--hard", first)
        self.commit("different work")
        git, oid, _ = self.stage()
        with self.assertRaisesRegex(pub.Failure, "Non-fast-forward"):
            pub.check_update(git, str(self.remote), self.ref, oid)

    def test_hook_and_config_not_imported_and_staging_is_frozen(self):
        marker = self.root / "executed"
        hook = self.src / ".git/hooks/pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        hook.chmod(0o755)
        run(self.src, "config", "core.hooksPath", str(hook.parent))
        run(self.src, "config", "credential.helper", "!touch " + str(marker))
        git, oid, bundle = self.stage()
        self.commit("after snapshot")
        bundle.write_bytes(b"replaced after staging")
        config = (git.root / "config").read_text()
        self.assertNotIn("credential", config)
        self.assertNotIn(str(hook.parent), config)
        pub.push(git, str(self.remote), self.ref, oid, None)
        self.assertFalse(marker.exists())
        self.assertEqual(run(self.remote, "rev-parse", self.ref), oid)
        self.assertNotEqual(run(self.src, "rev-parse", "HEAD"), oid)

    def test_publisher_reference_hook_disabled(self):
        git, oid, _ = self.stage()
        marker = self.root / "hook-ran"
        hooks = git.root / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        hook.chmod(0o755)
        git.run("update-ref", "refs/heads/test", oid)
        self.assertFalse(marker.exists())

    def test_symlink_fifo_and_size_rejected(self):
        source = self.root / "input"
        source.write_bytes(b"1234")
        link = self.root / "link"
        link.symlink_to(source)
        with self.assertRaises(OSError):
            pub.copy_bundle(link, self.root / "dest", 100)
        with self.assertRaises(pub.Failure):
            pub.copy_bundle(source, self.root / "dest", 3)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(pub.Failure):
            pub.copy_bundle(fifo, self.root / "dest", 100)

    def test_incremental_and_invalid_bundles_rejected(self):
        first = run(self.src, "rev-parse", "HEAD")
        self.commit("second")
        bundle = self.root / "incremental.bundle"
        run(self.src, "bundle", "create", str(bundle), first + "..main")
        for name, contents in (("incremental", bundle.read_bytes()), ("invalid", b"garbage")):
            source = self.root / (name + ".input")
            source.write_bytes(contents)
            stage = self.root / name
            stage.mkdir()
            with self.assertRaises(pub.Failure):
                pub.prepare(LocalGit(stage, GIT), source, "main", 1024**2)

    def test_environment_and_branch_validation(self):
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_DIR": "evil", "SSH_AUTH_SOCK": "secret", "LD_PRELOAD": "evil"}):
            env = pub.clean_environment(self.root)
            for key in ("GIT_DIR", "GIT_CONFIG_COUNT", "SSH_AUTH_SOCK", "LD_PRELOAD"):
                self.assertNotIn(key, env)
        for value in ("--all", "a..b", "a.lock", "x/../y", "a\nb", "a/", "a//b", ".hidden"):
            with self.assertRaises(pub.Failure):
                pub.branch(value)
        self.assertEqual(pub.branch("agent/task-1"), "agent/task-1")

    def test_cli_inspect_offline(self):
        _, oid, bundle = self.stage()
        home = self.root / "host-home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        env.pop("SSH_AUTH_SOCK", None)
        result = subprocess.run([os.sys.executable, str(Path(pub.__file__)), str(bundle),
                                 "--repo", "vbuterin/securepub", "--branch", "beta",
                                 "--inspect"], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(oid, result.stdout)
        self.assertIn("nothing published", result.stdout)
        self.assertEqual(list((home / ".local/state/securepub").iterdir()), [])

    def test_real_private_terminal_approval(self):
        # Exercise the actual /dev/tty path on a private PTY, never the desktop.
        for approve in (False, True):
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    pub.confirm("owner/repo", self.ref, "a" * 40, None)
                except pub.Failure:
                    os._exit(2)
                os._exit(0)
            reaped = False
            try:
                output = b""
                deadline = time.monotonic() + 5
                while b"> " not in output:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0)
                    ready, _, _ = select.select([fd], [], [], remaining)
                    self.assertTrue(ready)
                    output += os.read(fd, 8192)
                answer = ("publish owner/repo " + self.ref + " " + "a" * 40) if approve else "no"
                os.write(fd, (answer + "\n").encode())
                while time.monotonic() < deadline:
                    child, status = os.waitpid(pid, os.WNOHANG)
                    if child:
                        reaped = True
                        break
                    time.sleep(0.01)
                self.assertTrue(reaped, "approval child did not exit")
                self.assertEqual(os.waitstatus_to_exitcode(status), 0 if approve else 2)
            finally:
                if not reaped:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                os.close(fd)

    def test_approval_cannot_come_from_stdin(self):
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaisesRegex(pub.Failure, "host terminal"):
                pub.confirm("owner/repo", self.ref, "a" * 40, None)
        tty = mock.MagicMock()
        tty.__enter__.return_value = tty
        tty.readline.return_value = "yes\n"
        with mock.patch("builtins.open", return_value=tty):
            with self.assertRaisesRegex(pub.Failure, "cancelled"):
                pub.confirm("owner/repo", self.ref, "a" * 40, None)


if __name__ == "__main__":
    unittest.main()
