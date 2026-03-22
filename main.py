import asyncio
import datetime
import re
import traceback
import uuid
from zoneinfo import ZoneInfo

from astrbot.api import logger, sp, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import Provider, ProviderRequest

from .plugin_config import PluginConfig, parse_plugin_config
from .runtime_state import RuntimeState


class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config = config or {}
        self.runtime = RuntimeState()
        self._display_timezone = self._resolve_config_timezone()
        # key: unified_msg_origin（每个 Bot 独立队列）
        # value: {
        #   "processing": bool,
        #   "owner_uid": str,
        #   "owner_event_id": int|None,        # id(event) of lock owner
        #   "pending_count": int,              # number of pending (blocked) events
        #   "last_pending_event_id": int|None, # id(event) of last pending event
        #   "combined_prompt": str,            # merged prompt of all pending events
        # }
        self._sessions: dict = {}
        logger.info(
            "model-choice-active | plugin initialized | timezone=%s",
            self._display_timezone,
        )

    def _get_session(self, uid: str) -> dict:
        if uid not in self._sessions:
            self._sessions[uid] = {
                "processing": False,
                "owner_uid": "",
                "owner_event_id": None,
                "pending_count": 0,
                "last_pending_event_id": None,
                "combined_prompt": "",
            }
        return self._sessions[uid]

    @staticmethod
    def _get_session_key(event: AstrMessageEvent) -> str:
        """每个 Bot 独立维护自己的处理队列（使用 unified_msg_origin）。"""
        return event.unified_msg_origin

    def _cfg(self) -> PluginConfig:
        return parse_plugin_config(self.config)

    def _touch_origin(self, origin: str, cfg: PluginConfig) -> None:
        self.runtime.touch_origin(origin, cfg.global_settings.lru_cache.max_origins)

    def _resolve_config_timezone(self) -> str:
        base_cfg = self.context.get_config()
        if not isinstance(base_cfg, dict):
            return "Asia/Shanghai"
        timezone_name = str(base_cfg.get("timezone") or "").strip()
        return timezone_name or "Asia/Shanghai"

    def _resolve_tzinfo(self) -> datetime.tzinfo:
        timezone_name = self._resolve_config_timezone()
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:
                return datetime.timezone.utc

    @staticmethod
    def _provider_label(provider: object | None) -> str:
        if provider is None:
            return "none"
        provider_id = getattr(provider, "provider_id", None) or getattr(
            provider, "id", None
        )
        if provider_id:
            return str(provider_id)
        model = getattr(provider, "model", None)
        cls_name = type(provider).__name__
        return f"{cls_name}({model})" if model else cls_name

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        cfg = self._cfg()
        self._display_timezone = self._resolve_config_timezone()
        logger.info(
            "model-choice-active | loaded | active_reply=%s discard_concurrent=%s lru_max_origins=%s timezone=%s",
            cfg.active_reply_enabled,
            cfg.discard_concurrent.enable,
            cfg.global_settings.lru_cache.max_origins,
            self._display_timezone,
        )

    def _allow_active_reply(self, event: AstrMessageEvent, cfg: PluginConfig) -> bool:
        ar = cfg.active_reply
        if not cfg.active_reply_enabled:
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        if event.is_at_or_wake_command:
            return False
        if ar.whitelist and (
            event.unified_msg_origin not in ar.whitelist
            and (event.get_group_id() and event.get_group_id() not in ar.whitelist)
        ):
            return False
        return True

    async def _resolve_persona_mask(self, event: AstrMessageEvent) -> tuple[str, str]:
        persona_id = ""
        try:
            session_service_config = await sp.get_async(
                scope="umo",
                scope_id=event.unified_msg_origin,
                key="session_service_config",
                default={},
            )
            if isinstance(session_service_config, dict):
                persona_id = str(session_service_config.get("persona_id") or "").strip()
        except Exception as e:
            logger.debug(f"model-choice-active | 获取 session persona 失败: {e}")

        if not persona_id:
            try:
                curr_cid = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin,
                    )
                )
                if curr_cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        event.unified_msg_origin,
                        curr_cid,
                    )
                    if conv and conv.persona_id:
                        persona_id = str(conv.persona_id).strip()
            except Exception as e:
                logger.debug(f"model-choice-active | 获取 conversation persona 失败: {e}")

        if not persona_id:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            persona_id = str(
                cfg.get("provider_settings", {}).get("default_personality") or ""
            ).strip()

        if persona_id == "[%None]":
            return "none", "No persona mask."

        persona = None
        if persona_id:
            try:
                persona = next(
                    (
                        p
                        for p in self.context.persona_manager.personas_v3
                        if p.get("name") == persona_id
                    ),
                    None,
                )
            except Exception:
                persona = None

        if not persona:
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(
                    event.unified_msg_origin
                )
            except Exception:
                persona = {"name": "default", "prompt": ""}

        persona_name = str(persona.get("name") or "default")
        persona_prompt = str(persona.get("prompt") or "").strip()
        if not persona_prompt:
            persona_prompt = "You are a helpful and friendly assistant."
        return persona_name, persona_prompt

    def _resolve_model_choice_provider(
        self, event: AstrMessageEvent, cfg: PluginConfig
    ) -> Provider | None:
        provider_id = str(cfg.active_reply.model_choice_provider_id or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider and isinstance(provider, Provider):
                logger.debug(
                    "model-choice-active | model_choice provider from config | provider=%s",
                    self._provider_label(provider),
                )
                return provider
            logger.warning(
                "model-choice-active | 配置的 model_choice_provider_id 无效或类型不匹配: %s",
                provider_id,
            )

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider and isinstance(provider, Provider):
            logger.debug(
                "model-choice-active | model_choice provider fallback to current | provider=%s",
                self._provider_label(provider),
            )
            return provider
        return None

    async def _judge_model_choice(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        origin: str,
        messages: list[str],
        trigger_reason: str,
    ) -> bool:
        ar = cfg.active_reply
        history = self.runtime.model_choice_histories[origin]
        history_context_lines = []
        if ar.model_history_messages > 0:
            history_context_lines = history[-ar.model_history_messages :]
        history_context = (
            "\n".join(history_context_lines)
            if history_context_lines
            else "(disabled or no additional history)"
        )

        logger.info(
            "model-choice-active | model_choice | 开始判定 | "
            f"origin={origin} trigger={trigger_reason} stack_size={len(messages)} "
            f"history={len(history_context_lines)}"
        )

        provider = self._resolve_model_choice_provider(event, cfg)
        if not provider:
            logger.error("model-choice-active | 未找到可用提供商，无法执行模型选择触发")
            return False

        persona_name, persona_mask = await self._resolve_persona_mask(event)
        prompt_tmpl = ar.model_choice_prompt
        try:
            judge_prompt = prompt_tmpl.format(
                stack_size=len(messages),
                messages="\n".join(messages),
                history_count=len(history_context_lines),
                history_context=history_context,
                persona_name=persona_name,
                persona_mask=persona_mask,
            )
        except Exception:
            judge_prompt = (
                f"{prompt_tmpl}\n\n"
                f"人格面具({persona_name}):\n{persona_mask}\n\n"
                f"最近消息:\n{chr(10).join(messages)}\n\n"
                f"额外历史上下文({len(history_context_lines)}):\n{history_context}\n\n"
                "请仅输出 REPLY 或 SKIP。"
            )

        reply_pattern = ar.model_choice_reply_pattern
        skip_pattern = ar.model_choice_skip_pattern
        max_retries = ar.model_choice_max_retries
        retry_default_reply = ar.model_choice_retry_default_reply

        def _match_decision(raw: str) -> str | None:
            """返回 'REPLY'、'SKIP' 或 None（无法判定）。"""
            try:
                if reply_pattern and re.search(reply_pattern, raw):
                    return "REPLY"
            except re.error as exc:
                logger.warning(
                    "model-choice-active | model_choice_reply_pattern 正则无效(%s)，降级为关键词匹配", exc
                )
                if "REPLY" in raw.upper():
                    return "REPLY"
            try:
                if skip_pattern and re.search(skip_pattern, raw):
                    return "SKIP"
            except re.error as exc:
                logger.warning(
                    "model-choice-active | model_choice_skip_pattern 正则无效(%s)，降级为关键词匹配", exc
                )
                if "SKIP" in raw.upper():
                    return "SKIP"
            return None

        for attempt in range(max_retries + 1):
            try:
                judge_resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=judge_prompt,
                        session_id=uuid.uuid4().hex,
                        persist=False,
                    ),
                    timeout=cfg.global_settings.timeouts.model_choice_sec,
                )
            except asyncio.TimeoutError:
                logger.error("model-choice-active | 模型选择触发判定超时（第 %d 次）", attempt + 1)
                return False
            except Exception as e:
                logger.error("model-choice-active | 模型选择触发判定失败（第 %d 次）: %s", attempt + 1, e)
                return False

            decision_raw = (judge_resp.completion_text or "").strip()
            logger.debug(
                "model-choice-active | model_choice | LLM I/O | origin=%s attempt=%d\n"
                "--- prompt ---\n%s\n--- output ---\n%s",
                origin, attempt + 1, judge_prompt, decision_raw,
            )
            decision = _match_decision(decision_raw)
            if decision == "REPLY":
                logger.info(
                    "model-choice-active | model_choice | 判定通过(REPLY) | "
                    f"origin={origin} trigger={trigger_reason} persona={persona_name} attempt={attempt + 1}"
                )
                return True
            if decision == "SKIP":
                logger.info(
                    "model-choice-active | model_choice | 判定拒绝(SKIP) | "
                    f"origin={origin} trigger={trigger_reason} persona={persona_name} "
                    f"output={decision_raw} attempt={attempt + 1}"
                )
                return False
            logger.warning(
                "model-choice-active | model_choice | 无法判定（第 %d/%d 次），output=%r",
                attempt + 1, max_retries + 1, decision_raw,
            )

        logger.warning(
            "model-choice-active | model_choice | 重试耗尽，使用默认值 %s | "
            "origin=%s trigger=%s persona=%s",
            "REPLY" if retry_default_reply else "SKIP",
            origin, trigger_reason, persona_name,
        )
        return retry_default_reply

    async def _need_active_reply(
        self, event: AstrMessageEvent, cfg: PluginConfig
    ) -> bool:
        if not self._allow_active_reply(event, cfg):
            return False

        origin = event.unified_msg_origin
        self._touch_origin(origin, cfg)

        ar = cfg.active_reply
        text = (event.message_str or "").strip() or "[Empty]"
        nickname = event.message_obj.sender.nickname
        sender_id = event.get_sender_id()
        stack = self.runtime.active_reply_stacks[origin]
        history = self.runtime.model_choice_histories[origin]

        stack.append(f"[{nickname}/{sender_id}]: {text}")
        history_line = (
            f"[{nickname}/{sender_id}/"
            f"{datetime.datetime.now().strftime('%H:%M:%S')}]: {text}"
        )
        history.append(history_line)

        history_limit = max(
            60,
            ar.model_stack_size * 6,
            ar.model_history_messages * 6,
        )
        if len(history) > history_limit:
            del history[:-history_limit]

        logger.info(
            "model-choice-active | model_choice | 栈填充 | "
            f"origin={origin} progress={len(stack)}/{ar.model_stack_size} "
            f"sender={sender_id}"
        )

        if len(stack) < ar.model_stack_size:
            return False

        messages = stack[-ar.model_stack_size :]
        stack.clear()
        return await self._judge_model_choice(
            event,
            cfg,
            origin,
            messages,
            trigger_reason="stack_full",
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        cfg = self._cfg()

        # 过滤当前 Bot 自身发出的消息（防止 Bot 回复自己造成自回复循环）
        sender_id = str(event.get_sender_id() or "")
        current_self_id = str(getattr(event, "self_id", "") or "")
        if sender_id and current_self_id and sender_id == current_self_id:
            logger.debug(
                "model-choice-active | on_group_message | 忽略自身消息（防自回复）| self_id=%s",
                current_self_id,
            )
            return

        uid = event.unified_msg_origin

        if not cfg.discard_concurrent.enable:
            # 不启用并发丢弃，直接走 active_reply 逻辑
            if cfg.active_reply_enabled:
                has_content = any(
                    isinstance(comp, (Plain, Image, Reply))
                    for comp in event.message_obj.message
                )
                if has_content:
                    need_active = await self._need_active_reply(event, cfg)
                    if need_active:
                        async for item in self._do_active_reply(event, cfg):
                            yield item
            return

        # ---- discard_concurrent 逻辑 ----
        session_key = self._get_session_key(event)
        session = self._get_session(session_key)

        if not session["processing"]:
            # 当前无锁，直接获锁处理
            session["processing"] = True
            session["owner_uid"] = uid
            session["owner_event_id"] = id(event)
            session["pending_count"] = 0
            session["last_pending_event_id"] = None
            session["combined_prompt"] = ""
            logger.debug(
                "model-choice-active | discard_concurrent | 锁状态: processing=True owner=%s | 会话 %s 获锁",
                uid, session_key,
            )
            if hasattr(event, "set_extra"):
                event.set_extra("_discard_concurrent_owner", True)
                event.set_extra("_discard_concurrent_session_key", session_key)
            async for item in self._handle_message_with_lock(event, cfg, session_key, uid):
                yield item
            return

        # 有锁：注册为积压消息，阻塞等待，不 stop_event
        session["pending_count"] += 1
        session["last_pending_event_id"] = id(event)
        msg_text = (event.message_str or "").strip()
        if msg_text:
            existing = session.get("combined_prompt", "")
            session["combined_prompt"] = (existing + "\n" + msg_text).strip() if existing else msg_text

        my_pending_id = id(event)
        pending_pos = session["pending_count"]
        logger.info(
            "model-choice-active | discard_concurrent | 锁状态: processing=True owner=%s pending=%d | "
            "会话 %s 阻塞等待（第 %d 条）: %r",
            session["owner_uid"],
            session["pending_count"],
            session_key,
            pending_pos,
            event.message_str,
        )

        # 阻塞等待锁释放（最多 120 秒）
        wait_ms = 0
        while session.get("processing", False) and wait_ms < 120000:
            await asyncio.sleep(0.1)
            wait_ms += 100

        if session.get("processing", False):
            logger.warning(
                "model-choice-active | discard_concurrent | 会话 %s 等待锁超时，放弃消息: %r",
                session_key, event.message_str,
            )
            event.stop_event()
            return

        # 锁已释放，检查自己是否是最后一条积压消息
        if session.get("last_pending_event_id") != my_pending_id:
            logger.debug(
                "model-choice-active | discard_concurrent | 会话 %s 非最后积压消息（pos=%d），stop pipeline",
                session_key, pending_pos,
            )
            event.stop_event()
            return

        # 是最后一条，获锁并以合并 prompt 重新处理（走完整 AstrBot pipeline）
        combined_prompt = session["combined_prompt"]
        total_pending = session["pending_count"]
        session["processing"] = True
        session["owner_uid"] = uid
        session["owner_event_id"] = id(event)
        session["pending_count"] = 0
        session["last_pending_event_id"] = None
        session["combined_prompt"] = ""
        logger.info(
            "model-choice-active | discard_concurrent | 会话 %s 最后积压消息（pos=%d，共 %d 条）获锁，合并 prompt 注入",
            session_key, pending_pos, total_pending,
        )
        if hasattr(event, "set_extra"):
            event.set_extra("_discard_concurrent_owner", True)
            event.set_extra("_discard_concurrent_session_key", session_key)
        async for item in self._handle_message_with_lock(
            event, cfg, session_key, uid, override_prompt=combined_prompt
        ):
            yield item

    async def _handle_message_with_lock(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        session_key: str,
        uid: str,
        override_prompt: str | None = None,
    ):
        """持有锁后，执行 active_reply 判定和 LLM 请求，完成后由 on_after_sent 释放锁。"""
        def _release(reason: str) -> None:
            s = self._get_session(session_key)
            if s["processing"] and s["owner_uid"] == uid:
                s["processing"] = False
                s["owner_uid"] = ""
                s["owner_event_id"] = None
                logger.debug(
                    "model-choice-active | discard_concurrent | 锁状态: processing=False | 会话 %s 释放锁（%s）",
                    session_key, reason,
                )

        if not cfg.active_reply_enabled:
            _release("active_reply disabled")
            return

        has_content = any(
            isinstance(comp, (Plain, Image, Reply))
            for comp in event.message_obj.message
        )
        if not has_content and not override_prompt:
            _release("no content")
            return

        need_active = await self._need_active_reply(event, cfg)
        if not need_active:
            _release("model_choice SKIP")
            return

        async for item in self._do_active_reply(
            event, cfg, session_key=session_key, uid=uid, override_prompt=override_prompt
        ):
            yield item

    async def _do_active_reply(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        session_key: str | None = None,
        uid: str | None = None,
        override_prompt: str | None = None,
    ):
        """触发 active_reply LLM 请求。yield request_llm 走完整 AstrBot pipeline（人格、知识库、工具注入等）。
        当 session_key 和 uid 均不为 None 时，锁将由 on_after_sent 在回复发送完成后释放。
        """
        def _release(reason: str) -> None:
            if session_key is None or uid is None:
                return
            s = self._get_session(session_key)
            if s["processing"] and s["owner_uid"] == uid:
                s["processing"] = False
                s["owner_uid"] = ""
                s["owner_event_id"] = None
                logger.debug(
                    "model-choice-active | discard_concurrent | 锁状态: processing=False | 会话 %s 释放锁（%s）",
                    session_key, reason,
                )

        provider = self.context.get_using_provider(event.unified_msg_origin)
        if not provider:
            logger.error("model-choice-active | 未找到任何 LLM 提供商，无法主动回复")
            _release("no provider")
            return
        try:
            prompt = override_prompt if override_prompt else event.message_str
            logger.info(
                "model-choice-active | active_reply triggered | origin=%s provider=%s session=%s override=%s",
                event.unified_msg_origin,
                self._provider_label(provider),
                session_key,
                bool(override_prompt),
            )
            if hasattr(event, "set_extra"):
                event.set_extra("_active_reply_triggered", True)
                event.set_extra("_active_reply_mode", "model_choice")

            session_curr_cid = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin,
                )
            )
            if not session_curr_cid:
                logger.error(
                    "model-choice-active | 当前未处于对话状态，无法主动回复，"
                    "请使用 /switch 或 /new 创建一个会话。"
                )
                _release("no conversation")
                return

            conv = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin,
                session_curr_cid,
            )
            if not conv:
                logger.error("model-choice-active | 未找到对话，无法主动回复")
                _release("conv not found")
                return

            # yield request_llm 走完整 AstrBot pipeline；锁由 on_after_sent 在回复发送完成后释放
            yield event.request_llm(
                prompt=prompt,
                session_id=event.session_id,
                conversation=conv,
            )
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error(f"model-choice-active | 主动回复失败: {e}")
            _release("exception")

    @filter.after_message_sent()
    async def on_after_sent(self, event: AstrMessageEvent):
        """回复发送完成后释放并发锁。积压消息注入已在 on_group_message 里通过阻塞等待+重新处理实现。"""
        cfg = self._cfg()
        if not cfg.discard_concurrent.enable:
            return

        is_owner = False
        session_key = None
        if hasattr(event, "get_extra"):
            is_owner = bool(event.get_extra("_discard_concurrent_owner"))
            session_key = event.get_extra("_discard_concurrent_session_key")
        elif hasattr(event, "extras"):
            is_owner = bool(event.extras.get("_discard_concurrent_owner"))
            session_key = event.extras.get("_discard_concurrent_session_key")

        if session_key is None:
            session_key = self._get_session_key(event)

        if not is_owner:
            session_tmp = self._get_session(session_key)
            if session_tmp.get("owner_event_id") == id(event):
                is_owner = True

        if not is_owner:
            return

        uid = event.unified_msg_origin
        session = self._get_session(session_key)
        logger.debug(
            "model-choice-active | discard_concurrent | on_after_sent | "
            "session_key=%s processing=%s owner=%s current=%s pending=%d",
            session_key,
            session["processing"],
            session["owner_uid"],
            uid,
            session["pending_count"],
        )
        if not session["processing"]:
            return
        if session["owner_uid"] and session["owner_uid"] != uid:
            return

        session["processing"] = False
        session["owner_uid"] = ""
        session["owner_event_id"] = None
        logger.info(
            "model-choice-active | discard_concurrent | 锁状态: processing=False | "
            "会话 %s 释放锁（on_after_sent），pending=%d",
            session_key, session["pending_count"],
        )

    async def terminate(self) -> None:
        self._sessions.clear()
        logger.info("model-choice-active | plugin terminated")
