# SiliconFlow GLM-5.2 Message API 配置

外网版本的默认且唯一生产模型 profile 是 `siliconflow`，通过 SiliconFlow 的
OpenAI-compatible Chat Completions API 调用 `zai-org/GLM-5.2`：

| 配置 | 默认值 / 来源 |
|---|---|
| profile | `siliconflow` |
| base URL | `https://api.siliconflow.cn/v1` |
| model | `zai-org/GLM-5.2` |
| API key | Windows Credential Manager（UI）或 `SILICONFLOW_API_KEY`（维护 CLI） |
| 默认思考模式 | `disabled`（实测完整提示约 40–70 秒返回；可手动改为 `enabled` / `omit`） |
| 外网结构化生成 | SSE 流式接收，宿主聚合后按同一 JSON 契约校验 |
| 单次空闲超时 | 300 秒；完整读超时不自动重试 |
| 流大小边界 | 候选内容 16 MiB；原始 SSE 线缆流 256 MiB |
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

## 咨询性视觉复核 profile（不参与任何结论）

`siliconflow_vision` 是独立于授权生成的视觉复核 profile：锁定同一 base URL，模型锁定为
`Qwen/Qwen3-VL-32B-Instruct`，与 `siliconflow` 共用 `SILICONFLOW_API_KEY`；
`SILICONFLOW_VISION_BASE_URL` / `SILICONFLOW_VISION_MODEL` / `SILICONFLOW_VISION_CA_BUNDLE`
若设置也必须分别精确等于锁定值（base URL 与上述一致）。默认不发送 `enable_thinking`，
使用与授权链路相同的 SSE 流式接收与失败关闭语义。

视觉复核只发送宿主渲染的几何预览图（重编码 PNG，长边 ≤1600px，单张 ≤2 MiB，合计
≤12 MiB，每次任务 ≤8 张）与固定中文提示，输出 `visual_review_report` 仅为咨询性证据：
不参与门禁、批准、执行或失败归因，不改变任何候选或状态机。请求端只持久化图片的
SHA-256 与字节数，不持久化像素数据。`proprietary_source` 会话不发送任何图片；
API key 缺失或 profile 未配置时视觉复核只记录提示并跳过。

## 请求与响应契约

Harness 向以下地址发起 OpenAI-compatible SSE 流式请求：

```text
POST https://api.siliconflow.cn/v1/chat/completions
```

最小请求参数为 `model`、`messages`、`temperature`、`max_tokens` 和 `stream: true`。
外网 profile 默认显式发送 `enable_thinking: false`，避免长思考让完整代码生成长期不结束；
设置中仍可手动启用，或省略该字段。结构化输出模式若被 endpoint
拒绝，客户端会在同一受限尝试中降级到普通 `message.content` JSON；不会切换模型、
provider 或 endpoint。

客户端以 64 KiB 有界块增量读取 SSE。服务的 `delta.content` 会在内存中聚合为
`choices[0].message.content`；聚合结果必须是恰好一个 Harness 约定的 JSON 对象，且其
UTF-8 编码不得超过 16 MiB。`reasoning_content` 不计入候选上限，只增量记录字符数、
字节数与 SHA-256，不作为候选，也不保留或持久化原文。原始 SSE 线缆流另有独立的
256 MiB 硬上限。

只有同时收到显式成功 `finish_reason=stop` 与 `[DONE]` 的完整流才可产生候选；缺少任一
结束信号、结束后仍出现数据、UTF-8/事件格式错误或超过任一大小上限都会失败关闭。
模型响应始终是不可信候选；只有宿主固定代码能够规范化、验证、编译、执行和提升候选。
模型不能提供命令、runner、环境变量、凭据、输出路径或执行权限。

## 数据边界与失败语义

`siliconflow` 属于 `external` profile category。公开接口任务会显式绑定该 profile；
标记为 `proprietary_source` 的源码任务仍只允许显式 `intranet` profile，不会自动
发送到 SiliconFlow，也不存在失败后的 provider fallback。

- `401`：key 缺失、失效或格式错误；
- `402` / quota / balance：endpoint 可达，但账户额度不足；
- `404` / `model_not_found`：账户不可用或 model id 错误；
- `429` / `5xx`：按固定次数和上限退避重试；
- 完整读超时：返回带模型、thinking 与 token budget 的明确错误，不自动重复长请求；
- 响应截断、非 UTF-8、非精确 JSON 或 schema/固定门禁失败：不提升候选，可在受限
  repair budget 内请求完整修复。

所有失败都保留去密后的状态、请求哈希和安全响应元数据，不保存 authorization header、
原始思考文本或可能回显凭据的 provider body。对于 SSE，记录只保留原始流的 SHA-256、
字节数、是否完整读完以及安全聚合元数据，供诊断与审计使用。
