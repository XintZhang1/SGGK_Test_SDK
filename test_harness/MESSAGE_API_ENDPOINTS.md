# SiliconFlow GLM-5.2 Message API 配置

外网版本的默认且唯一生产模型 profile 是 `siliconflow`，通过 SiliconFlow 的
OpenAI-compatible Chat Completions API 调用 `zai-org/GLM-5.2`：

| 配置 | 默认值 / 来源 |
|---|---|
| profile | `siliconflow` |
| base URL | `https://api.siliconflow.cn/v1` |
| model | `zai-org/GLM-5.2` |
| API key | Windows Credential Manager（UI）或 `SILICONFLOW_API_KEY`（维护 CLI） |
| 默认思考模式 | `enabled`（可显式改为 `disabled` / `omit`） |
| 可选 CA | `SILICONFLOW_CA_BUNDLE` |

base URL 与 model 是非敏感默认值；API key 没有仓库默认值，缺失时配置会失败关闭。
key 不写入 JSON、日志、provenance、prompt 或模型输出，只在内存中的
`Authorization: Bearer ...` 请求头使用。不要把 key 写进命令历史或提交到 Git。

维护者如需从命令行运行，可在当前 PowerShell 会话设置：

```powershell
$env:SILICONFLOW_API_KEY = "<在本机粘贴 key>"
$env:SGGK_HARNESS_PROFILE = "siliconflow"
```

`SILICONFLOW_BASE_URL` 和 `SILICONFLOW_MODEL` 仅为部署兼容保留，若设置也必须分别
精确等于 `https://api.siliconflow.cn/v1` 和 `zai-org/GLM-5.2`。生产 profile 不接受
其他 HTTPS host、代理 endpoint 或 model。普通用户通过
`SGGK_Harness_UI.cmd` 保存 key；UI 只展示 `api_key_configured` 布尔值。

## 请求与响应契约

Harness 向以下地址发起非流式请求：

```text
POST https://api.siliconflow.cn/v1/chat/completions
```

最小请求参数为 `model`、`messages`、`temperature` 和 `max_tokens`。外网 profile
默认显式发送 `enable_thinking: true`，避免依赖 provider 端会变化的隐式默认值；运行
配置也可明确发送 `false` 或省略该字段。结构化输出模式若被 endpoint
拒绝，客户端会在同一受限尝试中降级到普通 `message.content` JSON；不会切换模型、
provider 或 endpoint。

服务必须在 `choices[0].message.content` 返回恰好一个 Harness 约定的 JSON 对象。
`reasoning_content` 只记录长度与 SHA-256，不作为候选，也不持久化原文。模型响应
始终是不可信候选；只有宿主固定代码能够规范化、验证、编译、执行和提升候选。
模型不能提供命令、runner、环境变量、凭据、输出路径或执行权限。

## 数据边界与失败语义

`siliconflow` 属于 `external` profile category。公开接口任务会显式绑定该 profile；
标记为 `proprietary_source` 的源码任务仍只允许显式 `intranet` profile，不会自动
发送到 SiliconFlow，也不存在失败后的 provider fallback。

- `401`：key 缺失、失效或格式错误；
- `402` / quota / balance：endpoint 可达，但账户额度不足；
- `404` / `model_not_found`：账户不可用或 model id 错误；
- `429` / `5xx`：按固定次数和上限退避重试；
- 响应截断、非 UTF-8、非精确 JSON 或 schema/固定门禁失败：不提升候选，可在受限
  repair budget 内请求完整修复。

所有失败都保留去密后的状态、请求哈希和安全响应元数据，不保存 authorization header、
原始思考文本或可能回显凭据的 provider body。
