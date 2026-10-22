#!/usr/bin/env python3
"""Disposable local Git tests: no account, SSH, credentials, or network."""
import importlib.util
import json
import shutil
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

    def test_origin_validation(self):
        for url in ('git@github.com:owner/repo.git', 'ssh://git@github.com/owner/repo.git',
                    'https://github.com/owner/repo', 'https://github.com/owner/repo.git'):
            self.assertEqual(pub.github_origin(url), 'owner/repo')
        for url in ('ext::evil', 'file:///repo', 'git@evil.com:owner/repo',
                    'https://github.com/owner/repo?x=y', 'https://github.com/owner/../repo',
                    'https://token@github.com/owner/repo', 'https://github.com/owner/repo\n'):
            with self.assertRaises(pub.Failure):
                pub.github_origin(url)

    def test_directory_cli_defaults_with_fake_export(self):
        _, oid, bundle = self.stage()
        home = self.root / 'private-home'
        home.mkdir()

        def export(directory, output, source):
            self.assertEqual(directory, self.src)
            self.assertIsNone(source)
            shutil.copyfile(bundle, output / 'checkout.bundle')
            return dict(origin='https://github.com/owner/repo.git', branch='main', oid=oid, dirty=True)

        with mock.patch.dict(os.environ, HOME=str(home)), mock.patch.object(pub, 'export_checkout', export), \
                mock.patch('builtins.print') as printer:
            self.assertEqual(pub.main([str(self.src), '--inspect']), 0)
        messages = '\n'.join(str(c.args[0]) for c in printer.call_args_list)
        self.assertIn('Destination: owner/repo refs/heads/main', messages)
        self.assertIn('NOT included', messages)

    def test_export_command_contains_only_expected_grants(self):
        output = self.root / 'output'
        output.mkdir()
        metadata = dict(origin='git@github.com:owner/repo.git', branch='main', oid='a' * 40, dirty=False)
        (output / 'metadata.json').write_text(json.dumps(metadata))
        child = mock.Mock(returncode=0)
        with mock.patch.object(pub, 'executable', side_effect=lambda name: '/nix/store/test/bin/' + name), \
                mock.patch.object(pub.sys, 'executable', '/nix/store/test/bin/python3'), \
                mock.patch.object(pub.subprocess, 'Popen', return_value=child) as popen:
            self.assertEqual(pub.export_backend(self.src, output, backend='bubblewrap'), metadata)
        command = popen.call_args.args[0]
        for flag in ('--unshare-all', '--new-session', '--clearenv', '--die-with-parent'):
            self.assertIn(flag, command)
        self.assertIn(['--ro-bind', str(self.src), '/repo'], [command[i:i+3] for i in range(len(command))])
        self.assertEqual(command.count('--bind'), 1)
        self.assertNotIn('SSH_AUTH_SOCK', popen.call_args.kwargs['env'])

    @unittest.skipUnless(os.environ.get('SECUREPUB_TEST_BWRAP') == '1',
                         'opt-in host Bubblewrap integration test')
    def test_real_directory_export(self):
        run(self.src, 'remote', 'add', 'origin', 'https://github.com/owner/repo.git')
        run(self.src, 'config', 'core.fsmonitor', '/repo/evil')
        (self.src / 'evil').write_text('#!/bin/sh\ntouch /out/hook-ran\n')
        (self.src / 'evil').chmod(0o755)
        hooks = self.src / '.git/hooks'
        hooks.mkdir(exist_ok=True)
        hook = hooks / 'pre-auto-gc'
        hook.write_text('#!/bin/sh\ntouch /out/hook-ran\n')
        hook.chmod(0o755)
        output = self.root / 'output'
        output.mkdir()
        metadata = pub.export_backend(self.src, output, backend='bubblewrap')
        self.assertEqual(metadata['origin'], 'https://github.com/owner/repo.git')
        self.assertEqual(metadata['branch'], 'main')
        self.assertTrue(metadata['dirty'])
        self.assertFalse((output / 'hook-ran').exists())
        staged = self.root / 'verified'
        staged.mkdir()
        oid, _ = pub.prepare(LocalGit(staged, GIT), output / 'checkout.bundle', 'main', 1024**2)
        self.assertEqual(oid, metadata['oid'])
        run(self.src, 'checkout', '--detach')
        with self.assertRaises(pub.Failure):
            pub.export_backend(self.src, output, backend='bubblewrap')

    @unittest.skipUnless(os.environ.get('SECUREPUB_TEST_MONKEYBOX') == '1',
                         'opt-in installed Monkeybox exporter test')
    def test_real_monkeybox_export(self):
        # Run using the installed module, whose launcher/helper paths are pinned.
        installed = os.environ.get('SECUREPUB_INSTALLED_MODULE')
        self.assertTrue(installed, 'Set SECUREPUB_INSTALLED_MODULE to the installed securepub.py')
        spec = importlib.util.spec_from_file_location('installed_securepub', installed)
        installed_pub = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installed_pub)
        run(self.src, 'remote', 'add', 'origin', 'git@github.com:owner/repo.git')
        output = self.root / 'vm-real-output'
        output.mkdir()
        metadata = installed_pub.export_backend(self.src, output, backend='monkeybox')
        self.assertEqual(metadata['branch'], 'main')
        self.assertEqual(metadata['origin'], 'git@github.com:owner/repo.git')
        staged = self.root / 'vm-verified'
        staged.mkdir()
        oid, _ = pub.prepare(LocalGit(staged, GIT), output / 'checkout.bundle', 'main', 1024**2)
        self.assertEqual(oid, metadata['oid'])

    def test_backend_selection_and_confirmed_fallback(self):
        metadata = dict(origin='git@github.com:owner/repo.git', branch='main', oid='a' * 40, dirty=False)
        for mode in ('primary', 'fallback', 'deny'):
            output = self.root / mode
            output.mkdir()
            calls = []
            def backend(directory, dest, source, *, backend):
                calls.append(backend)
                if backend == 'monkeybox' and mode != 'primary':
                    (dest / 'checkout.bundle').write_bytes(b'failed-export')
                    raise pub.Failure('VM unavailable')
                self.assertFalse((dest / 'checkout.bundle').exists())
                (dest / 'checkout.bundle').write_bytes(b'good-export')
                return metadata
            with mock.patch.object(pub, 'export_backend', side_effect=backend), \
                    mock.patch.object(pub, 'confirm_fallback') as approval:
                if mode == 'deny':
                    approval.side_effect = pub.Failure('cancelled')
                    with self.assertRaises(pub.Failure):
                        pub.export_checkout(self.src, output)
                    self.assertEqual(calls, ['monkeybox'])
                    self.assertFalse((output / 'checkout.bundle').exists())
                else:
                    self.assertEqual(pub.export_checkout(self.src, output), metadata)
                    self.assertEqual((output / 'checkout.bundle').read_bytes(), b'good-export')
                    self.assertEqual(calls, ['monkeybox'] if mode == 'primary' else ['monkeybox', 'bubblewrap'])
                self.assertEqual(approval.call_count, 0 if mode == 'primary' else 1)

    def test_fallback_requires_terminal_and_exact_phrase(self):
        with mock.patch('builtins.open', side_effect=OSError('no tty')):
            with self.assertRaisesRegex(pub.Failure, 'host terminal'):
                pub.confirm_fallback()
        for phrase in ('yes\n', 'use bubblewrap\n'):
            tty = mock.MagicMock()
            tty.__enter__.return_value = tty
            tty.readline.return_value = phrase
            with mock.patch('builtins.open', return_value=tty):
                if phrase == 'yes\n':
                    with self.assertRaisesRegex(pub.Failure, 'cancelled'):
                        pub.confirm_fallback()
                else:
                    pub.confirm_fallback()
            self.assertIn('shares the host kernel', tty.write.call_args.args[0])

    def test_monkeybox_command_uses_dedicated_launcher(self):
        output = self.root / 'vm-output'
        output.mkdir()
        metadata = dict(origin='git@github.com:owner/repo.git', branch='main', oid='a' * 40, dirty=False)
        (output / 'metadata.json').write_text(json.dumps(metadata))
        child = mock.Mock(returncode=0)
        with mock.patch.object(pub, 'executable', side_effect=lambda name: '/nix/store/test/bin/' + name), \
                mock.patch.object(pub.sys, 'executable', '/nix/store/test/bin/python3'), \
                mock.patch.object(pub, '__file__', '/nix/store/publisher/securepub.py'), \
                mock.patch.object(pub.subprocess, 'Popen', return_value=child) as popen:
            self.assertEqual(pub.export_backend(self.src, output, backend='monkeybox'), metadata)
        command = popen.call_args.args[0]
        env = popen.call_args.kwargs['env']
        self.assertIn(pub.MONKEYBOX_EXECUTABLE, command[0])
        self.assertEqual(command[1], str(output))
        self.assertIn('--ephemeral', command)
        self.assertEqual(env['SECUREPUB_CHECKOUT'], str(self.src))
        self.assertNotIn('SSH_AUTH_SOCK', env)
        self.assertNotIn('MSB_PATH', env)
        self.assertFalse(Path(env['MBOX_STATE_ROOT']).is_relative_to(output))

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
