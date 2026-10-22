# Lazurio Hermes Agent: releases

Runbook for the owner of Lazurio Hermes Agent releases (the Organization
Steward, today Pablo, `agentrozjedemeai`). The Steward prepares, reviews,
tests and dispatches a release; the Organization Admin (Matěj,
`immakermatty`) approves it in the GitHub environment and is the only one who
moves `main` to a new upstream base. Plan: DEV-6676 in the Human and Machine
Mission Control; direction: Lazurio decision 0210 point 2.

`Lazurio/hermes-agent` distributes [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research (MIT) for Lazurio's Osobní Prostředí, the personal
Environment where Hermes runs Buddy, the user's personal agent. It is a
**rolling distribution patch-stack**, the same model as `Lazurio/t3code` and
`Lazurio/OpenMausBot`: `main` is an exact upstream stable release tag and on
top of it only the current Lazurio overlay commits. Lazurio changes Hermes
only where upstream does not fit Lazurio, and every change says when it goes
away.

Decision 0210 point 2: Hermes is installed the way upstream recommends, with
the official installer, the source taken from this fork at the exact commit
Lazurio Machines pin. This fork therefore publishes no packages or images; a
release is a protected tag and a GitHub Release whose notes say what is in it.

## Licence

Hermes Agent is under the MIT License (`LICENSE`, copyright Nous Research).
The fork keeps that file unchanged, and the Lazurio changes are MIT too.

## Upstream base

| | |
| --- | --- |
| Upstream | `NousResearch/hermes-agent` |
| Stable tag | `v0.22.4` (published 2026-10-21) |
| Tag object | `64dbae64747ab640a3fece06f359307c30d129da` (annotated) |
| Commit (peeled) | `4d04303818b61bf3aa09278af48356315515965b` |

`UPSTREAM_TAG` and `UPSTREAM_SHA` in `.github/workflows/lazurio-fork-ci.yml`
are the machine-readable copy of this table; CI, the release workflow and the
contract test (`scripts/lazurio_fork_contract_test.py`) hold all three
together. Upstream tags are annotated, so the commit is always the peeled one
(`git rev-parse "refs/tags/v0.22.4^{commit}"`, or the `^{}` line of
`git ls-remote`), never the tag object.

The previous base was upstream `v0.22.2` (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`),
released here as `v0.22.2-lazurio.1` without any Lazurio change. Because that
release had no overlay and upstream `v0.22.4` descends from it, `main` moved
to `v0.22.4` by a plain fast-forward (see
[Following upstream](#following-upstream)).

## How Lazurio installs a release

Lazurio Machines (`HumanAndMachine-ai/Machines`) pin a release as
`{version, commit}`: `version` is `X.Y.Z-lazurio.N`, `commit` the commit its
tag names. The Osobní Prostředí then runs the official installer with this
fork as the source:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh |
  HERMES_REPO_URL=https://github.com/Lazurio/hermes-agent.git bash -s -- --commit <commit>
```

- **The pin is the update path.** An Osobní Prostředí moves to a new release
  only when its pin in Machines moves. Upstream's own update channel
  (`hermes update`, the installer's stable channel) follows NousResearch tags
  and releases, not this fork, so Lazurio installs never use it.
- **The pinned commit must be on `main`.** The installer clones `main` and
  refuses a `--commit` that is not its ancestor (`commit_not_on_branch`).
  A fast-forward keeps every older release on `main`. A rebuild replaces
  `main` (below), after which an install still pinned to an older release
  fails closed instead of installing something else. So a rebuild is released
  right after the swap and Machines move the pin to it; pins only move forward
  and an old release is never installed again (decision 0166, repair forward).
- **`hermes --version` needs upstream's tag.** Hermes names its version after
  the highest upstream stable tag (`vX.Y.Z`) reachable from the checkout,
  plus the commits since it (`0.22.4+3.g1a2b3c4`). The installer fetches this
  fork's tags, so the fork carries a copy of upstream's exact tag object for
  each base; the release workflow adds it when it is missing and refuses one
  that differs from upstream.
- **Extras and configuration** (`[acp]`, `[mcp]`, Buddy's `config.yaml`) belong
  to the Machines and LazurioPlatform installation of Buddy, not to this fork.

## Versions

The version is `X.Y.Z-lazurio.N`, tag `vX.Y.Z-lazurio.N`. `X.Y.Z` is the
upstream stable tag `main` stands on. `N` starts at 1 and grows with each
release on the same upstream base; a new upstream base starts again at `.1`.
`X.Y.Z-lazurio.0` is never released.

The release workflow refuses a version that is not higher than every
published Lazurio release, so the newest release is always the highest
version. Mind SemVer: `0.22.4-lazurio.1` sorts below upstream `0.22.4`;
nothing in Lazurio compares our versions with upstream's.

## Overlay

`main` is the exact upstream stable tag above and on top of it only these
commits, in this order:

| Commit | Why Lazurio needs it | Upstream and removal |
| --- | --- | --- |
| `release: Lazurio distribution on upstream v0.22.4` | This runbook, `.github/README.md`, `scripts/lazurio_fork_contract_test.py` and the workflows `lazurio-fork-ci.yml`, `lazurio-upstream-watch.yml` and `lazurio-release.yml`. | Lazurio-only; stays. In a rebuild it goes last. |
| `acp: model and thought_level session config options on the gateway viewer` | Buddy's chat in Lazurio T3 Code reads models only from ACP `configOptions` of category `model` and offers other selects in its composer; upstream's viewer advertises only the unstable `models` field and ignores `set_config_option`, so the chat showed one "Default" model and no reasoning effort. Session replies now carry `model` (category `model`, the shared model picker) and `thought_level` (category `thought_level`, upstream's `/reasoning` levels with upstream's labels) beside `models` and `modes`; `set_config_option` runs the owner's model pick or `reasoning` mutation; afterwards what the editor was shown follows the owner by reading it: an owner `session.info` or an adopted snapshot (whose events the pump skips) whose model or level differs asks for a refresh, which reads the owner's current `session.info` and sends `config_option_update` when it differs (one at a time per session, after every answer and attach reply too, through the live connection a reconnect installs, never opening one; a failed read keeps the request and is retried a few times on its connection), so an older event never overwrites a newer answer. Code: `acp_adapter/lazurio_config_options.py`, hooks in `acp_adapter/gateway_server.py`. Tests: `tests/acp_adapter/test_lazurio_config_options*.py`. | Upstream-friendly. Remove when upstream's gateway ACP viewer advertises config options for the model and the reasoning effort and implements `set_config_option` for them (v0.22.4: `set_config_option` answers `[]`). |

Rules:

- **Upstream first.** A change lands here only when upstream does not fit
  Lazurio. Where it would help everyone, propose it upstream as well and link
  the proposal in this table.
- **One commit per row**, with the row's subject, so a rebuild replays the
  overlay one row at a time. A later fix of a row is a normal commit on `main`;
  the next rebuild folds it into its row.
- **Every row has a removal condition**: what upstream has to ship before the
  row goes. A rebuild removes every row whose condition upstream met.
- **Lazurio code lives in Lazurio files**, named `lazurio_*` beside the
  upstream modules they extend (for example `acp_adapter/lazurio_*.py`; the
  packaging ships `acp_adapter`'s own modules but not subpackages), and
  upstream files carry only the hooks into them.
- **Upstream files the overlay changes are listed** in
  `allowed_upstream_changes` in `lazurio-fork-ci.yml`, grouped by the row that
  owns them, each with its reason. The overlay changes upstream files only in
  their content: CI fails on a content change to any other upstream file, and
  always on a deleted, renamed or retyped upstream file, listed or not. New
  Lazurio files need no entry. Upstream workflow files are never changed; they are disabled in the
  repository settings instead (see [Automation boundary](#automation-boundary)).
- Pull requests into `main` merge by **rebase only**, without a merge commit;
  CI and the release refuse a merge commit above the upstream tag.

## When to release

- **New upstream stable** (`NousResearch/hermes-agent` publishes a GitHub
  Release `vX.Y.Z` that is neither a draft nor a pre-release): follow upstream
  ([Following upstream](#following-upstream)) and release `X.Y.Z-lazurio.1`.
- **A fix or change of the overlay** on the same base: after it lands on
  `main`, release the next `-lazurio.N`.
- Never release upstream's `main`, a canary (`vX.Y.Z+canary.…`), a release
  candidate (`rc.…`) or anything other than the tip of `main`.

## Following upstream

The daily workflow `Lazurio upstream watch` opens one tracking issue,
`Rebuild on upstream Hermes Agent vX.Y.Z`, when upstream has a stable release
newer than `UPSTREAM_TAG` on `main`, and comments on it once per newer tag.
The Steward takes the issue; the steps are below.

### Fast-forward (only with an empty overlay)

When `main` carries no overlay commit and the new upstream commit descends
from `main`, `main` moves by a plain fast-forward, without any rewrite:

```bash
NEW_TAG=vX.Y.Z
NEW_SHA="$(git ls-remote https://github.com/NousResearch/hermes-agent.git "refs/tags/$NEW_TAG^{}" | cut -f1)"
git fetch origin main
git fetch https://github.com/NousResearch/hermes-agent.git "refs/tags/$NEW_TAG:refs/tags/upstream-$NEW_TAG"
test "$(git rev-parse "refs/tags/upstream-$NEW_TAG^{commit}")" = "$NEW_SHA"
git merge-base --is-ancestor origin/main "$NEW_SHA"
git push origin "$NEW_SHA:refs/heads/main"            # no --force: a fast-forward or nothing
```

Open a pull request whose head is exactly `$NEW_SHA` first (GitHub's buttons
cannot fast-forward without rewriting the commits); the push above, by the
Organization Admin, marks it merged. Push that head as a branch with
`git -c push.negotiate=true push origin "$NEW_SHA:refs/heads/lazurio/upstream-$NEW_TAG"`:
the negotiation finds upstream's objects in the fork network, so nothing is
uploaded (a blobless clone would otherwise download and upload every new
blob). This is how `main` moved from `v0.22.2` to `v0.22.4`.

### Rebuild (the overlay is not empty)

`main` is replaced, never merged with upstream: the old `main` is not merged
into the candidate or replayed wholesale.

1. **Inventory.** In the tracking issue, decide for each overlay row: keep,
   rebuild on a moved upstream seam, shrink (upstream now covers part of it)
   or remove (upstream covers it). Default is remove; a kept row needs
   current upstream evidence that it is still missing. What upstream changed
   in the overlay's files and where they conflict:

   ```bash
   OLD_SHA="$(git show origin/main:.github/workflows/lazurio-fork-ci.yml | sed -n 's/^  UPSTREAM_SHA: //p')"
   overlay_files="$(mktemp)"
   git diff --name-only "$OLD_SHA" origin/main > "$overlay_files"
   git diff --name-status --no-renames "$OLD_SHA" "$NEW_SHA" | awk -F'\t' 'NR == FNR { o[$0]; next } $2 in o' "$overlay_files" -
   git merge-tree --write-tree --name-only --merge-base="$OLD_SHA" "$NEW_SHA" origin/main | awk 'NR == 1 { next } /^$/ { exit } { print }'
   ```

   The Organization Admin approves each decision that changes behaviour for
   people or other systems, or adds a file to the allowlist.
2. **Candidate.** Branch `lazurio/rebuild-$NEW_TAG` from `$NEW_SHA` exactly.
   Replay the kept rows in the table's order, one commit per row, by intent
   (`git diff --binary "$OLD_SHA" origin/main -- <row's files> | git apply --3way --index`,
   or a clean `git cherry-pick`), the release row last.
3. **Pins.** In `lazurio-fork-ci.yml` set `UPSTREAM_TAG` and `UPSTREAM_SHA`;
   update the table [Upstream base](#upstream-base), the overlay table, the
   allowlist (remove files the overlay no longer changes) and the test gate.
4. **CI and review.** Open the pull request and dispatch CI on the branch: a
   rebuild never contains the old `main`, so GitHub reports a conflict and
   starts no `pull_request` run for it:

   ```bash
   gh workflow run lazurio-fork-ci.yml --repo Lazurio/hermes-agent --ref "lazurio/rebuild-$NEW_TAG"
   ```

   The Steward reviews the exact head; the pull request is never merged with
   a button.
5. **Capture.** Before `main` moves, the current `main` must be captured by a
   published **immutable** `v…-lazurio.N` release, on the commit itself or on
   an ancestor that differs from it only under `docs/`. If it is not, release
   the old `main` first.

   ```bash
   expected_old_main="$(git ls-remote https://github.com/Lazurio/hermes-agent.git refs/heads/main | cut -f1)"
   candidate_head="$(gh pr view "lazurio/rebuild-$NEW_TAG" --repo Lazurio/hermes-agent --json headRefOid --jq .headRefOid)"
   capture="$(gh api repos/Lazurio/hermes-agent/releases/latest --jq .tag_name)"
   capture_sha="$(gh api "repos/Lazurio/hermes-agent/commits/$capture" --jq .sha)"
   git merge-base --is-ancestor "$capture_sha" "$expected_old_main"
   git diff --quiet "$capture_sha" "$expected_old_main" -- . ':(exclude)docs/'
   test "$(gh api "repos/Lazurio/hermes-agent/releases/tags/$capture" --jq .immutable)" = true
   ```

6. **Swap.** Only the Organization Admin, or their Task Agent on an explicit
   instruction bound to both SHAs, replaces `main`, with the exact lease. The
   Human and Machine Organization's rules must name this repository among the
   rolling forks first ([One-time setup](#one-time-setup), step 1); until then
   the swap is blocked.

   ```bash
   git push --force-with-lease="refs/heads/main:$expected_old_main" \
     origin "$candidate_head:refs/heads/main"
   ```

   A failed lease means a concurrent change: audit it, never retry
   automatically. GitHub then shows the rebuild pull request as merged.
7. **Release** `X.Y.Z-lazurio.1` from the new `main` right away and move the
   Machines pin (see [How Lazurio installs a release](#how-lazurio-installs-a-release)).
   Close the tracking issue with links to the release and the pull request.

## Testing before a release

`Lazurio Fork CI` runs on every pull request, on `main` and inside every
release run, on the exact source commit:

- **Overlay guard and contract**: the exact upstream tag and commit, no merge
  commit above it, no upstream file deleted, renamed or retyped, content
  changes only in allowlisted upstream files, upstream workflows disabled,
  and `scripts/lazurio_fork_contract_test.py` (which also runs the guard's own
  script against scratch repositories).
- **Upstream and overlay tests**: upstream's own locked toolchain
  (`.github/actions/setup-pm`, extra `acp`, test environment) and upstream's
  runner (`scripts/run_tests.sh`, one process per test file) over a fixed
  gate (upstream's ACP adapter tests, which include its real gateway and ACP
  stdio tests, and upstream's workflow graph test) plus every test file the
  overlay adds or changes. A gate path that disappears fails the step, so a
  rebuild reviews the gate.

Locally, in a checkout with Python 3.11 or newer:

```bash
python3 scripts/lazurio_fork_contract_test.py
scripts/run_tests.sh tests/acp_adapter tests/ci/test_workflow_job_graph.py
```

Upstream marks its real gateway tests `platforms("linux")`, so a macOS run
skips them; CI runs them on Linux.

## Dispatching a release

Only from `main`, and only from the commit at its tip:

```bash
VERSION=0.22.4-lazurio.1
SOURCE_SHA="$(git ls-remote https://github.com/Lazurio/hermes-agent.git refs/heads/main | cut -f1)"
CI="$(gh api "repos/Lazurio/hermes-agent/contents/.github/workflows/lazurio-fork-ci.yml?ref=$SOURCE_SHA" \
  -H 'Accept: application/vnd.github.raw')"
UPSTREAM_TAG="$(sed -n 's/^  UPSTREAM_TAG: //p' <<<"$CI")"
UPSTREAM_SHA="$(sed -n 's/^  UPSTREAM_SHA: //p' <<<"$CI")"

gh workflow run lazurio-release.yml --repo Lazurio/hermes-agent --ref main \
  -f version="$VERSION" -f source_sha="$SOURCE_SHA" \
  -f upstream_tag="$UPSTREAM_TAG" -f upstream_sha="$UPSTREAM_SHA"
gh run list --repo Lazurio/hermes-agent --workflow lazurio-release.yml --limit 1
```

`Lazurio Hermes Agent Release` then:

1. **Verify source and version** checks the inputs, that `source_sha` is the
   tip of `main`, stands on the exact upstream tag (whose peeled commit is
   `upstream_sha`, as the source's own CI pins it) with no merge commit above
   it, that no upstream workflow is enabled, that neither tag nor release
   exists and that the version is the highest.
2. **Fork CI on the exact source** runs `Lazurio Fork CI` on `source_sha`, so
   the release run carries its own test evidence.
3. **Publish release** waits for approval in `lazurio-hermes-agent-release`.
   After approval it checks again, before any write, that `source_sha` is
   still the tip of `main` and that no upstream workflow became enabled; adds
   upstream's exact tag object for `upstream_tag` if the fork lacks it (and
   stops if the fork's tag differs); creates the tag `v<version>` on
   `source_sha` with the release App token; and publishes the GitHub Release
   `Lazurio Hermes Agent v<version>` as `latest`, with the source, the
   upstream base, the overlay commits, the install command and the link to
   the run in its notes. It never overwrites a tag or a release.

## Approval

When the first two jobs are green, the Steward asks Matěj to approve and
sends the run link; he approves in GitHub (run → Review deployments →
`lazurio-hermes-agent-release` → Approve). The environment has
`prevent_self_review`, so the Steward dispatches and the Admin approves. If
the release should not go out, the Admin rejects it and nothing is published.

## Verifying a published release

```bash
VERSION=0.22.4-lazurio.1
gh release view "v$VERSION" --repo Lazurio/hermes-agent --json tagName,isImmutable,body
gh api "repos/Lazurio/hermes-agent/commits/v$VERSION" --jq .sha            # the pinned commit
gh api repos/Lazurio/hermes-agent/releases/latest --jq .tag_name
```

## Tag protection and the release App

A tag ruleset `Protect Lazurio channel tags` on `refs/tags/v*-lazurio.*`
(creation, update, deletion) has the GitHub App `Lazurio Hermes Agent
Release` (contents write, metadata read, installed only on
`Lazurio/hermes-agent`) as its only bypass actor. Its ID is the environment
variable `LAZURIO_RELEASE_APP_ID` and its private key the environment secret
`LAZURIO_RELEASE_APP_PRIVATE_KEY`, both in `lazurio-hermes-agent-release`, so
only an approved run can mint the token and only for this repository. Nobody,
the Steward and the Admin included, creates a channel tag by hand. Releases
are immutable (repository setting), which the capture rule of a rebuild
relies on.

## One-time setup

The fork has no Lazurio ruleset, environment, App or workflow run yet;
GitHub Actions and Issues are off. Only the Organization Admin can do these
steps, in this order:

1. **Allow the patch-stack for this repository.** The Human and Machine
   `AGENTS.md` (section 6) and its skill `upstream-rebase-fork` name the forks
   whose `main` may be replaced by a rebuild swap; adding
   `Lazurio/hermes-agent` there is a reviewed change of that Organization
   ([HumanAndMachine-ai/HumanAndMachine-ai_GEN3#199](https://github.com/HumanAndMachine-ai/HumanAndMachine-ai_GEN3/pull/199)).
   A fast-forward needs no exception; a swap waits for it.
2. **Fast-forward `main` to the base** (done once for `v0.22.4`, see above)
   and merge the distribution pull request by rebase.
3. **Actions.** Enable Actions with GitHub-owned actions only, then disable
   every upstream workflow at once (command in
   [Automation boundary](#automation-boundary)). Upstream workflows that
   register later are disabled the same way.
4. **Issues** on, for the watch workflow's tracking issue and for bug
   reports (`.github/README.md`).
5. **Release App, environment and tag ruleset** as described above;
   environment `lazurio-hermes-agent-release` with required reviewer Matěj,
   `prevent_self_review`, deployments from `main` only and the variable
   `LAZURIO_HERMES_AGENT_RELEASE_CONTROL=reviewed-v1`. Immutable releases on.
6. **Protect `main`**: a branch ruleset with pull requests required, merge
   method rebase only, linear history, the required checks
   `Overlay guard and contract` and `Upstream and overlay tests`, and
   `non_fast_forward` with the `OrganizationAdmin` bypass the swap uses; in
   the repository settings allow rebase merging only.
7. **First release** `0.22.4-lazurio.1`, dispatched by the Steward and
   approved by the Admin, after immutable releases are on: it is the capture
   the first rebuild swap relies on.

## Automation boundary

Upstream workflow files stay in the tree unchanged, so a rebuild brings no
workflow diff, but they must be **disabled** in this repository's Actions
settings: they cut and publish upstream releases, Docker images and the docs
site, run on upstream's own runners and need upstream secrets. Enabled here,
a push to `main` or a tag would set them off. `Lazurio Fork CI` and the
release both fail while any of them is enabled.

```bash
gh workflow list --repo Lazurio/hermes-agent --all --json id,path,state \
  | jq -r '.[] | select(.path | test("lazurio-") | not) | select(.state == "active") | .id' \
  | xargs -n1 gh workflow disable --repo Lazurio/hermes-agent
```

GitHub registers a workflow only at its first triggering event, and only a
registered workflow can be disabled; after a push that brings a new upstream
workflow, cancel its run and run the command again. Only `lazurio-fork-ci.yml`
(read-only), `lazurio-upstream-watch.yml` (writes only issues of this
repository, with `GITHUB_TOKEN`) and the manually dispatched
`lazurio-release.yml` stay active.

## No rollback: repair forward

Lazurio has no rollback (decision 0166). A broken release is repaired
forward: fix or revert the commit on `main`, release the next, higher
`-lazurio.N` and move the Machines pin. Do not pin an Osobní Prostředí back
to an older release; Hermes migrates its state forward.

## Do not

- Delete or rewrite a published release or its tag; a wrong release is
  replaced by a higher version.
- Publish by hand (`gh release create`), use the release App outside the
  workflow or copy its private key.
- Force-push `main` outside step 6 of the rebuild.
- Merge a rebuild pull request with a button, or merge upstream into `main`.
- Enable or run upstream workflows.

## Rebuild issue

The watch workflow opens the tracking issue from the template between the
markers below; to open it by hand:

```bash
body="$(mktemp)"
gh api 'repos/Lazurio/hermes-agent/contents/docs/operations/lazurio-fork-release.md?ref=main' \
  -H 'Accept: application/vnd.github.raw' |
  sed -n '/^<!-- rebuild-issue:start -->$/,/^<!-- rebuild-issue:end -->$/p' > "$body"
gh issue create --repo Lazurio/hermes-agent --title "Rebuild on upstream Hermes Agent vX.Y.Z" --body-file "$body"
```

<!-- rebuild-issue:start -->

Rebuild of the Lazurio overlay on a new stable release of upstream Hermes
Agent. Steps:
[Following upstream](https://github.com/Lazurio/hermes-agent/blob/main/docs/operations/lazurio-fork-release.md#following-upstream).
This issue is public: no names of client Organizations, people or private
hosts, only Environments and roles.

**Inputs**

- New base: `vX.Y.Z` = `<peeled commit>`
- Old base: `<UPSTREAM_TAG>` = `<UPSTREAM_SHA>`; `main` = `<expected_old_main>`
- Capture of `main`: `v…-lazurio.N` = `<commit>` (immutable)
- Candidate: branch `lazurio/rebuild-vX.Y.Z`, pull request #…, head `<candidate_head>`

**Steps**

- [ ] Inventory: one row per overlay row below, each decided.
- [ ] Candidate: one commit per row in the overlay table's order, the release row last.
- [ ] Pins, allowlist and test gate updated; `Lazurio Fork CI` green on the exact head.
- [ ] Review by the Steward on the exact head.
- [ ] Capture of the old `main` verified; `main` swapped (`expected_old_main` → `candidate_head`).
- [ ] `X.Y.Z-lazurio.1` released and the Machines pin moved.

**Decisions per overlay row** (keep, rebuild, shrink or remove)

| Row | What upstream changed in its files | Decision | Reason | Approved by |
| --- | --- | --- | --- | --- |
| `release: Lazurio distribution on upstream …` | | | | |

**Open questions and blockers**

- …

<!-- rebuild-issue:end -->
