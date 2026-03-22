from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL_CHOICE_PROMPT = (
    "你当前的人格面具是：{persona_name}\n"
    "人格设定如下：\n{persona_mask}\n\n"
    "**重要背景**：你正在一个公开群聊中。这不是私人对话，群聊中有真实用户，也可能有其他 AI 助手或 Bot 角色。\n"
    "你的发言应有明确价值，不要随意打断对话。请严格遵守你的人格设定，仅在确有必要时才发言，大多数情况应选择 SKIP。\n\n"
    "以下是最近 {stack_size} 条群聊消息：\n"
    "{messages}\n\n"
    "额外历史上下文（最近 {history_count} 条）：\n"
    "{history_context}\n\n"
    "请严格站在该人格的角度判断你是否应该主动发言。"
    "判断标准：仅当你的人格在此时机**必须**发言（例如对话明确等待你、你的角色职责要求你响应）时才输出 REPLY；"
    "其他所有情况（包括发言时机不到、非你职责、已有他人回应等）都应输出 SKIP。"
    "如果需要发言，只输出 REPLY；否则只输出 SKIP。"
)

# 默认正则：匹配模型输出中包含 REPLY 关键词（忽略大小写，支持词边界）
DEFAULT_REPLY_PATTERN = r"(?i)\bREPLY\b"
# 默认正则：匹配模型输出中包含 SKIP 关键词（忽略大小写，支持词边界）
DEFAULT_SKIP_PATTERN = r"(?i)\bSKIP\b"


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_pos_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _parse_whitelist(value: Any) -> list[str]:
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(token).strip() for token in value if str(token).strip()]
    return []


@dataclass(frozen=True)
class ActiveReplyConfig:
    enable: bool = False
    model_stack_size: int = 8
    model_history_messages: int = 0
    model_choice_provider_id: str = ""
    model_choice_prompt: str = DEFAULT_MODEL_CHOICE_PROMPT
    model_choice_reply_pattern: str = DEFAULT_REPLY_PATTERN
    model_choice_skip_pattern: str = DEFAULT_SKIP_PATTERN
    # 当 REPLY/SKIP 均未匹配时重新判定，最多重试次数（0 表示不重试）
    model_choice_max_retries: int = 2
    # 重试耗尽后的默认行为：True=REPLY（主动回复），False=SKIP（跳过）
    model_choice_retry_default_reply: bool = False
    whitelist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiscardConcurrentConfig:
    enable: bool = False
    notify_user: bool = True


@dataclass(frozen=True)
class GlobalLruConfig:
    max_origins: int = 500


@dataclass(frozen=True)
class GlobalTimeoutConfig:
    model_choice_sec: float = 45.0


@dataclass(frozen=True)
class GlobalSettingsConfig:
    lru_cache: GlobalLruConfig = field(default_factory=GlobalLruConfig)
    timeouts: GlobalTimeoutConfig = field(default_factory=GlobalTimeoutConfig)


@dataclass(frozen=True)
class PluginConfig:
    active_reply: ActiveReplyConfig = field(default_factory=ActiveReplyConfig)
    discard_concurrent: DiscardConcurrentConfig = field(default_factory=DiscardConcurrentConfig)
    global_settings: GlobalSettingsConfig = field(default_factory=GlobalSettingsConfig)

    @property
    def active_reply_enabled(self) -> bool:
        return self.active_reply.enable


def parse_plugin_config(raw: dict[str, Any] | None) -> PluginConfig:
    raw = raw or {}

    active_reply_raw = raw.get("active_reply", {})
    active_reply = ActiveReplyConfig(
        enable=_to_bool(active_reply_raw.get("enable"), False),
        model_stack_size=max(1, _to_int(active_reply_raw.get("model_stack_size"), 8)),
        model_history_messages=max(
            0, _to_int(active_reply_raw.get("model_history_messages"), 0)
        ),
        model_choice_provider_id=str(
            active_reply_raw.get("model_choice_provider_id") or ""
        ).strip(),
        model_choice_prompt=str(
            active_reply_raw.get("model_choice_prompt") or DEFAULT_MODEL_CHOICE_PROMPT
        ),
        model_choice_reply_pattern=str(
            active_reply_raw.get("model_choice_reply_pattern") or DEFAULT_REPLY_PATTERN
        ),
        model_choice_skip_pattern=str(
            active_reply_raw.get("model_choice_skip_pattern") or DEFAULT_SKIP_PATTERN
        ),
        whitelist=_parse_whitelist(active_reply_raw.get("whitelist", "")),
    )

    discard_concurrent_raw = raw.get("discard_concurrent", {})
    discard_concurrent = DiscardConcurrentConfig(
        enable=_to_bool(discard_concurrent_raw.get("enable"), False),
        notify_user=_to_bool(discard_concurrent_raw.get("notify_user"), True),
    )

    global_settings_raw = raw.get("global_settings", {})
    lru_raw = global_settings_raw.get("lru_cache", {})
    timeouts_raw = global_settings_raw.get("timeouts", {})
    global_settings = GlobalSettingsConfig(
        lru_cache=GlobalLruConfig(
            max_origins=max(1, _to_int(lru_raw.get("max_origins"), 500))
        ),
        timeouts=GlobalTimeoutConfig(
            model_choice_sec=_to_pos_float(timeouts_raw.get("model_choice_sec"), 45.0),
        ),
    )

    return PluginConfig(
        active_reply=active_reply,
        discard_concurrent=discard_concurrent,
        global_settings=global_settings,
    )
