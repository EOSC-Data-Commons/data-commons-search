"""Tests for database persistence functions against a real PostgreSQL database."""

from typing import Any, Literal

import pytest

from data_commons_search.db import (
    get_conversation,
    get_conversations,
    make_conversation_label,
    store_messages,
)
from data_commons_search.models import MessageItem, TextPart, ToolCallItem, ToolResultItem, UserInfo
from tests.conftest import postgres_available

pytestmark = pytest.mark.skipif(not postgres_available(), reason="PostgreSQL not reachable")


@pytest.fixture()
def user():
    return UserInfo(sub="user-001", email="test@example.com", name="Test User", preferred_username="testuser")


@pytest.fixture()
def other_user():
    return UserInfo(sub="user-002", email="other@example.com", name="Other User")


def msg(role: Literal["system", "user", "assistant"] = "user", text: str = "Hello") -> MessageItem:
    return MessageItem(role=role, content=[TextPart(text=text)])


class TestMakeConversationLabel:
    def test_returns_first_user_message_text(self):
        assert make_conversation_label([msg("user", "Find climate datasets")]) == "Find climate datasets"

    def test_skips_non_user_messages(self):
        items = [msg("assistant", "Sure!"), msg("user", "My question")]
        assert make_conversation_label(items) == "My question"

    def test_truncates_long_text(self):
        long_text = "a" * 150
        label = make_conversation_label([msg("user", long_text)])
        assert label is not None
        assert len(label) <= 100
        assert label.endswith("…")

    def test_exactly_max_length_not_truncated(self):
        text = "a" * 50
        assert make_conversation_label([msg("user", text)]) == text

    def test_returns_none_when_no_user_message(self):
        assert make_conversation_label([msg("assistant", "Hi")]) is None

    def test_returns_none_for_empty_list(self):
        assert make_conversation_label([]) is None


# ── store_messages / get_conversations / get_conversation ─────────────────────


def test_creates_conversation_on_first_store(user):
    store_messages(user=user, thread_id="t1", items=[msg("user", "Hello")])
    assert len(get_conversations(user.sub)) == 1


def test_conversation_label_derived_from_user_message(user):
    store_messages(user=user, thread_id="t1", items=[msg("user", "CO2 in rivers")])
    assert get_conversations(user.sub)[0].label == "CO2 in rivers"


def test_conversation_label_fallback_when_no_user_message(user):
    store_messages(user=user, thread_id="t1", items=[msg("assistant", "Hi")])
    assert get_conversations(user.sub)[0].label == "New conversation"


def test_empty_items_skips_store(user):
    store_messages(user=user, thread_id="t1", items=[])
    assert get_conversations(user.sub) == []


def test_get_conversation_returns_none_for_unknown_thread(user) -> None:
    assert get_conversation(user.sub, "does-not-exist") is None


def test_messages_roundtrip_correctly(user) -> None:
    items = [
        msg("user", "Find CO2 datasets"),
        msg("assistant", "Here are some datasets…"),
    ]
    store_messages(user=user, thread_id="t1", items=items)

    detail: Any = get_conversation(user.sub, "t1")
    assert detail is not None
    assert detail.thread_id == "t1"
    assert len(detail.items) == 2
    assert detail.items[0].type == "message"
    assert detail.items[0].role == "user"
    assert detail.items[0].content[0].text == "Find CO2 datasets"
    assert detail.items[1].type == "message"
    assert detail.items[1].role == "assistant"
    assert detail.items[1].content[0].text == "Here are some datasets…"


def test_tool_call_and_result_roundtrip(user):
    tool_call = ToolCallItem(name="search_datasets", arguments={"query": "CO2"})
    tool_result = ToolResultItem(call_id=tool_call.id, content="Found 3 results")
    items = [msg("user", "Search CO2"), tool_call, tool_result]
    store_messages(user=user, thread_id="t1", items=items)

    detail: Any = get_conversation(user.sub, "t1")
    assert detail is not None
    assert len(detail.items) == 3
    assert detail.items[0].type == "message"
    assert detail.items[1].type == "tool_call"
    assert detail.items[1].name == "search_datasets"
    assert detail.items[2].type == "tool_result"
    assert detail.items[2].content == "Found 3 results"


def test_appending_to_existing_conversation(user):
    """Second store call appends messages; only one conversation is created."""
    store_messages(user=user, thread_id="t1", items=[msg("user", "First message")])
    store_messages(user=user, thread_id="t1", items=[msg("assistant", "Reply")])

    assert len(get_conversations(user.sub)) == 1
    detail = get_conversation(user.sub, "t1")
    assert detail is not None
    assert len(detail.items) == 2


def test_multiple_conversations_listed(user):
    store_messages(user=user, thread_id="t1", items=[msg("user", "Thread one")])
    store_messages(user=user, thread_id="t2", items=[msg("user", "Thread two")])

    summaries = get_conversations(user.sub)
    assert len(summaries) == 2
    thread_ids = {s.thread_id for s in summaries}
    assert thread_ids == {"t1", "t2"}


def test_conversations_isolated_between_users(user, other_user):
    store_messages(user=user, thread_id="t1", items=[msg("user", "User one msg")])
    store_messages(user=other_user, thread_id="t2", items=[msg("user", "User two msg")])

    assert len(get_conversations(user.sub)) == 1
    assert len(get_conversations(other_user.sub)) == 1
    assert get_conversation(user.sub, "t2") is None
    assert get_conversation(other_user.sub, "t1") is None


def test_user_is_upserted_on_repeated_calls(user):
    """store_messages can be called repeatedly for the same user without error."""
    store_messages(user=user, thread_id="t1", items=[msg("user", "First")])
    store_messages(user=user, thread_id="t2", items=[msg("user", "Second")])
    assert len(get_conversations(user.sub)) == 2
