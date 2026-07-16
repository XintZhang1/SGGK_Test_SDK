# SGGK Test SDK Harness

这是面向 SiliconFlow `zai-org/GLM-5.2` Message API 的 SGGK SDK 测试 Harness。普通用户通过
本地网页 UI 完成公开接口输入、测试代码/用例生成、产物审查、修改、批准、真实 SDK
执行和 bug 证据查看，不需要操作 PowerShell。

## 新电脑离线启动

目标电脑需预先安装 Visual Studio 2022 或 CMake 已支持的更新版本（当前已验证
Visual Studio 2026），并包含“使用 C++ 的桌面开发”工作负载。Harness UI 会自动选择
本机已安装且与 CMake 匹配的最新生成器：

1. 拷贝完整仓库和公司内部提供的 SGGK SDK/许可；
2. 双击 `install_offline.cmd`；
3. 双击 `SGGK_Harness_UI.cmd`；
4. 点击环境检查旁的“配置”，在“外网模型与本机路径”中填写 SiliconFlow API key、SDK
   和 runner；ABC 数据通过独立 ABC 面板下载或绑定索引；
5. 核心环境检查通过后输入一个 public function。

离线包包含 CPython 3.11、便携 CMake、全部固定版本 Python wheels 和 VC++ 运行库安装
介质，并通过 `offline_bundle/manifest.json` 记录大小与 SHA-256。SGGK SDK、许可和源码
属于公司资产，不进入 git，需通过批准的内网介质提供。

详细步骤见 [离线 UI 部署指南](docs/SGGK_HARNESS_离线UI部署指南.md)。

## 工作流

```text
public function -> 接口能力解析 -> 并行 GLM-5.2 候选 -> 固定门禁
                -> UI 查看文档与代码 -> 自然语言修改 -> 宿主批准绑定
                -> 真实 SDK 执行 -> 重放/归因/缩减 -> 最终报告
```

SiliconFlow GLM-5.2 Message API 是外网版本的默认生产模型 provider。模型输出始终是
不可信候选；只有宿主固定代码能够规范化、验证、编译、执行和提升候选。不存在人工产物
入口、隐式 provider 切换或自动 fallback。受保护源码仍受 profile 数据边界约束，不会
自动发送到外部 endpoint。

## 外网版增强

- UI 可生成 ABC v00 全量计划、拉取全部 STEP + meta chunks、显示字节/归档进度、取消并
  从 `.part` 与归档缓存续传；也可有界检查并绑定含可验证 `dataset_index.json` 的已有
  fetch 根目录或直接绑定该索引文件。
- 绑定 ABC STEP 索引后，`step_import` 会启用固定 `abc_step_import` campaign profile。
  外部绝对路径只由宿主绑定，不会出现在模型 prompt 中。
- UI 会静态检测 Siemens NX 安装；只有用户明确点击后才运行真实 NX Python 探针。
  Harness journal runner 使用固定脚本、路径白名单、`shell=False`、超时和进程树终止。

主要文档：

- [Harness 架构与信任边界](test_harness/HARNESS_ARCHITECTURE.md)
- [SiliconFlow GLM-5.2 Message API 配置](test_harness/MESSAGE_API_ENDPOINTS.md)
- [生成代码与测试用例审查指南](docs/SGGK_HARNESS_生成代码与测试用例审查指南.md)
- [源码风险驱动测试指南](docs/SGGK_HARNESS_源码风险驱动测试指南.md)
- [大规模 ABC 测试计划](docs/ABC_DATASET_LARGE_TEST_PLAN.md)
- [NX Python API 环境与安全调用](test_harness/NX_PYTHON_API.md)

`harness.ps1` 仅作为维护者诊断兼容入口保留；普通使用、配置和产物查看全部通过 UI。
