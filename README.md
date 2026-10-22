# securepub (beta)

A **host-only publication gate**, not a Git workspace manager. Agents clone,
commit, merge and test inside mbox/sbox. They export a Git bundle. A user runs
securepub on the host, approves an exact commit/repository/branch, and securepub
publishes it using existing host SSH authentication.

No merge automation, account setup, repository creation, credential delegation,
agent-callable daemon, `--yes`, or arbitrary destination URL. Multiple source
repositories are fine: each publication is a separate bundle and approval.

## Boundary

- Treat the development checkout **and its `.git`** as untrusted. Do not run
  host Git, build tools, or executable editor integrations against it.
- Keep the installed publisher, its host state, SSH credentials, SSH agent
  socket and host terminal **outside all guest writable/control grants**.
- Host user, installed Python/Git/OpenSSH and host ancestor directories are
  trusted. This does not defend against an agent already executing as your
  unsandboxed host user, a compromised host kernel, or Git parser exploits.
- mbox and sbox can export the same artifact. sbox shares the host kernel;
  GPU drivers add attack surface. These are not equivalent containment levels.
- The publisher copies the input to private staging, never hard-links it, never
  checks out guest files, and never runs Git in the source checkout. Input
  symlinks and non-regular files are refused. Hard-linked input is safe to copy;
  the staged inode is independent. All staged bytes are still untrusted data.
- Fresh bare Git repository, empty template, hooks disabled on **every** Git
  invocation, sanitized environment, system/global Git config disabled, no
  inherited remotes/configuration/credential helpers. Executable paths are
  pinned by the Nix package. Source-mode execution trusts host PATH.
- Imports SHA-1, self-contained bundles only. Object validation and process
  limits are defenses in depth, not a parser sandbox or complete disk quota.
  Bundle default limit is 512 MiB, max 2048 MiB. Full histories can be costly.
- New branches and fast-forward updates only. No deletion, tags, forced history
  rewrite, or implicit default branch. A compare-and-swap lease checks the
  reviewed prior remote revision (or absence). Git may treat a concurrent update
  to the *same desired commit* as already up to date; no new write is needed.
- `/dev/tty` approval is a host-user interaction, **not remote attestation or a
  desktop security prompt**. Do not expose host terminal control to an agent.
  The guest may request publication socially, but must not control approval.
- Publication can trigger GitHub Actions/deployments and disclose any secret in
  reachable history. Use a beta repository with Actions disabled or a reviewed,
  restricted CI policy. Repository contents are not certified safe by fsck.

## Export inside the development sandbox

First commit the changes you intend to publish. Choose a source branch:

```sh
git bundle create /path/to/exchange/proposal.bundle main
```

Use a full branch history, not `base..main`: the first beta deliberately refuses
bundles requiring pre-existing objects. Bundle creation is development Git and
belongs inside the sandbox. Exporting does not require authentication. Public
cloning/fetching can also remain entirely inside the sandbox. Private reads need
an independently authorized read capability.

## Inspect and publish on the host

Use a **trusted installed version**, not the current agent-writable source:

```sh
securepub /path/to/exchange/proposal.bundle \
  --source main --repo vbuterin/securepub --branch beta/first-test --inspect
```

`--inspect` validates locally with no SSH/network and prints the exact commit and
bundle SHA256. It does not review code for you or preserve staging after exit.
Inspect the proposed code in a sandbox. For publication, repeat without
`--inspect`; this stages anew and prompts for that invocation's exact commit:

```sh
securepub /path/to/exchange/proposal.bundle \
  --source main --repo vbuterin/securepub --branch beta/first-test
```

The prompt requires typing `publish OWNER/REPO refs/heads/BRANCH FULL_COMMIT_ID`.
There is no approval via stdin. Check that this ID matches your reviewed result.
A destination repository must already exist. Updating an existing branch requires
its tip to be an ancestor of the proposal; otherwise merge inside the sandbox
and export again. Every update needs new approval.

SSH is pinned to `git@github.com`, host-key checking is mandatory, batch mode is
on, and `~/.ssh/config` is **not used** (avoids ProxyCommand and other executable
configuration). Existing standard SSH identities / host agent may work; an
explicit host `--identity /absolute/path/to/key` is also supported. No passwords,
key creation, host-key enrollment, or account setup are implemented. Git HTTPS
credential helpers and SSH aliases are not supported by this beta. Normal
OpenSSH host known-hosts/default identity lookup still applies. Network queries
before approval authenticate reads, but cannot publish through this tool.

State is temporarily stored under `~/.local/state/securepub` (0700). Keep it
outside grants, even if a sandbox can see several repositories. Normal exits
clean staging; SIGKILL/machine failure may leave private staging to remove
manually. Never use the broad host home as a guest writable grant.

A failed/interrupted push may have succeeded remotely before the connection was
lost. Check the exact destination before retrying. No durable audit database or
recovery daemon is included.

## Packaging / self-hosting beta

The standalone flake exports a package, app and optional NixOS module
(`programs.securepub.enable`). It uses an exact nixpkgs revision. Building the
package runs disposable local tests. The module installs only the command: it
provisions no credential, network listener, agent socket or host grants.

1. Review this initial source and publish `vbuterin/securepub` manually using
   your existing trusted workflow. This bootstrap is intentionally outside the
   new gate. Select a license before advertising redistribution terms.
2. Pin that published revision in your host configuration and build/check it via
   the host's normal approved NixOS mechanism. Do not install an unreviewed
   development copy and call that the trusted boundary.
3. Run the installed command's local inspection on a sandbox-exported bundle.
4. Make a small change inside mbox/sbox, commit, export a new bundle, and publish
   it to an explicit beta branch using the **old installed publisher**.
5. Verify the remote commit. Updating the installed publisher is a separate
   reviewed configuration update; publishing its source does not self-update it.

## Local tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_securepub.py -v
```

Tests use disposable local repositories only. They cover new publication,
fast-forward, divergence, remote races, frozen staging, hook/config isolation,
input rejection, environment/branch validation, terminal denial and the offline
CLI, plus acceptance/cancellation through a private PTY. They do **not** prove
a real SSH/GitHub push, host terminal isolation,
mbox/sbox containment, or Nix package evaluation.

Current evidence: 11 tests passed with Python and Git 2.55.0 in the development
environment. The standalone flake has not yet been evaluated/built. Its nixpkgs
lock metadata was copied from the existing Monkeybox lock for that exact revision;
no Nix command was run to generate or validate it here.

No GitHub live test or host activation has been performed for this beta.
