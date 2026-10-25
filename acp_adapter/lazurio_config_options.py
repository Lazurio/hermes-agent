"""Lazurio overlay: the session's fast mode as the one ACP session config option.

Buddy's chat in Lazurio T3 Code offers one choice, Normal or Fast: Hermes's fast mode answers sooner
and uses more of the subscription. It offers no model and no reasoning effort: the Lazurio Platform
sets both in Hermes's own config and the chat never overrides them (the Organization Admin's
decision of 2026-10-25). Upstream's gateway viewer advertises no session config options and answers
``set_config_option`` without effect.

Here a session whose route offers fast mode advertises one select, ``fast`` (``normal`` or
``fast``), beside upstream's ``models`` and ``modes``, which stay as upstream has them. It is offered
by the check upstream's session-scoped ``/fast`` makes before it pins a tier (``_set_fast`` in
``gateway/session_tui_config.py``: ``resolve_fast_mode_overrides`` on the session's model and route),
and its value is the tier the session's next turn runs, the owner's ``config.get fast``: a static
tier (fast, ultrafast) shows as ``fast``, anything else (normal, an ``auto`` or ``cold`` window) as
``normal``. Setting it runs that same control, the TUI's ``/fast`` (``config.set fast``): scoped to
the session, never written to config.yaml, and refused by the owner where the route has no fast
mode. A change from any surface reaches the editor as ``config_option_update``, at the latest when
the session's next turn ends.

Overlay row ``acp: fast mode session config option on the gateway viewer``
(docs/operations/lazurio-fork-release.md). Removal: when upstream's gateway ACP viewer advertises the
session's fast mode as a session config option and implements ``set_config_option`` for it.
"""
from __future__ import annotations

import asyncio
import logging

import acp
from acp.schema import ConfigOptionUpdate, SessionConfigOptionSelect, SessionConfigSelectOption

logger = logging.getLogger(__name__)

FAST = "fast"
NORMAL = "normal"
VALUES = (NORMAL, FAST)


def fast_offered(info: dict) -> bool:
    """Whether the session's route offers fast mode: the check ``_set_fast`` makes before it pins a
    static tier, on the model and provider of the owner's ``session.info``. That carries no base URL,
    so a first-party provider served from another endpoint shows the option and the owner, which
    checks the session's base URL too, refuses fast there (``fast_unavailable``)."""
    from hermes_cli.models import resolve_fast_mode_overrides
    model = str(info.get("model") or "").strip()
    return bool(model) and resolve_fast_mode_overrides(model, provider=info.get("provider") or None) is not None


def fast_value(tier) -> str:
    """The select's value for an owner tier (``session.info`` ``service_tier``) or the owner's word for
    it (``config.get fast``): a static tier is fast mode on; normal and a bounded ``auto`` or ``cold``
    window are ``normal``, as ``config.get fast`` reads them."""
    from agent.fast_mode import STATIC_TIERS, parse_service_tier
    return FAST if parse_service_tier(tier) in STATIC_TIERS else NORMAL


def build_config_options(info: dict, value: str) -> list[SessionConfigOptionSelect]:
    """The editor's config options for a session's ``session.info`` and its fast-mode value: the
    ``fast`` select where the route offers fast mode, else none."""
    if not fast_offered(info):
        return []
    return [SessionConfigOptionSelect(
        type="select", id=FAST, name="Fast mode",
        description="Fast mode answers sooner and uses more of the subscription.",
        current_value=value,
        options=[SessionConfigSelectOption(value=NORMAL, name="Normal"),
                 SessionConfigSelectOption(value=FAST, name="Fast")])]


def _shown(options) -> tuple:
    return tuple((option.id, option.current_value) for option in options)


