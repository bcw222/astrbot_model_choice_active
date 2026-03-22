from __future__ import annotations

from astrbot_plugin_model_choice_active.runtime_state import RuntimeState


def test_touch_origin_evicts_oldest_state() -> None:
    state = RuntimeState()

    state.active_reply_stacks["o1"].append("a1")
    state.model_choice_histories["o1"].append("h1")

    state.touch_origin("o1", max_origins=1)
    state.touch_origin("o2", max_origins=1)

    assert "o1" not in state.active_reply_stacks
    assert "o1" not in state.model_choice_histories
    assert list(state.origin_lru.keys()) == ["o2"]


def test_cleanup_origin_removes_all_runtime_state() -> None:
    state = RuntimeState()
    state.active_reply_stacks["origin"].append("stack")
    state.model_choice_histories["origin"].append("hist")
    state.touch_origin("origin", max_origins=10)

    state.cleanup_origin("origin")

    assert "origin" not in state.active_reply_stacks
    assert "origin" not in state.model_choice_histories
    assert "origin" not in state.origin_lru
