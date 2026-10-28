"""Lazurio overlay: a message that arrives while Buddy works steers his running turn by default.

gateway/lazurio_busy_default.py; overlay row ``gateway: a message that arrives while Buddy works
steers his running turn by default``.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, SessionSource
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The gateway's config home, with no config.yaml and no busy mode in the environment."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
    return tmp_path


def test_a_gateway_without_a_busy_mode_steers(home):
    assert GatewayRunner._load_busy_input_mode() == "steer"


@pytest.mark.parametrize("mode", ["interrupt", "queue", "steer"])
def test_the_configured_busy_mode_wins(home, mode):
    # What the person's own /busy writes (save_config_value("display.busy_input_mode", ...)).
    (home / "config.yaml").write_text(f"display:\n  busy_input_mode: {mode}\n", encoding="utf-8")
    assert GatewayRunner._load_busy_input_mode() == mode


def test_the_environment_wins_over_the_config(home, monkeypatch):
    (home / "config.yaml").write_text("display:\n  busy_input_mode: steer\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "interrupt")
    assert GatewayRunner._load_busy_input_mode() == "interrupt"


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}


def _runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    # As the runtime init loads them (gateway/run_runtime_init.py).
    runner._busy_input_mode = GatewayRunner._load_busy_input_mode()
    runner._busy_text_mode = GatewayRunner._load_busy_text_mode()
    runner._profile_adapters = {}
    runner.adapters = {}
    runner._sessions = {}
    runner._draining = False
    runner._restart_requested = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda source: True
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    return runner


async def _follow_up(tmp_path, monkeypatch, *, profile_mode=None, message_type=MessageType.TEXT,
                     started_ago=1.0):
    """A Telegram follow-up while a routed profile's turn runs, ``started_ago`` seconds after it
    started (upstream's default grace window, 3 s, untouched); returns the agent and the adapter."""
    monkeypatch.delenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner = _runner()
    # A routed profile whose config.yaml names no busy mode takes the gateway's.
    profile_home = tmp_path / "buddy"
    profile_home.mkdir()
    display = "display:\n  tool_progress: off\n"
    if profile_mode is not None:
        display += f"  busy_input_mode: {profile_mode}\n"
    (profile_home / "config.yaml").write_text(display, encoding="utf-8")
    assert await runner._start_one_profile_adapters("buddy", profile_home, {}) == 0
    adapter = _Adapter(PlatformConfig(enabled=True, token="test-token"), Platform.TELEGRAM)
    runner._profile_adapters["buddy"][Platform.TELEGRAM] = adapter
    runner._configure_profile_adapter(adapter, "buddy", Platform.TELEGRAM)
    event = MessageEvent(
        text="only the ACP changes",
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm", user_id="user-1", profile="buddy",
        ),
        message_id="message-1",
    )
    session_key = runner._session_key_for_source(event.source)
    agent = MagicMock()
    agent._active_children = []
    agent.steer.return_value = True
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time() - started_ago

    assert await runner._handle_message(event) is None
    return agent, adapter, session_key


@pytest.mark.asyncio
@pytest.mark.parametrize("started_ago", [1.0, 30.0])  # inside upstream's 3 s grace window, and after it
async def test_a_text_that_arrives_while_buddy_works_steers_his_turn(home, tmp_path, monkeypatch, started_ago):
    agent, adapter, session_key = await _follow_up(tmp_path, monkeypatch, started_ago=started_ago)

    agent.interrupt.assert_not_called()
    agent.steer.assert_called_once()
    assert agent.steer.call_args.args[0].endswith("only the ACP changes")
    assert session_key not in adapter._pending_messages


@pytest.mark.asyncio
async def test_the_grace_window_still_holds_a_text_in_queue_mode(home, tmp_path, monkeypatch):
    agent, _adapter, _key = await _follow_up(tmp_path, monkeypatch, profile_mode="queue")

    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_the_grace_window_still_holds_a_photo_in_steer_mode(home, tmp_path, monkeypatch):
    agent, _adapter, _key = await _follow_up(tmp_path, monkeypatch, message_type=MessageType.PHOTO)

    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()