class LazurioConfigOptionsMixin:
    """Mixed into ``GatewayACPAgent`` (``acp_adapter/gateway_server.py``), whose ``_session_fields``,
    ``set_config_option``, snapshot adoption (``_adopt``), ``session.info`` projection and
    post-attach notices call the hooks below.

    What the editor was last shown follows the owner by reading it: a refresh reads the owner's
    ``session.info`` (the route) and ``config.get fast`` (the tier) and sends ``config_option_update``
    when the options differ. Refreshes of a session run one at a time and repeat while another
    request arrived, so the last answer the editor gets reflects the owner's latest read; no event
    payload is merged after an await, so an older event can never overwrite a newer answer.

    The owner publishes no event when a session's tier is pinned, and its ``session.info`` reports
    the tier the session's agent last ran with. So an owner ``session.info`` or an adopted snapshot
    asks for a refresh when its model or provider differs from the route the options were built
    from, or its tier from the value the editor was shown: another surface's change reaches the
    editor at once on an attach or this editor's own set, and otherwise at the latest with the
    ``session.info`` that ends the session's next turn. A reconnect reads only when its snapshot
    differs so (upstream's reconnect tests pin every call on the new connection)."""

    _LAZURIO_ROUTE_KEYS = ("model", "provider")
    # A failed owner read is retried after these delays (seconds) on the same connection; after the
    # last one the request stays until the next change, set, attach or reconnect asks again.
    _LAZURIO_RETRY_DELAYS = (0.5, 2.0, 8.0)

    def _lazurio_states(self) -> dict:
        # Session -> {"info": the owner info the options were built from, "value": the fast-mode
        # value last read or set, "options": the options the editor was last shown, "shown": their
        # (id, value) pairs, "lock": serializes this session's refreshes and records, "dirty": a
        # refresh is wanted, "failures": owner reads failed in a row}.
        return self.__dict__.setdefault("_lazurio_config_state", {})

    def _lazurio_state(self, session_id) -> dict:
        return self._lazurio_states().setdefault(session_id, {
            "info": {}, "value": None, "options": [], "shown": (), "lock": asyncio.Lock(), "dirty": False})

    def _lazurio_record(self, session_id, info, value) -> list[SessionConfigOptionSelect]:
        options = build_config_options(info, value)
        self._lazurio_state(session_id).update(info=dict(info), value=value, options=options,
                                               shown=_shown(options))
        return options

    def _lazurio_config_fields(self, session_id, info) -> dict:
        """``config_options`` for a new/load/resume/fork reply, valued by the tier of the attach's
        ``session.info``. That is the tier the session's agent last ran with, not one pinned since,
        and the owner may publish a change while the reply is built, before this session has state to
        project it onto, so the post-attach notices (``_after_attach``) end with a refresh
        (``_lazurio_reconcile``)."""
        options = self._lazurio_record(session_id, info, fast_value(info.get("service_tier")))
        self._lazurio_states()[session_id]["dirty"] = True
        return {"config_options": options}

    def _lazurio_shown_options(self, session_id) -> list[SessionConfigOptionSelect]:
        """The answer to another config id (the edit approval policy, or one this server ignores):
        the options the editor was last shown, read without a call to the owner."""
        return list((self._lazurio_states().get(session_id) or {}).get("options") or [])

    async def _lazurio_set_config_option(self, session_id, value):
        """``fast``: the owner's session-scoped fast control (``config.set fast``), then the session's
        options with the tier the owner answered. Like the session's other controls, it retires what
        a lost submit or an ambiguous control retained, so the editor's later repeat of those is new
        work. The control writes no receipt: a retry after a lost answer pins the same tier again.
        A refresh follows the answer or the failure, so a change another surface made meanwhile, or a
        pin the owner made although its answer never came (a timeout), still reaches the editor; one
        whose connection was lost with the answer reaches it as another surface's change does."""
        if value not in VALUES:
            raise acp.RequestError.invalid_params({"configId": FAST, "value": value, "allowed": list(VALUES)})
        client = await self._client()
        self._unacked.pop(session_id, None)  # a control between a lost submit and its repeat retires it
        self._mutations.retire(session_id)
        state = self._lazurio_state(session_id)
        try:
            result = await client.rpc("config.set", session_id=session_id, key=FAST, value=value)
            async with state["lock"]:
                info = state.get("info") or (self._snapshots.get(session_id) or {}).get("info") or {}
                return self._lazurio_record(session_id, info, fast_value(result.get("value")))
        finally:
            self._lazurio_request_refresh(session_id)

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
        """Read the owner's route and tier and send ``config_option_update`` when the options differ
        from what the editor was shown; repeat while a new request arrived during a round. Uses the
        live connection only and never opens one: without one (``aclose``, a lost transport) the
        request stays and the reconnect's own adoption decides whether to read. A failed read keeps
        the request and is retried on its connection (``_lazurio_retry``)."""
        state = self._lazurio_states().get(session_id)
        if state is None:
            return
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
                    tier = (await client.rpc("config.get", session_id=session_id, key=FAST)).get("value")
                except Exception:
                    # Boundary, as upstream's post-attach notices: a detached editor or a refused read
                    # is not a failure of the editor's own requests. The request stays.
                    logger.debug("Lazurio config options of %s not refreshed", session_id, exc_info=True)
                    state["dirty"] = True
                    self._lazurio_retry(session_id, client)
                    return
                state["failures"] = 0
                shown = state["shown"]
                options = self._lazurio_record(session_id, info, fast_value(tier))
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
        """An owner ``session.info`` or an adopted snapshot asks for a refresh when its route differs
        from the one the options were built from (another surface's model pick), or its tier from the
        value the editor was shown (a pin another surface made, read once a turn ran with it, or one
        an owner restart dropped). Adoption counts as upstream's ``_adopt`` says: the pump skips the
        events a snapshot covers, so a change it carries (a slash command's or reconnect's fresh
        snapshot) is seen only here."""
        state = self._lazurio_states().get(sid)
        if state is None:
            return
        if (any(payload[key] != state["info"].get(key) for key in self._LAZURIO_ROUTE_KEYS if key in payload)
                or ("service_tier" in payload and fast_value(payload["service_tier"]) != state["value"])):
            self._lazurio_request_refresh(sid)
