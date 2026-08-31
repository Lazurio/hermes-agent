# Lazurio Hermes fork policy

`Lazurio/hermes-agent` is a single-maintainer release mirror used as an exact,
auditable runtime dependency by Lazurio. It is not a second Hermes development
project. Matěj Suchánek owns its maintenance; product development and broadly
useful fixes belong upstream in `NousResearch/hermes-agent`.

## Invariants

- Refresh only from an upstream Hermes release tag, never from a floating
  branch or an arbitrary snapshot of upstream `main`.
- Preserve the upstream release tree byte-for-byte in a tree-neutral bridge
  commit. Its first parent is the upstream release commit and its second parent
  is the previous Lazurio fork head.
- Apply only the small, ordered patch stack declared in
  `fork-maintenance/patches.v1.json` after that bridge.
- Pin consumers to an exact fork commit. A branch, tag, or version label is not
  sufficient supply-chain identity.
- Never rewrite a published fork head or tag. Roll back a consumer to its last
  known-good exact pin, then repair forward in a maintenance pull request.

The bridge makes two facts independently inspectable: the imported release
tree is exactly upstream, and every Lazurio-owned difference is later in the
declared overlay. Upstream authors remain represented by their original Git
history. This fork does not copy them into Lazurio's contributor registry.

## Release refresh

1. Fetch the new upstream release tag and record its tag object, peeled commit,
   tree and release page in the manifest. Review the release notes and verify
   the tag through GitHub; verify a cryptographic signature when upstream
   provides one.
2. From the previous fork head, create a bridge whose tree equals the upstream
   release tree, with the upstream commit as first parent and the previous fork
   head as second parent.
3. Before reapplying any Lazurio patch, run its retirement probe against the
   new upstream tree. If upstream now provides equivalent behavior, delete the
   patch. If it provides part of the behavior, narrow the patch and its declared
   paths. Equivalence is decided by behavior and tests, not by similar code.
4. Reapply the remaining patches in manifest order. Each patch must state its
   upstream gap, runtime impact, exact paths and objective retirement condition.
5. Run `python3 fork-maintenance/audit_overlay.py`, the complete upstream test
   suite, and the Lazurio disposable Resident compatibility proof. Only then
   update the exact Hermes pin in Machines and the Lazurio release descriptor.

Normal feature contributions are not accepted on this repository. A useful
Hermes change should be proposed upstream; a Lazurio-only compatibility patch
may live here temporarily only when its boundary and deletion test are explicit.

## Patch retirement rule

Every refresh asks, for every active patch: “What observable capability is
still missing upstream?” If the answer is “none,” keeping the patch is a
failure. Retire it in the same refresh, remove its paths from the overlay and
record the upstream release or test that proved parity. Historical rationale
remains in Git; the live manifest describes only the current overlay.

Runtime-changing patches require a dedicated compatibility test and a named
upstream issue or other public convergence target. The maintenance-policy patch
is deliberately runtime-neutral and may remain while upstream CI assumes a
multi-contributor development repository and NousResearch-owned larger runner
labels. In this mirror those jobs use standard GitHub-hosted runners with
bounded concurrency; upstream keeps its larger runners unchanged.
