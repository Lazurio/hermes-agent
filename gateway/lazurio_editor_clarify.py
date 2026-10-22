"""Lazurio overlay: an ACP editor that answers form elicitation gets the ``clarify`` tool.

Buddy asks its user with ``clarify`` (Lazurio decision 0211). Upstream already answers a canonical
``clarify`` prompt in an ACP editor: an editor that advertised ``elicitation.form`` gets an
``elicitation/create`` form (free text, or the offered choices) and any other editor a permission
card with the choices plus Skip (``acp_adapter/elicitation.py``). But an ACP session's tools come
from the ``hermes-acp`` toolset, the coding posture without ``clarify``, so the model of a session
Lazurio T3 Code opened could never ask.

Here an ACP viewer whose editor advertised ``elicitation.form`` creates its session with
``editor.lazurio_form_elicitation``, and the owner adds the ``clarify`` toolset to that session,
unless the profile disables it (``agent.disabled_toolsets``). Other editors keep upstream's tools.
The viewer sends the key only to an owner that advertises ``CAPABILITY``, so a new viewer never
fails against an owner from before this overlay.

Overlay row ``acp: clarify for an ACP editor that answers form elicitation``
(docs/operations/lazurio-fork-release.md). Removal: when upstream gives ACP sessions of editors
that answer form elicitation the ``clarify`` tool.
"""
from __future__ import annotations

EDITOR_KEY = "lazurio_form_elicitation"
CAPABILITY = "lazurio-acp-form-clarify-v1"
TOOLSET = "clarify"


def validate(editor: dict) -> None:
    """Refuse a malformed key with the field it names (``validate_editor``'s create-path rule)."""
    from hermes_state_runtime import RuntimeStoreError
    if EDITOR_KEY in editor and type(editor[EDITOR_KEY]) is not bool:
        raise RuntimeStoreError("invalid_params", field=f"editor.{EDITOR_KEY}", detail="must be a boolean")


def editor_params(request: dict) -> dict:
    """The part of a stored launch request ``clarify_toolsets`` reads: its editor policy, if any."""
    editor = request.get("editor")
    return {"editor": editor} if isinstance(editor, dict) else {}


def clarify_toolsets(params: dict, config: dict, source: str) -> set[str]:
    """``{"clarify"}`` for an ACP session whose editor answers form elicitation, else nothing."""
    editor = params.get("editor")
    if source != "acp" or not isinstance(editor, dict) or editor.get(EDITOR_KEY) is not True:
        return set()
    from agent.skill_utils import parse_config_string_list
    disabled = set(parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets")))
    return set() if TOOLSET in disabled else {TOOLSET}
