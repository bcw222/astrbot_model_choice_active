# astrbot_plugin_model_choice_active

**Version**: `v1.0.0`
***Author**: [bcw222](https://github.com/bcw222)

从 [astrbot_plugin_astrbot_enhance_mode](https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode) 分离出的独立精简插件，包含两项功能：

1. **模型主动回复**：让模型自主决定是否主动介入群聊
2. **并发消息丢弃**：处理消息期间忽略新消息，回复完成后自动回复最新一条

## 功能说明

### 模型主动回复（`active_reply`）

在群聊中，每累计 N 条消息后，插件会将这批消息连同人格面具信息一起发给指定模型，由模型判断是否需要主动介入回复。模型只需输出 `REPLY` 或 `SKIP`。

- 仅对群聊生效，私聊和被 @ / 唤醒命令触发的消息不参与判定
- 支持白名单限制触发范围（按 `unified_msg_origin` 或群号）
- 判定模型可独立配置，与当前会话使用的模型解耦
- 判定时可附带额外历史上下文，帮助模型做出更准确的判断

### 并发消息丢弃（`discard_concurrent`）

当机器人正在处理某条消息（等待 LLM 回复等）时，若同一会话中又有新消息到来：

1. 忽略新消息，不重复触发 LLM
2. 收集期间所有被忽略的消息内容
3. 当前消息回复完成后，将所有积压消息拼接后注入到新一轮 LLM 请求，确保内容不丢失

可避免用户连续发消息导致并发混乱，同时保证所有消息最终都被处理。

## 安装

将插件目录放到 `data/plugins/`，重启 AstrBot 即可。

建议同时关闭 AstrBot 内置主动回复（`active_reply.enable`）和内置引用回复（`reply_with_quote`），避免重叠。

## 配置

### `active_reply`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用主动回复 |
| `model_stack_size` | int | `8` | 累计多少条消息后触发一次模型判定 |
| `model_history_messages` | int | `0` | 判定时附带的额外历史条数（0 表示不附带） |
| `model_choice_provider_id` | string | `""` | 判定模型的提供商 ID，留空使用当前会话默认提供商 |
| `model_choice_prompt` | string | 见下方 | 判定提示词，支持占位符 |
| `whitelist` | string | `""` | 逗号分隔的来源/群号白名单，留空表示所有群 |

#### 判定提示词占位符

| 占位符 | 说明 |
| --- | --- |
| `{stack_size}` | 本次判定的消息条数 |
| `{messages}` | 消息列表文本 |
| `{history_count}` | 附带的历史条数 |
| `{history_context}` | 历史上下文文本 |
| `{persona_name}` | 当前人格面具名称 |
| `{persona_mask}` | 当前人格面具提示词 |

### `discard_concurrent`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用并发消息丢弃 |

### `global_settings`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `lru_cache.max_origins` | int | `500` | 最多保留多少个会话来源的运行时状态 |
| `timeouts.model_choice_sec` | float | `45` | 模型判定调用超时（秒） |

## 项目结构

```text
astrbot_plugin_model_choice_active/
├── main.py
├── plugin_config.py
├── runtime_state.py
├── _conf_schema.json
├── metadata.yaml
├── requirements.txt
└── README.md
```
