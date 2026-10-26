"""Contract of the Lazurio distribution files (standard library only).

Run from the repository root:  python3 scripts/lazurio_fork_contract_test.py -v

It holds together what CI, the release and the runbook say about the rolling distribution
patch-stack: the exact upstream base, the allowlist of upstream files the overlay may change, the
gated release that never overwrites, and the watch that writes only issues.
Runbook: docs/operations/lazurio-fork-release.md.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
# Upstream's stable tag shape (hermes_cli/update_channel.py STABLE_TAG_RE).
STABLE_TAG = r"v(?:0|[1-9]\d{0,2})\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


CI = read(".github/workflows/lazurio-fork-ci.yml")
RELEASE = read(".github/workflows/lazurio-release.yml")
WATCH = read(".github/workflows/lazurio-upstream-watch.yml")
RUNBOOK = read("docs/operations/lazurio-fork-release.md")
README = read(".github/README.md")


def pins() -> tuple[str, str]:
    tag = re.search(rf"^  UPSTREAM_TAG: ({STABLE_TAG})\n", CI, re.MULTILINE)
    sha = re.search(r"^  UPSTREAM_SHA: ([0-9a-f]{40})\n", CI, re.MULTILINE)
    assert tag and sha, "lazurio-fork-ci.yml pins UPSTREAM_TAG and UPSTREAM_SHA"
    return tag.group(1), sha.group(1)


def allowlist() -> list[str]:
    block = re.search(r"allowed_upstream_changes=\(\n(.*?)^\s*\)\n", CI, re.DOTALL | re.MULTILINE)
    assert block, "the guard declares allowed_upstream_changes"
    return [line.strip() for line in block.group(1).splitlines() if line.strip()]


def guard_script(allowed: list[str]) -> str:
    """The guard step's own script from lazurio-fork-ci.yml, with *allowed* as its allowlist."""
    step = re.search(r"- name: Overlay changes only allowed upstream files\n {8}run: \|\n((?: {10}.*\n|\n)+)", CI)
    assert step, "the guard step runs a script"
    script = textwrap.dedent(step.group(1))
    listed = "allowed_upstream_changes=(\n" + "".join(f"  {path}\n" for path in allowed) + ")\n"
    script, count = re.subn(r"(?ms)^allowed_upstream_changes=\(\n.*?^\)\n", lambda _: listed, script)
    assert count == 1, "the guard script declares its allowlist once"
    return script


def _git(work: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=work,
                          env=env, check=True, capture_output=True, text=True, encoding="utf-8", timeout=60).stdout.strip()


def _retype(work: Path, path: str) -> None:
    (work / path).unlink()
    os.symlink("README.md", work / path)


# The guard scenarios' changes to a scratch upstream file: verb -> what it does to the file.
CHANGES = {
    "edit": lambda work, path: (work / path).write_text("lazurio\n", encoding="utf-8"),
    "add": lambda work, path: (work / path).write_text("lazurio\n", encoding="utf-8"),
    "delete": lambda work, path: _git(work, "rm", "-q", path),
    "rename": lambda work, path: _git(work, "mv", path, f"lazurio-{path}"),
    "retype": _retype,
}


