import pytest

from agent.message_metadata import (
    INFLIGHT_TASK_RESTATEMENT, TYPED_ASK, append_message, is_compaction_scaffolding, record_absorbed_message,
    replay_metadata_fields, stamp_message_timestamp,
)


def test_stamp_preserves_source_timestamp(monkeypatch):
    monkeypatch.setattr("agent.message_metadata.wall_time", lambda: 999.0)
    message = {"role": "user", "content": "hello", "timestamp": 123.0}

    result = stamp_message_timestamp(message)

    assert result is message
    assert message["timestamp"] == 123.0


def test_append_stamps_same_mapping_at_append_time(monkeypatch):
    monkeypatch.setattr("agent.message_metadata.wall_time", lambda: 456.0)
    messages = []
    message = {"role": "tool", "content": "ok"}

    result = append_message(messages, message)

    assert result is message
    assert messages == [message]
    assert message["timestamp"] == 456.0


def test_replay_keeps_a_user_rows_typed_ask_beside_the_scaffolding_flag():
    """Gateway replay carries the stamp (previous_message reads it after a restart) without
    turning the row into compaction scaffolding, which would skip its timestamp."""
    stamped = {"role": "user", "content": "x", "display_metadata": {TYPED_ASK: "ask", "other": 1}}
    both = {"role": "user", "content": "x", "display_metadata": {TYPED_ASK: "ask", INFLIGHT_TASK_RESTATEMENT: True}}

    assert replay_metadata_fields(stamped) == {"display_metadata": {TYPED_ASK: "ask"}}
    assert not is_compaction_scaffolding(stamped)
    assert replay_metadata_fields(both) == {"display_metadata": {INFLIGHT_TASK_RESTATEMENT: True, TYPED_ASK: "ask"}}
    assert replay_metadata_fields({"role": "assistant", "content": "x"}) == {}


_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def _stamped_image_turn():
    return {"role": "user", "content": [{"type": "text", "text": "what is this"}, _IMAGE,
                                        {"type": "text", "text": "PLUGIN NOTE"}], "display_metadata": {TYPED_ASK: "what is this"}}


def _previous(row):
    from agent.turn_context import _previous_user_message_text
    return _previous_user_message_text([row, {"role": "assistant", "content": "x"}])


def _summary(flagged):
    from agent.context_compressor import SUMMARY_PREFIX, _SUMMARY_END_MARKER
    return {"role": "user", "content": f"{SUMMARY_PREFIX}\nold work\n{_SUMMARY_END_MARKER}",
            **({"_compressed_summary": True} if flagged else {})}


def _compressor():
    from agent.context_compressor import ContextCompressor
    compressor = ContextCompressor.__new__(ContextCompressor)
    compressor.quiet_mode = True
    return compressor


@pytest.mark.parametrize("flagged", [True, False], ids=["flagged", "content-only"])
def test_a_summary_carrier_restating_the_in_flight_turn_takes_its_typed_ask(flagged):
    """Compaction restates an unfinished stamped image turn onto the summary carrier; the carrier's
    only ask is that turn's, so ``previous_message`` must not read its notes or the restatement."""
    carrier = _summary(flagged)
    compressed = _compressor()._reappend_inflight_user_task([carrier], _stamped_image_turn())

    assert compressed == [carrier]  # merged onto the carrier: the transcript already ends on a user row
    assert carrier["display_metadata"][TYPED_ASK] == "what is this"
    assert _previous(carrier) == "what is this"


def test_an_anchor_leading_a_scaffolding_turn_keeps_its_typed_ask():
    from agent.conversation_compression import _merge_anchor_into_user_message
    from tools.todo_tool import TODO_INJECTION_HEADER

    target = {"role": "user", "content": f"{TODO_INJECTION_HEADER}\n- [ ] a", "_todo_snapshot_synthetic": True}
    _merge_anchor_into_user_message(target, _stamped_image_turn())

    assert _previous(target) == "what is this"


@pytest.mark.parametrize("ends_on_user", [True, False], ids=["merged-onto-carrier", "standalone-restatement"])
def test_a_turn_still_in_flight_across_two_compactions_keeps_its_typed_ask(ends_on_user):
    """The second compaction's in-flight row is the first one's carrier (it holds the restated task and
    its stamp); restating it again, merged or standalone, must keep the ask, not the notes or header."""
    first = _summary(True)
    _compressor()._reappend_inflight_user_task([first], _stamped_image_turn())
    second = _summary(True) if ends_on_user else {**_summary(True), "role": "assistant"}
    compressed = _compressor()._reappend_inflight_user_task([second], first)

    restated = compressed[-1]
    assert compressed == ([second] if ends_on_user else [second, restated])
    assert restated["display_metadata"][TYPED_ASK] == "what is this"
    assert _previous(restated) == "what is this"


