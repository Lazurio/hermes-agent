"""Lazurio overlay: the session's model and reasoning effort as ACP session config options.

Lazurio T3 Code builds its model picker only from ``configOptions`` of category ``model`` and offers
every other select, such as one of category ``thought_level``, in its composer. Upstream's gateway
viewer advertises the model only through the unstable ``models`` field and answers
``set_config_option`` without effect, so Buddy's chat showed one "Default" model and could not set
the reasoning effort.

Here both are config options beside ``models`` and ``modes`` (which stay, for Zed), and setting one
runs the owner mutation of the matching upstream control: ``model`` the model pick of
``set_session_model``, ``thought_level`` the session's ``/reasoning`` level. A change from any
surface reaches the editor as ``config_option_update``.

Overlay row ``acp: model and thought_level session config options on the gateway viewer``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's gateway ACP viewer advertises
config options for the model and the reasoning effort and implements ``set_config_option`` for them.
"""
from __future__ import annotations

import asyncio
import logging

import acp
from acp.schema import ConfigOptionUpdate, SessionConfigOptionSelect, SessionConfigSelectOption

logger = logging.getLogger(__name__)

MODEL = "model"
THOUGHT_LEVEL = "thought_level"
# Upstream's own reasoning picker shows a session without a level as "medium (default)"
# (``gateway.reasoning.level_default``), so the select names that level as the current one.
DEFAULT_THOUGHT_LEVEL = "medium"


def thought_level_values() -> tuple[str, ...]:
    """The levels upstream's ``/reasoning`` picker offers: off, then its ladder."""
    from hermes_constants import VALID_REASONING_EFFORTS
    return ("none", *VALID_REASONING_EFFORTS)


def current_thought_level(info: dict) -> str:
    level = str(info.get("reasoning_effort") or "").strip().lower()
    return level if level in thought_level_values() else DEFAULT_THOUGHT_LEVEL


def build_config_options(info: dict, models) -> list[SessionConfigOptionSelect]:
    """The editor's config options for a session's ``session.info`` and its model picker (``models``,
    a ``SessionModelState`` or None). Labels name levels as upstream does on the session's route,
    e.g. "ultra (sends max on this route)"."""
    from agent.reasoning_effort import effort_display_label
    options = []
    if models is not None and models.current_model_id:
        choices = [SessionConfigSelectOption(value=model.model_id, name=model.name, description=model.description)
                   for model in models.available_models]
        if models.current_model_id not in {choice.value for choice in choices}:
            choices.insert(0, SessionConfigSelectOption(value=models.current_model_id, name=models.current_model_id))
        options.append(SessionConfigOptionSelect(type="select", id=MODEL, name="Model", category="model",
                                                 current_value=models.current_model_id, options=choices))
    route = (info.get("provider"), info.get("model"), info.get("api_mode"))
    options.append(SessionConfigOptionSelect(
        type="select", id=THOUGHT_LEVEL, name="Reasoning effort", category="thought_level",
        description="How much the model reasons before it answers.",
        current_value=current_thought_level(info),
        options=[SessionConfigSelectOption(value=level, name=effort_display_label(level, *route))
                 for level in thought_level_values()]))
    return options


def _shown(options) -> tuple:
    return tuple((option.id, option.current_value) for option in options)


class LazurioConfigOptionsMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``), whose ``_session_fields``,
    ``set_config_option``, snapshot adoption (``_adopt``), ``session.info`` projection and
    post-attach notices call the hooks below.

    What the editor was last shown follows the owner by reading it: an owner ``session.info`` or an
    adopted snapshot whose model or level differs from what the editor was shown only asks for a
    refresh, which reads the owner's current ``session.info`` and sends ``config_option_update`` when
    it differs. Refreshes of a session run one at a time and repeat while another request arrived,
    so the last answer the editor gets reflects the owner's latest read; no event payload is merged
    after an await, so an older event can never overwrite a newer snapshot."""

    _LAZURIO_KEYS = ("model", "provider", "reasoning_effort")
    # A failed owner read is retried after these delays (seconds) on the same connection; after the
    # last one the request stays until the next change, control, attach or reconnect asks again.
    _LAZURIO_RETRY_DELAYS = (0.5, 2.0, 8.0)

    def _lazurio_states(self) -> dict:
        # Session -> {"info": the owner info the options were built from, "models": its picker,
        # "options": the options the editor was last shown, "shown": their (id, value) pairs,
        # "lock": serializes this session's refreshes and records, "dirty": a refresh is wanted,
        # "failures": owner reads failed in a row}.
        return self.__dict__.setdefault("_lazurio_config_state", {})

    def _lazurio_record(self, session_id, info, models) -> list[SessionConfigOptionSelect]:
        options = build_config_options(info, models)
        state = self._lazurio_states().setdefault(session_id, {"lock": asyncio.Lock(), "dirty": False})
        state.update(info=dict(info), models=models, options=options, shown=_shown(options))
        return options

    def _lazurio_config_fields(self, session_id, info, models) -> dict:
        """``config_options`` for a new/load/resume/fork reply. The owner may publish a new model or
        level while the reply is built, before this session has state to project it onto, so the
        post-attach notices (``_after_attach``) end with a refresh (``_lazurio_reconcile``)."""
        options = self._lazurio_record(session_id, info, models)
        self._lazurio_states()[session_id]["dirty"] = True
        return {"config_options": options}

    def _lazurio_shown_options(self, session_id) -> list[SessionConfigOptionSelect]:
        """The answer to another config id (the edit approval policy, or one this server ignores):
        the options the editor was last shown, read without a call to the owner."""
        return list((self._lazurio_states().get(session_id) or {}).get("options") or [])

    async def _lazurio_set_config_option(self, session_id, config_id, value):
        """``model`` and ``thought_level``: the owner's mutation, then the session's options from the
        owner's fresh snapshot. As ``set_session_model`` does, the prepared mutation is acknowledged
        only once that snapshot is adopted: an editor retrying after a lost answer replays the owner's
        receipt instead of writing again. A refresh follows the answer, so a change another surface
        made meanwhile still reaches the editor."""
        if not isinstance(value, str) or not value.strip():
            raise acp.RequestError.invalid_params({"configId": config_id, "value": value})
        if config_id == MODEL:
            await self.set_session_model(model_id=value, session_id=session_id)  # adopts the fresh snapshot
        else:
            if value not in thought_level_values():
                raise acp.RequestError.invalid_params({"configId": config_id, "value": value,
                                                       "allowed": list(thought_level_values())})
            client = await self._client()
            payload = {"effort": value}
            self._unacked.pop(session_id, None)  # a control between a lost submit and its repeat retires it
            await self._mutations.apply(client, session_id, "reasoning", payload)
            await self._adopt(session_id, await client.rpc("session.resume", session_id=session_id))
            self._mutations.acknowledge(session_id, "reasoning", payload)
        from acp_adapter.gateway_session_meta import model_state
        info = dict((self._snapshots.get(session_id) or {}).get("info") or {})
        models = await asyncio.to_thread(model_state, info)
        state = self._lazurio_states().setdefault(session_id, {"lock": asyncio.Lock(), "dirty": False})
        async with state["lock"]:
            options = self._lazurio_record(session_id, info, models)
        self._lazurio_request_refresh(session_id)
        return options

    def _lazurio_request_refresh(self, session_id):
        state = self._lazurio_states().get(session_id)
        if state is None or self._conn is None:
            return
        state["dirty"] = True
        # Asked with no connection installed: a reconnect re-attaching its sessions (``_client``
        # installs the connection only after ``_reattach`` adopted their snapshots), so read after it.
        after_connect = self._gateway is None
        task = asyncio.get_running_loop().create_task(self._lazurio_refresh(session_id, after_connect))
        self._background.add(task)  # aclose cancels it with the other background work
        task.add_done_callback(self._background.discard)

    async def _lazurio_reconcile(self, session_id):
        """The last post-attach notice: what the attach reply showed, refreshed from the owner."""
        await self._lazurio_refresh(session_id)

    async def _lazurio_refresh(self, session_id, after_connect=False):
        """Read the owner's current model and level and send ``config_option_update`` when they
        differ from what the editor was shown; repeat while a new request arrived during a round.
        Uses the live connection only and never opens one: without one (``aclose``, a lost
        transport) the request stays and the reconnect's own adoption decides whether to read. A
        failed read keeps the request and is retried on its connection (``_lazurio_retry``)."""
        state = self._lazurio_states().get(session_id)
        if state is None:
            return
        from acp_adapter.gateway_session_meta import model_state
        if after_connect:
            async with self._connect_lock:  # until the reconnect in progress has installed its connection
                pass
        async with state["lock"]:
            while state["dirty"]:
                client = self._gateway
                if client is None or self._transport_closed():
                    return
                state["dirty"] = False
                try:
                    info = await client.rpc("session.info", session_id=session_id)
                    models = state["models"]
                    if any(info.get(key) != state["info"].get(key) for key in ("model", "provider")):
                        models = await asyncio.to_thread(model_state, info)
                except Exception:
                    # Boundary, as upstream's post-attach notices: a detached editor or a refused read
                    # is not a failure of the editor's own requests. The request stays.
                    logger.debug("Lazurio config options of %s not refreshed", session_id, exc_info=True)
                    state["dirty"] = True
                    self._lazurio_retry(session_id, client)
                    return
                state["failures"] = 0
                shown = state["shown"]
                options = self._lazurio_record(session_id, info, models)
                if _shown(options) != shown and self._conn:
                    await self._conn.session_update(session_id=self._editor_id(session_id),
                                                    update=ConfigOptionUpdate(session_update="config_option_update",
                                                                              config_options=options))

    def _lazurio_retry(self, session_id, client):
        """Retry a failed read after the next delay, on the connection it failed on: a reconnect's
        own adoption decides for a new connection. Bounded; afterwards the next request reads."""
        state = self._lazurio_states()[session_id]
        failures = state["failures"] = state.get("failures", 0) + 1
        if failures > len(self._LAZURIO_RETRY_DELAYS) or self._conn is None:
            return

        async def retry():
            await asyncio.sleep(self._LAZURIO_RETRY_DELAYS[failures - 1])
            if self._gateway is client and not self._transport_closed():
                await self._lazurio_refresh(session_id)

        task = asyncio.get_running_loop().create_task(retry())
        self._background.add(task)  # aclose cancels it with the other background work
        task.add_done_callback(self._background.discard)

    async def _lazurio_project_config(self, sid, editor_id, payload):
        """An owner ``session.info`` or an adopted snapshot whose model or level differs from what the
        editor was shown (another surface's pick, or this editor's own) asks for a refresh. Adoption
        counts as upstream's ``_adopt`` says: the pump skips the events a snapshot covers, so a
        change it carries (a slash command's or reconnect's fresh snapshot) is seen only here."""
        state = self._lazurio_states().get(sid)
        keys = [key for key in self._LAZURIO_KEYS if key in payload]
        if state is None or not keys or all(payload[key] == state["info"].get(key) for key in keys):
            return
        self._lazurio_request_refresh(sid)
