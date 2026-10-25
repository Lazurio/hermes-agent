"""Lazurio overlay: identify names the sign-in cancel contract, and a stopping gateway keeps it.

Lazurio's Launchpad (LazurioPlatform#635) takes a sign-in's "not found" as Hermes' word that no
worker of it can save any more, and reads from the gateway's control socket whether that guarantee
holds: ``identify`` lists ``lazurio-oauth-cancel-contract-v1`` while the gateway is ready. That the
guarantee holds is tested in tests/hermes_cli/test_lazurio_oauth_sweep.py (the sweep, the DELETE
route, a dropped start, and the stop's release of the runtime lock). Overlay row ``oauth: identify
names the sign-in cancel contract, and a stopping gateway keeps it``
(docs/operations/lazurio-fork-release.md).
"""
import asyncio

import pytest

CONTRACT = "lazurio-oauth-cancel-contract-v1"


@pytest.mark.asyncio
async def test_identify_names_the_contract_while_the_gateway_is_ready_and_not_once_it_drains():
    """Read as the Launchpad reads it: the control socket's identify, through the repo's strict reader."""
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.run_bootstrap import _start_gateway_start_control_socket
    from gateway.run_runtime import (drain_gateway_runtime, initialize_gateway_runtime,
                                     publish_gateway_runtime_ready, start_gateway_runtime_api)
    from gateway.runtime_ownership import process_ownership
    from hermes_cli.gateway_runtime_discovery import query_identify
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.chmod(0o700)  # a profile home is private; the strict reader refuses one others can write

    async def identify():
        return await asyncio.to_thread(query_identify, home, timeout=5)

    process_ownership.reserve([home])
    runner = GatewayRunner(GatewayConfig())
    control = None
    try:
        await initialize_gateway_runtime(runner)
        control = await _start_gateway_start_control_socket(runner)
        assert control is not None
        await start_gateway_runtime_api(runner)
        assert await runner.start()
        starting = await identify()
        assert starting["state"] == "starting" and CONTRACT not in starting["capabilities"]

        publish_gateway_runtime_ready(runner)
        ready = await identify()
        assert ready["state"] == "ready"
        # Next to upstream's own capabilities, which every existing reader still finds.
        assert {"session-authority-v1", "durable-admission-v1", "event-replay-v1", CONTRACT} <= set(
            ready["capabilities"])

        await drain_gateway_runtime(runner)
        draining = await identify()
        assert draining["state"] == "draining" and draining["capabilities"] == []
    finally:
        await runner.stop()
        if control is not None:
            await control.stop()
        process_ownership.close()
