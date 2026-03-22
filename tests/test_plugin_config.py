from __future__ import annotations

import math

from astrbot_plugin_model_choice_active.plugin_config import parse_plugin_config


def test_parse_plugin_config_defaults() -> None:
    cfg = parse_plugin_config(None)
    assert cfg.active_reply.enable is False
    assert cfg.active_reply.model_choice_provider_id == ""
    assert cfg.active_reply.model_stack_size == 8
    assert cfg.active_reply_enabled is False


def test_active_reply_enabled_when_enable_is_true() -> None:
    cfg = parse_plugin_config(
        {
            "active_reply": {"enable": True},
        }
    )
    assert cfg.active_reply_enabled is True


def test_active_reply_limits_are_normalized() -> None:
    cfg = parse_plugin_config(
        {
            "active_reply": {
                "model_stack_size": 0,
                "model_history_messages": -99,
                "model_choice_provider_id": "  provider-1  ",
                "whitelist": "a,b, c",
            },
            "global_settings": {
                "lru_cache": {"max_origins": 0},
                "timeouts": {"model_choice_sec": "0"},
            },
        }
    )

    assert cfg.active_reply.model_stack_size == 1
    assert cfg.active_reply.model_history_messages == 0
    assert cfg.active_reply.model_choice_provider_id == "provider-1"
    assert cfg.active_reply.whitelist == ["a", "b", "c"]
    assert cfg.global_settings.lru_cache.max_origins == 1
    assert math.isclose(cfg.global_settings.timeouts.model_choice_sec, 45.0)
