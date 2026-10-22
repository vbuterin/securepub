# securepub (beta)

A **host-only publication gate**, not a Git workspace manager. Agents clone,
commit, merge and test inside mbox/sbox. On the host, simply run:

```sh
securepub /path/to/repository
```

It reads `origin` and the current branch inside a credential-free **Monkeybox
microVM**, exports the committed history automatically, then asks for approval of
the exact commit/repository/branch before pushing using host SSH authentication.

- Destination is the literal `remote.origin.url` on GitHub and the **same branch
  name as the current local branch**. Upstream mappings, `push.default`,
  `pushRemote`, URL rewrites and custom push refspecs are deliberately ignored.
- Accepts standard GitHub SSH and HTTPS origin URLs; publication still uses SSH.
  Explicit `origin.pushurl` is rejected to avoid ambiguity.
- Only commits are published. Dirty/untracked files produce a warning; the tool
  never stages files or creates commits.
- Detached HEAD, unborn branches, linked worktrees and bare repos are rejected.
  Linked worktrees can use explicit bundle mode instead.
- `--repo`, `--branch`, `--source` override defaults. `--inspect` validates
  locally without authentication or network access.
- Approval shows commit count, branch, repository and changed filenames. Enter
  `Y`/`yes` to accept, `V`/`view` for a full diff, or `N`/`no` to refuse (case
  insensitive). Blank input/EOF cancels; viewing never approves publication.

Directory mode tries a dedicated Monkeybox launcher first (KVM required), pinned
by the Nix package. It does not use the host's general-purpose `mbox` profile.
If VM launch/export fails, the host terminal asks whether to retry with
Bubblewrap, explicitly warning that this shares the host kernel. Type
`use bubblewrap` to authorize that one retry; anything else or no terminal cancels.
This prompt can also appear for a bad repository/export, not just missing KVM;
read the error rather than assuming a downgrade will fix it.

Fallback permission authorizes **export only**. Publication still needs its own
exact-commit approval. Failed output is discarded before a fresh fallback export.
No silent downgrade, remembered permission, or unsandboxed fallback exists.
Explicit bundle mode remains available below.

No merge automation, account setup, repository creation, credential delegation,
agent-callable daemon, `--yes`, or arbitrary destination URL. Multiple source
repositories are fine: each publication is a separate bundle and approval.

## Boundary

- Treat the development checkout **and its `.git`** as untrusted. Do not run
  unsandboxed host Git, build tools, or executable editor integrations against it.
- Keep the installed publisher, its host state, SSH credentials, SSH agent
  socket and host terminal **outside all guest writable/control grants**.
- Host user, installed Python/Git/OpenSSH and host ancestor directories are
  trusted. This does not defend against an agent already executing as your
  unsandboxed host user, a compromised host kernel, or Git parser exploits.
- mbox and sbox can export the same artifact. sbox shares the host kernel;
  GPU drivers add attack surface. These are not equivalent containment levels.
- The publisher copies the input to private staging, never hard-links it, never
  checks out guest files. Only the isolated exporter runs Git in the source
  checkout; the credential-bearing publisher uses a fresh bare repository. Input
  symlinks and non-regular files are refused. Hard-linked input is safe to copy;
  the staged inode is independent. All staged bytes are still untrusted data.
- Both exporters receive the read-only checkout/Nix store/trusted helper and
  one disposable writable export directory, without host credentials or terminal.
  The checkout and Nix store must contain no host secrets. Metadata and bundle
  are revalidated outside the exporter; checkout config cannot configure pushing.
- Primary: a dedicated Monkeybox microVM with blocked network, no port relays,
  no host profile, no configured persistent binds, no desktop/audio/camera grants,
  and an ephemeral guest home. Only export output is the writable project mount.
  Per-attempt engine state uses a short, random 0700 `/tmp/sp-*` directory,
  outside guest grants and separate from ordinary mbox engines. Explicit short
  `MSB_HOME`/`MBOX_STATE_ROOT` paths avoid the Unix socket path-length limit;
  host `TMPDIR` is not used for these paths. Normal success/failure cleans them;
  SIGKILL or a machine crash may leave private runtime files for manual cleanup.
  Kernel isolation comes from the VM, not namespaces.
- Fallback: Bubblewrap with private network/PID/IPC/user namespaces and private
  proc/dev/tmp/home. This shares the host kernel and requires explicit consent.
- Export errors do not certify the checkout safe. Object parsing in the host
  publishing repository is still outside either exporter sandbox.
