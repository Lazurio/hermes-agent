"""Lazurio overlay: ``hermes acp --check`` also checks the MCP SDK.

The MCP servers an editor passes to its sessions are attached through the ``mcp`` SDK; without it
upstream skips them with a debug line. The check now fails then, with the command that installs it,
and passes as before when the SDK imports. Overlay row: ``acp: hermes acp --check also checks the
MCP SDK`` (docs/operations/lazurio-fork-release.md).
"""
import sys

import pytest

from acp_adapter import entry


def test_the_check_passes_when_the_mcp_sdk_imports(capsys):
    entry.main(["--check"])
    out, err = capsys.readouterr()
    assert out.strip().splitlines()[-1] == "Hermes ACP check OK" and "MCP SDK" not in err


@pytest.mark.parametrize("module", ["mcp", "mcp.client.stdio"])
def test_the_check_fails_with_the_install_command_when_the_mcp_sdk_does_not_import(monkeypatch, capsys, module):
    monkeypatch.setitem(sys.modules, module, None)  # importing it now raises ImportError
    with pytest.raises(SystemExit) as failed:
        entry.main(["--check"])
    assert failed.value.code == 1
    out, err = capsys.readouterr()
    assert "Hermes ACP check OK" not in out
    assert "the MCP SDK does not import" in err and "hermes pm install --extra mcp" in err


def test_hermes_acp_reports_the_mcp_sdk_not_a_missing_acp_extra(monkeypatch, capsys):
    """``hermes acp`` turns an ImportError into "install the acp extra"; the check exits instead."""
    from types import SimpleNamespace
    from hermes_cli.main_agent_cmds import cmd_acp
    monkeypatch.setitem(sys.modules, "mcp", None)
    with pytest.raises(SystemExit) as failed:
        cmd_acp(SimpleNamespace(check=True))
    assert failed.value.code == 1
    err = capsys.readouterr().err
    assert "hermes pm install --extra mcp" in err and "--extra acp" not in err
