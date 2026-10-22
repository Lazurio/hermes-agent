# Lazurio Hermes Agent

Lazurio Hermes Agent is a modified distribution of [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research for [Lazurio](https://github.com/Lazurio). It runs Buddy, the user's personal
agent, inside the user's Lazurio Osobní Prostředí (personal Environment), where Buddy talks to the
user through Lazurio T3 Code over the Agent Client Protocol (ACP).

It is not an official Hermes Agent release, and the Hermes Agent team does not support it. Report
problems with it here, not upstream (see [Reporting issues](#reporting-issues)).

## How it differs from Hermes Agent

Hermes Agent itself is unchanged apart from the Lazurio overlay. Today the overlay is only the
distribution: this README, the [release runbook](../docs/operations/lazurio-fork-release.md) and
the `lazurio-*` workflows that test and release it. Every change Lazurio makes to Hermes is listed
in the runbook's overlay table, with why Lazurio needs it and what upstream has to ship before it
goes away.

## Tracking upstream

`main` is an exact upstream stable release tag with the Lazurio overlay commits on top, and nothing
else, the same model as Lazurio's T3 Code and MausBot distributions. A daily check opens an issue
in this repository when upstream publishes a newer stable release; the overlay is then rebuilt on
that release, one commit per change, and every change upstream now covers is dropped. Changes that
would help everyone are offered upstream.

## Releases

Releases are on this repository's [Releases](https://github.com/Lazurio/hermes-agent/releases) page:
`X.Y.Z-lazurio.N`, tagged `vX.Y.Z-lazurio.N`, where `X.Y.Z` is the upstream release it is built on
and `N` counts the releases on that base. A release is a protected tag and release notes, with no
packages: Lazurio installs Hermes the way upstream recommends, with the official installer and this
repository's source at the release's exact commit:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh |
  HERMES_REPO_URL=https://github.com/Lazurio/hermes-agent.git bash -s -- --commit <commit>
```

Lazurio Machines pin that commit for each Osobní Prostředí; upstream's own update channel does not
follow this repository.

## Reporting issues

Report problems with Lazurio Hermes Agent in this repository's
[issues](https://github.com/Lazurio/hermes-agent/issues), especially anything about Buddy, ACP and
the other changes in the overlay. Please don't report them to the Hermes Agent team. If a problem
also happens in upstream Hermes Agent, report it upstream too.

## Credit and license

Lazurio Hermes Agent is built on [Hermes Agent](https://github.com/NousResearch/hermes-agent) by
Nous Research and its contributors. Hermes Agent is licensed under the MIT License, and so are the
Lazurio changes: see [LICENSE](../LICENSE), which keeps upstream's copyright notice. Upstream's own
README is [README.md](../README.md).