- Publishing uses a fresh bare Git repository, empty template, hooks disabled on **every** Git
  invocation, sanitized environment, system/global Git config disabled, no
  inherited remotes/configuration/credential helpers. Executable paths are
  pinned by the Nix package. Source-mode execution trusts host PATH.
- Imports SHA-1, self-contained bundles only. Object validation and process
  limits are defenses in depth, not a parser sandbox or complete disk quota.
  Bundle default limit is 512 MiB, max 2048 MiB. Full histories can be costly.
  Git/export resource caps apply inside the guest and to host-side Git, not to
  the Monkeybox supervisor: its default sparse VM disk exceeds the 2 GiB export
  cap. The supervisor retains inherited host limits, disables core dumps, and
  has the export wall-clock timeout; the dedicated VM bounds guest memory/CPUs.
  Supervisor logs/runtime disk usage have no additional per-file cap from
  securepub. These limits are not a complete disk quota.
- New branches and fast-forward updates only. No deletion, tags, forced history
  rewrite, or fallback to an implicit `main` destination. A compare-and-swap lease checks the
  reviewed prior remote revision (or absence). Git may treat a concurrent update
  to the *same desired commit* as already up to date; no new write is needed.
- `/dev/tty` approval is a host-user interaction, **not remote attestation or a
  desktop security prompt**. Do not expose host terminal control to an agent.
  The guest may request publication socially, but must not control approval.
- Publication can trigger GitHub Actions/deployments and disclose any secret in
  reachable history. Use a beta repository with Actions disabled or a reviewed,
  restricted CI policy. Repository contents are not certified safe by fsck.

## Alternative: explicit bundle export inside the development sandbox

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

The host terminal shows, for example:

```text
Requesting to publish 3 commits of branch main to vbuterin/monkeybox

Total 4 files changed:

./example.py
./tests/example_test.py
./other_stuff.py
./README.md

Please type Y to accept, V to view a full diff, and N to refuse
```

Whitespace and case are ignored for `y`/`yes`, `v`/`view`, `n`/`no`. Unknown
answers reprompt; blank input/EOF cancels. No stdin approval. Hashes remain
internal during normal publication (available with `--inspect`). Approval is
bound to the same frozen commit and remote lease even without typing its hash.

Files and diff are the **net tree change**, not each commit's individual patch.
A new branch is compared with an empty tree. Renames appear as deletion/addition;
binary changes use Git binary patches. Viewing returns to the approval prompt.
No pager, external diff, textconv, checkout or project code runs on the host.
Control/non-ASCII characters are escaped for terminal safety. Diff output above
2 MiB aborts rather than showing an incomplete diff and allowing approval.

In directory mode when publishing to origin, the live destination must match
its last-fetched `refs/remotes/origin/<destination-branch>` in the checkout.
Missing tracking ref means a new branch is expected. If the remote has changed,
publication fails **before fetching/review**; the agent must fetch and merge in
the development sandbox, then retry. Securepub never updates the checkout's
tracking refs itself, including after a successful push. Fetch there before the
next publication. Tracking metadata is bounded to 8 KiB; very large ref sets
currently fail closed.

Bundle mode and explicit `--repo` overrides to another repository have no local
tracking baseline; they check fast-forward ancestry against the live remote.
All modes recheck the remote after approval and enforce a compare-and-swap lease
at push time. No automatic pull, merge, rebase, retry or history rewrite. Every
update needs new approval. A destination repository must already exist.

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
provisions no credential, network listener, agent socket or persistent host grants.

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

Directory-mode orchestration, URL validation, primary/fallback ordering, denial,
separate output directories and both sandbox commands have local tests. Run the optional real exporter test on a host with working
user namespaces and Nix-store Python/Git:

```sh
SECUREPUB_TEST_BWRAP=1 PYTHONDONTWRITEBYTECODE=1 python3 tests/test_securepub.py -v
```

The development environment rejects Bubblewrap startup (`Unexpected capabilities
but not setuid`). Neither the new dedicated Monkeybox package nor the fallback
has passed real sandbox integration here. Mocked tests do not establish runtime
containment. The Monkeybox lock entry uses the already recorded parent-config
revision/hash; it still needs a host Nix build and a real VM export test.

The original bundle-mode tests passed with Python and Git 2.55.0. The standalone flake has not yet been evaluated/built. Its nixpkgs
lock metadata was copied from the existing Monkeybox lock for that exact revision;
no Nix command was run to generate or validate it here.

No GitHub live test or host activation has been performed for this beta.
