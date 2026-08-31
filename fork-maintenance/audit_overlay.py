#!/usr/bin/env python3
"""Fail closed unless HEAD is the declared Lazurio overlay on an upstream release."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "fork-maintenance" / "patches.v1.json"
NON_RUNTIME_PREFIXES = (".github/", "fork-maintenance/")
NON_RUNTIME_FILES = {"FORK_POLICY.md"}


class AuditError(RuntimeError):
    pass


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode != 0:
        raise AuditError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def load_manifest() -> dict:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(
            f"cannot read {MANIFEST_PATH.relative_to(ROOT)}: {exc}"
        ) from exc
    require(
        manifest.get("schema_version") == "lazurio.hermes-fork-patches.v1",
        "unsupported manifest schema",
    )
    require(
        manifest.get("repository", {}).get("model")
        == "single-maintainer-release-mirror",
        "unexpected maintenance model",
    )
    return manifest


def audit() -> None:
    manifest = load_manifest()
    repository = manifest["repository"]
    observed_repository = os.environ.get("GITHUB_REPOSITORY")
    if observed_repository:
        require(
            observed_repository == repository["canonical"],
            f"workflow is running in {observed_repository}, not {repository['canonical']}",
        )

    upstream = manifest["upstream"]
    bridge = manifest["bridge"]
    upstream_commit = upstream["commit"]
    upstream_tree = upstream["tree"]
    bridge_commit = bridge["commit"]

    require(
        git("cat-file", "-t", upstream_commit) == "commit",
        "declared upstream commit is unavailable",
    )
    require(
        git("rev-parse", f"{upstream_commit}^{{tree}}") == upstream_tree,
        "declared upstream tree does not match its commit",
    )
    require(
        git("cat-file", "-t", bridge_commit) == "commit",
        "declared bridge commit is unavailable",
    )

    bridge_record = git("rev-list", "--parents", "-n", "1", bridge_commit).split()
    require(
        bridge_record[1:] == bridge["parent_order"],
        "bridge parent order differs from the manifest",
    )
    require(
        bridge["parent_order"] == [upstream_commit, bridge["previous_fork_head"]],
        "bridge parents do not name upstream first and previous fork head second",
    )
    require(
        git("rev-parse", f"{bridge_commit}^{{tree}}") == upstream_tree,
        "bridge is not tree-neutral against upstream",
    )
    require(
        bridge.get("tree_matches_upstream") is True,
        "manifest must assert the tree-neutral bridge invariant",
    )

    head = git("rev-parse", "HEAD")
    require(
        git("merge-base", bridge_commit, head) == bridge_commit,
        "HEAD is not based on the declared bridge",
    )
    require(
        not git("rev-list", "--merges", f"{bridge_commit}..{head}"),
        "overlay contains a merge commit",
    )

    patches = manifest.get("patches")
    require(
        isinstance(patches, list) and patches,
        "manifest needs at least one patch declaration",
    )
    ids: set[str] = set()
    declared_paths: set[str] = set()
    for patch in patches:
        patch_id = patch.get("id")
        require(
            isinstance(patch_id, str) and patch_id and patch_id not in ids,
            "patch ids must be non-empty and unique",
        )
        ids.add(patch_id)
        require(
            patch.get("status") == "active",
            f"live manifest contains non-active patch {patch_id}",
        )
        require(
            isinstance(patch.get("runtime_delta"), bool),
            f"patch {patch_id} must declare runtime_delta",
        )
        for field in ("upstream_gap", "retirement_probe", "retirement_condition"):
            require(
                isinstance(patch.get(field), str) and patch[field].strip(),
                f"patch {patch_id} lacks {field}",
            )
        paths = patch.get("paths")
        require(isinstance(paths, list) and paths, f"patch {patch_id} has no paths")
        for path in paths:
            require(
                isinstance(path, str)
                and path
                and not path.startswith("/")
                and ".." not in Path(path).parts,
                f"patch {patch_id} has unsafe path {path!r}",
            )
            require(
                path not in declared_paths,
                f"path {path} is owned by more than one patch",
            )
            if not patch["runtime_delta"]:
                require(
                    path in NON_RUNTIME_FILES or path.startswith(NON_RUNTIME_PREFIXES),
                    f"runtime-neutral patch {patch_id} declares runtime path {path}",
                )
            declared_paths.add(path)

    changed_paths = set(
        filter(None, git("diff", "--name-only", bridge_commit, head).splitlines())
    )
    require(
        changed_paths == declared_paths,
        f"overlay paths differ: undeclared={sorted(changed_paths - declared_paths)}, stale={sorted(declared_paths - changed_paths)}",
    )

    print(
        f"OK: {head} is a {len(patches)}-patch declared overlay on {upstream['release_tag']} ({upstream_commit})."
    )


def main() -> int:
    try:
        audit()
    except (AuditError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