def _run_guard(work: Path, allowed: list[str], change: str) -> subprocess.CompletedProcess:
    _git(work, "init", "-q")
    for name, text in (("README.md", "upstream\n"), ("SOUL.md", "soul\n"), ("tool.sh", "echo\n")):
        (work / name).write_text(text, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "upstream")
    upstream = _git(work, "rev-parse", "HEAD")
    for step in change.split("; "):
        verb, path = step.split(" ", 1)
        CHANGES[verb](work, path)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "overlay")
    env = {**os.environ, "UPSTREAM_SHA": upstream, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    script = guard_script(allowed)
    return subprocess.run(["bash", "-c", script], cwd=work, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, check=False, env=env)


class ForkCI(unittest.TestCase):
    def test_ci_is_read_only_and_pins_the_exact_upstream_base(self):
        self.assertRegex(CI, r"(?m)^permissions:\n  contents: read\n  id-token: none\n")
        self.assertNotRegex(CI, r"contents: write|packages: write|id-token: write|secrets\.")
        tag, sha = pins()
        self.assertRegex(CI, r"(?m)^  UPSTREAM_REPOSITORY: https://github\.com/NousResearch/hermes-agent\.git\n")
        # The runbook's table names the same base, and the commit is the tag's peeled one.
        self.assertIn(f"| Stable tag | `{tag}` ", RUNBOOK)
        self.assertIn(f"| Commit (peeled) | `{sha}` |", RUNBOOK)
        self.assertIn('test "$(git rev-parse "refs/tags/upstream-$UPSTREAM_TAG^{commit}")" = "$UPSTREAM_SHA"', CI)
        self.assertIn('git merge-base --is-ancestor "$UPSTREAM_SHA" HEAD', CI)
        self.assertIn('test "$(git rev-list --merges "$UPSTREAM_SHA..HEAD" --count)" = 0', CI)
        self.assertIn("Upstream workflows stay disabled", CI)
        # The release runs this CI on its exact source.
        self.assertRegex(CI, r"workflow_call:\n    inputs:\n      source_sha:\n")
        self.assertIn("SOURCE_REF: ${{ inputs.source_sha || github.event.pull_request.head.sha || github.sha }}", CI)

    def test_overlay_may_change_only_listed_upstream_files(self):
        lines = allowlist()
        entries = [line for line in lines if line and not line.startswith("#")]
        if entries:
            self.assertRegex(lines[0], r"^# \S", "the allowlist opens with a reason")
        self.assertEqual(len(entries), len(set(entries)), "allowlist entries are unique")
        for entry in entries:
            self.assertRegex(entry, r"^[\w./-]+\.[A-Za-z0-9]+$", entry)
            self.assertNotRegex(entry, r"[*?\[\]{}]|\.\.|/$", entry)
            self.assertFalse(entry.startswith(".github/workflows/"),
                             "upstream workflows stay unchanged; they are disabled in settings")
            self.assertNotIn("lazurio", entry.lower(), "Lazurio files need no allowlist entry")

    def test_the_guard_refuses_what_the_allowlist_does_not_allow(self):
        """The guard step's exact script against a scratch repository: an allowlisted file may change
        its content, but no upstream file may be deleted, renamed or retyped, listed or not."""
        cases = [
            ("listed content change and a new file", ["README.md"], "edit README.md; add lazurio_new.py", 0),
            ("only new files, empty allowlist", [], "add lazurio_new.py", 0),
            ("unlisted content change", ["README.md"], "edit SOUL.md", 1),
            ("listed file deleted", ["README.md"], "delete README.md", 1),
            ("listed file renamed", ["README.md"], "rename README.md", 1),
            ("listed file retyped", ["tool.sh"], "retype tool.sh", 1),
        ]
        for name, allowed, change, expected in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as work:
                status = _run_guard(Path(work), allowed, change)
                self.assertEqual(status.returncode, expected, f"{name}: {status.stderr}")

    def test_the_test_gate_runs_upstream_tests_with_upstreams_toolchain(self):
        self.assertIn("uses: ./.github/actions/setup-pm", CI)
        self.assertIn("""extras: '["acp"]'""", CI)
        self.assertIn("test-environment: 'true'", CI)
        self.assertIn('scripts/run_tests.sh "${paths[@]}"', CI)
        gate = re.search(r"gate=\(\n(.*?)^\s*\)\n", CI, re.DOTALL | re.MULTILINE)
        self.assertTrue(gate, "the test step declares its gate")
        paths = [line.strip() for line in gate.group(1).splitlines() if line.strip()]
        self.assertIn("tests/acp_adapter", paths)
        for path in paths:
            self.assertTrue((ROOT / path).exists(), f"gate path {path} exists")
        self.assertIn("is gone or holds no tests", CI)


class Release(unittest.TestCase):
    def test_release_is_manual_gated_and_never_overwrites(self):
        triggers = RELEASE[RELEASE.index("\non:"):RELEASE.index("\npermissions:")]
        self.assertIn("workflow_dispatch:", triggers)
        self.assertNotRegex(triggers, r"(?m)^  (push|schedule|release|pull_request\w*|workflow_call):")
        self.assertRegex(RELEASE, r"(?m)^permissions:\n  contents: read\n  id-token: none\n")
        self.assertIn("if: github.repository == 'Lazurio/hermes-agent'", RELEASE)
        self.assertIn("environment: lazurio-hermes-agent-release", RELEASE)
        self.assertIn("RELEASE_CONTROL: ${{ vars.LAZURIO_HERMES_AGENT_RELEASE_CONTROL }}", RELEASE)
        self.assertIn('test "$RELEASE_CONTROL" = "reviewed-v1"', RELEASE)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', RELEASE)
        self.assertIn(r'[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+-lazurio\.[1-9][0-9]*$ ]]', RELEASE)
        self.assertIn('[[ "$VERSION" == "${UPSTREAM_TAG#v}-lazurio."* ]]', RELEASE)
        self.assertIn("RELEASE_TAG: v${{ inputs.version }}", RELEASE)
        self.assertIn('test "$(git rev-parse refs/remotes/origin/main)" = "$SOURCE_SHA"', RELEASE)
        self.assertIn('test "$(git rev-list --merges "$UPSTREAM_SHA..$SOURCE_SHA" --count)" = 0', RELEASE)
        # The base is the one the source's own CI pins (an older stable tag is an ancestor too).
        self.assertIn("""test "$(sed -n 's/^  UPSTREAM_TAG: //p' .github/workflows/lazurio-fork-ci.yml)" = "$UPSTREAM_TAG\"""", RELEASE)
        self.assertIn("""test "$(sed -n 's/^  UPSTREAM_SHA: //p' .github/workflows/lazurio-fork-ci.yml)" = "$UPSTREAM_SHA\"""", RELEASE)
        self.assertIn("is not newer than the published", RELEASE)
        self.assertIn("Refuse while an upstream workflow is enabled", RELEASE)
        self.assertIn("Upstream workflows became enabled since verification", RELEASE)
        # The release runs Lazurio Fork CI on its exact source before anyone approves it.
        self.assertRegex(RELEASE, r"uses: \./\.github/workflows/lazurio-fork-ci\.yml\n    with:\n      source_sha: \$\{\{ inputs\.source_sha \}\}")
        self.assertIn("needs: [verify, ci]", RELEASE)
        # After approval: live main must still be the source, and only the release App tags.
        self.assertIn("main moved away from $SOURCE_SHA since dispatch", RELEASE)
        self.assertIn("vars.LAZURIO_RELEASE_APP_ID != ''", RELEASE)
        self.assertIn("secrets.LAZURIO_RELEASE_APP_PRIVATE_KEY != ''", RELEASE)
        self.assertRegex(RELEASE, r"owner: Lazurio\n {10}repositories: hermes-agent\n")
        self.assertIn("GH_TOKEN: ${{ steps.release_app.outputs.token }}", RELEASE)
        self.assertIn('--method POST "repos/$GITHUB_REPOSITORY/git/refs"', RELEASE)
        self.assertIn("--verify-tag", RELEASE)
        self.assertNotRegex(RELEASE, r"--force|--clobber|git push")

    def test_the_fork_carries_upstreams_exact_tag_object(self):
        self.assertIn('git ls-remote "$UPSTREAM_REPOSITORY" "refs/tags/$UPSTREAM_TAG"', RELEASE)
        self.assertIn('-f ref="refs/tags/$UPSTREAM_TAG" -f sha="$UPSTREAM_TAG_OBJECT"', RELEASE)
        self.assertIn("not upstream's tag object", RELEASE)

    def test_a_release_is_a_tag_and_notes_installed_from_the_exact_source(self):
        # Decision 0210 point 2: the official installer, this fork's source, the exact commit.
        install = "HERMES_REPO_URL=https://github.com/Lazurio/hermes-agent.git bash -s -- --commit"
        self.assertIn(install, RUNBOOK)
        self.assertIn(install, README)
        self.assertIn("HERMES_REPO_URL=https://github.com/$GITHUB_REPOSITORY.git bash -s -- --commit $SOURCE_SHA", RELEASE)
        self.assertNotRegex(RELEASE, r"upload-artifact|attest|docker|ghcr\.io|pypi|twine")
        self.assertIn("""git log --no-merges --reverse --pretty='format:- %s (%h)' "$UPSTREAM_SHA..$SOURCE_SHA\"""", RELEASE)


class Watch(unittest.TestCase):
    def test_upstream_watch_writes_only_issues(self):
        self.assertRegex(WATCH, r"on:\n  schedule:\n    - cron: \"[0-9 *]+\"\n  workflow_dispatch:\n")
        self.assertRegex(WATCH, r"(?m)^permissions:\n  contents: read\n  issues: write\n\n")
        self.assertIn("if: github.repository == 'Lazurio/hermes-agent'", WATCH)
        self.assertNotRegex(WATCH, r"contents: write|git push|gh release create|workflow run")
        self.assertIn("raw .github/workflows/lazurio-fork-ci.yml | sed -n 's/^  UPSTREAM_TAG: //p'", WATCH)
        self.assertIn("--exclude-drafts --exclude-pre-releases", WATCH)

    def test_the_watch_and_the_runbook_agree_on_the_tracking_issue(self):
        title = "Rebuild on upstream Hermes Agent "
        self.assertIn(f'startswith("{title}")', WATCH)
        self.assertIn(f'--title "{title}$latest"', WATCH)
        self.assertIn(f"`{title}vX.Y.Z`", RUNBOOK)
        self.assertEqual(RUNBOOK.count("\n<!-- rebuild-issue:start -->\n"), 1)
        self.assertEqual(RUNBOOK.count("\n<!-- rebuild-issue:end -->\n"), 1)
        self.assertLess(RUNBOOK.index("<!-- rebuild-issue:start -->"), RUNBOOK.index("<!-- rebuild-issue:end -->"))


class Workflows(unittest.TestCase):
    def test_lazurio_workflows_use_only_pinned_github_owned_actions(self):
        for path in sorted(WORKFLOWS.glob("lazurio-*.yml")):
            for uses in re.findall(r"(?m)^\s*(?:-\s+)?uses:\s*(\S+)", path.read_text(encoding="utf-8-sig")):
                if uses.startswith("./"):
                    continue
                self.assertRegex(uses, r"^actions/[\w.-]+@[0-9a-f]{40}$", f"{path.name}: {uses}")

    def test_the_overlay_table_lists_the_distribution_row(self):
        tag, _ = pins()
        self.assertIn(f"| `release: Lazurio distribution on upstream {tag}` |", RUNBOOK)
        self.assertIn("docs/operations/lazurio-fork-release.md", README)


class Overlay(unittest.TestCase):
    def test_the_pairing_row_names_its_key_placeholders_capability_and_hooks(self):
        """The pairing reply's row: the key and the capability the code names are the ones the runbook
        documents, and its upstream hooks are allowlisted and call the Lazurio module."""
        code = read("gateway/lazurio_pairing_message.py")
        key = re.search(r'^CONFIG_KEY = "([a-z_]+)"$', code, re.MULTILINE)
        capability = re.search(r'^CAPABILITY = "(lazurio-[a-z-]+-v\d+)"$', code, re.MULTILINE)
        assert key and capability, "gateway/lazurio_pairing_message.py names its key and its capability"
        subject = "pairing: a configured text replaces the reply that gives an unknown sender its code"
        row = re.search(rf"^\| `{re.escape(subject)}` \|(.+)\|$", RUNBOOK, re.MULTILINE)
        assert row, "the overlay table lists the pairing row"
        for name in (key.group(1), capability.group(1), "{code}", "{validity}", "gateway/run_inbound.py",
                     "tests/gateway/test_lazurio_pairing_message.py"):
            self.assertIn(name, row.group(1))
        self.assertIn("gateway/run_inbound.py", allowlist())
        inbound = read("gateway/run_inbound.py")
        self.assertIn("from gateway.lazurio_pairing_message import pairing_reply as lazurio_pairing_reply", inbound)
        # The configured reply comes first; upstream's code reply stays the fallback.
        self.assertIn("reply = lazurio_pairing_reply(code) or pairing_code_reply(", inbound)
        self.assertIn("from gateway.lazurio_pairing_message import CAPABILITY", read("gateway/run_runtime.py"))


if __name__ == "__main__":
    unittest.main()
