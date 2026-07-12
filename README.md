# SGGK Test SDK Harness

这是面向内网 Qwen3.6-35B-A3B Message API 的 SGGK SDK 测试 Harness。普通用户通过
本地网页 UI 完成公开接口输入、测试代码/用例生成、产物审查、修改、批准、真实 SDK
执行和 bug 证据查看，不需要操作 PowerShell。

## 新电脑离线启动

目标电脑只需预先安装 Visual Studio 2022，并包含“使用 C++ 的桌面开发”工作负载：

1. 拷贝完整仓库和公司内部提供的 SGGK SDK/许可；
2. 双击 `install_offline.cmd`；
3. 双击 `SGGK_Harness_UI.cmd`；
4. 在浏览器的“内网运行配置”中填写 Message API、模型、SDK、可选源码和 runner；
5. 环境检查通过后输入一个 public function。

离线包包含 CPython 3.11、便携 CMake、全部固定版本 Python wheels 和 VC++ 运行库安装
介质，并通过 `offline_bundle/manifest.json` 记录大小与 SHA-256。SGGK SDK、许可和源码
属于公司资产，不进入 git，需通过批准的内网介质提供。

详细步骤见 [离线 UI 部署指南](docs/SGGK_HARNESS_离线UI部署指南.md)。

## 工作流

```text
public function -> 接口/源码解析 -> 并行 Qwen 候选 -> 固定门禁
                -> UI 查看文档与代码 -> 自然语言修改 -> 宿主批准绑定
                -> 真实 SDK 执行 -> 重放/归因/缩减 -> 最终报告
```

内网 Qwen Message API 是唯一模型 provider。模型输出始终是不可信候选；只有宿主固定
代码能够规范化、验证、编译、执行和提升候选。不存在人工产物入口、外部模拟 provider
或自动 fallback。

主要文档：

- [Harness 架构与信任边界](test_harness/HARNESS_ARCHITECTURE.md)
- [内网 Message API 配置](test_harness/MESSAGE_API_ENDPOINTS.md)
- [生成代码与测试用例审查指南](docs/SGGK_HARNESS_生成代码与测试用例审查指南.md)
- [源码风险驱动测试指南](docs/SGGK_HARNESS_源码风险驱动测试指南.md)
- [大规模 ABC 测试计划](docs/ABC_DATASET_LARGE_TEST_PLAN.md)

`harness.ps1` 仅作为维护者诊断兼容入口保留；普通使用、配置和产物查看全部通过 UI。