@pytest.mark.parametrize("flagged", [True, False], ids=["flagged", "content-only"])
def test_a_summary_folded_under_a_stamped_row_adds_no_ask(flagged):
    """The joined row is not a carrier on the read path, so dropping the stamp would read the summary."""
    from agent.agent_runtime_helpers import _merge_consecutive_users

    survivor = {"role": "user", "content": "ask\n[screenshot]\n\nPLUGIN NOTE", "display_metadata": {TYPED_ASK: "ask"}}
    merged, _ = _merge_consecutive_users([survivor, _summary(flagged), {"role": "assistant", "content": "x"}])

    assert merged[0]["display_metadata"][TYPED_ASK] == "ask"
    assert _previous(merged[0]) == "ask"


@pytest.mark.parametrize("carrier_role", ["user", "assistant"])
def test_a_turn_restated_twice_keeps_its_typed_ask_whatever_role_carried_the_summary(carrier_role):
    """The first summary can ride an assistant(tool_calls) row; its restated task must still reach the
    second compaction's standalone user restatement with its stamp."""
    first = {**_summary(True), "role": carrier_role}
    if carrier_role == "assistant":
        first["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
    _compressor()._reappend_inflight_user_task([{"role": "user", "content": "earlier"}, first], _stamped_image_turn())
    assert first["display_metadata"][TYPED_ASK] == "what is this"
    assert replay_metadata_fields(first)["display_metadata"][TYPED_ASK] == "what is this"  # gateway replay
    second = {**_summary(True), "role": "assistant"}
    restated = _compressor()._reappend_inflight_user_task([second], first)[-1]

    assert restated["role"] == "user" and _previous(restated) == "what is this"


def _carrier_with_live_ask(flagged):
    from agent.context_compressor import SUMMARY_PREFIX, _SUMMARY_END_MARKER
    return {"role": "user", "content": f"{SUMMARY_PREFIX}\nold work\n{_SUMMARY_END_MARKER}\n\nlive typed ask",
            **({"_compressed_summary": True} if flagged else {})}


@pytest.mark.parametrize("flagged", [True, False], ids=["flagged", "content-only"])
def test_a_carrier_holding_an_unstamped_live_ask_keeps_that_ask(flagged):
    """A text-only ask after the end marker has no stamp; folding a stamped turn under it must keep that
    ask beside the turn's, never the summary or the turn's notes."""
    carrier = _carrier_with_live_ask(flagged)
    carrier["content"] += "\n\nwhat is this\n[screenshot]\n\nPLUGIN NOTE"
    record_absorbed_message(carrier, {"role": "user", "content": "what is this\n[screenshot]\n\nPLUGIN NOTE",
                                      "display_metadata": {TYPED_ASK: "what is this"}})

    assert carrier["display_metadata"][TYPED_ASK] == "live typed ask\n\nwhat is this"
    assert _previous(carrier) == "live typed ask\n\nwhat is this"


def test_a_plain_ask_followed_by_a_stamped_carrier_keeps_both_asks():
    """The survivor is judged by its own text before the fold, not by the summary it just absorbed: the
    joined row reads as a delimiter-form summary, so judging it afterwards stamps its prior-context block."""
    from agent.context_compressor import SUMMARY_PREFIX, ContextCompressor, is_compaction_summary_message

    carrier = {"role": "user", "content": "image ask\n[screenshot]\n\nPLUGIN NOTE", "display_metadata": {TYPED_ASK: "image ask"}}
    compressor = ContextCompressor.__new__(ContextCompressor)
    compressor._summary_has_user_turn = True
    compressor._merge_summary_into_tail_row(carrier, f"{SUMMARY_PREFIX}\nold work", "user", False)
    survivor = {"role": "user", "content": "plain ask\n\n" + carrier["content"]}
    assert is_compaction_summary_message(survivor)
    record_absorbed_message(survivor, carrier)

    assert survivor["display_metadata"][TYPED_ASK] == "plain ask\n\nimage ask"
    assert _previous(survivor) == "plain ask\n\nimage ask"
