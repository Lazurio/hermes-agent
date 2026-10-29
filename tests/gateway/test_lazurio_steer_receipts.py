"""Lazurio overlay: a keyed ``session.steer`` is applied once, on a real owner.

gateway/lazurio_steer_receipts.py: the owner keeps an applied keyed steer as its receipt, answers a
repeat of the key with it, even after the turn ended, and applies the guidance once. Overlay row:
``acp: a message sent while Buddy works steers his running turn, never stops it``
(docs/operations/lazurio-fork-release.md).
"""
import asyncio
import json

import pytest

from gateway.lazurio_steer_receipts import CAPABILITY
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket
from tests.gateway.test_session_busy_controls import owner, settled


@pytest.mark.platforms("linux")
def test_a_keyed_steer_is_applied_once_and_answers_its_receipt_after_the_turn_ended(tmp_path):
    with owner(tmp_path) as (home, peer, desc):
        async def probe():
            async with websocket(home, desc) as ws:
                described = await rpc(ws, 'runtime.describe')
                assert CAPABILITY in described['result']['capabilities'], described
                created = await rpc(ws, 'session.create', request_id='receipts', source='tui', toolsets=['terminal'])
                sid = created['result']['session_id']
                await rpc(ws, 'prompt.submit', session_id=sid, input_id='warm', text='WARM')
                await settled(home)
                peer.blocked.clear()
                peer.release.clear()
                await rpc(ws, 'prompt.submit', session_id=sid, input_id='steer', text='BLOCK_STEER')
                assert await asyncio.to_thread(peer.blocked.wait, 25)
                generation = (await rpc(ws, 'session.resume', session_id=sid))['result']['execution_generation']
                steer = {'session_id': sid, 'text': 'CORRECT_ONCE', 'execution_generation': generation,
                         'idempotency_key': 'turn-1:message-1'}

                first = await rpc(ws, 'session.steer', **steer)
                again = await rpc(ws, 'session.steer', **steer)

                assert first.get('result', {}).get('status') == 'queued', first
                assert again.get('result') == first['result'], again
                blank = await rpc(ws, 'session.steer', **dict(steer, idempotency_key=' '))
                assert blank.get('error', {}).get('message') == 'invalid_params', blank
                peer.release.set()
                await settled(home)
                # The turn took the guidance once.
                assert json.dumps(peer.requests[-1]['messages']).count('CORRECT_ONCE') == 1, peer.requests[-1]
                # After the turn ended, the key still answers its receipt; another key meets the fence.
                late = await rpc(ws, 'session.steer', **steer)
                assert late.get('result') == first['result'], late
                other = await rpc(ws, 'session.steer', **dict(steer, idempotency_key='turn-1:message-2'))
                assert other.get('error', {}).get('message') == 'stale_generation', other
                unkeyed = {key: value for key, value in steer.items() if key != 'idempotency_key'}
                assert (await rpc(ws, 'session.steer', **unkeyed)).get('error', {}).get('message') == 'stale_generation'
        asyncio.run(probe())
