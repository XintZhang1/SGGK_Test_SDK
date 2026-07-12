# 内网 Message API 配置

SGGK Harness 只有一个模型协议、一个用户流程和一个 provider：内网 Qwen
Message API。没有人工产物入口、外部模拟 provider 或自动回退路径。

本地 UI 中填写：

- 内网服务的 OpenAI-compatible base URL；
- Qwen3.6-35B-A3B 的实际 model id；
- 可选 API key；
- 可选的内网 CA PEM 文件；
- SGGK SDK、源码和本仓库 runner 路径。

服务必须在 `choices[0].message.content` 返回 Harness 约定的 JSON。模型响应始终
是不可信候选，只有宿主固定代码能够规范化、验证、编译、执行和提升候选。模型
不能提供命令、runner、环境变量、凭据、输出路径或执行权限。

普通用户通过 `SGGK_Harness_UI.cmd` 完成接口输入、产物审查、自然语言修改、明确
批准、真实 SDK 执行和报告查看。环境变量 CLI 仅保留作维护诊断用途。
