# SGGK Harness 全新 Windows 电脑部署与使用指南

普通用户不需要安装 Python、CMake、pip 或操作 PowerShell。本仓库已经固定并携带运行
所需的第三方文件。

## 前置条件

- Windows x64；
- Visual Studio 2022，“使用 C++ 的桌面开发”工作负载；
- 通过公司批准介质提供的 SGGK SDK 与许可；
- 可访问的 SiliconFlow OpenAI-compatible Message API 和有效 API key。

SGGK SDK 与许可不进入仓库，也不包含在通用离线包中。

## 安装和启动

1. 将完整仓库复制到用于外网测试的 Windows 电脑。
2. 双击根目录 `install_offline.cmd`。它会把 CPython、wheels 和 CMake 解压到被 git
   忽略的 `.offline_runtime/`，校验全部二进制 SHA-256，并执行关键 import 验证。
3. 双击 `SGGK_Harness_UI.cmd`。默认浏览器打开本机 `127.0.0.1:8765`。
4. 展开“外网模型与本机路径”，填写：
   - 必填 SiliconFlow API key（base URL 与 model id 已锁定）；
   - 可选 CA PEM；
   - SGGK SDK 目录；
   - 构建后的 `sggk_case_runner.exe` 路径；
   - 可选 Siemens NX 安装根目录。
   ABC 数据不在通用设置中手工填目录：请在下方 ABC 面板下载全量数据，或校验并绑定
   已有的 `dataset_index.json`。
5. 保存后确认核心环境检查通过；ABC 与 NX 检查为按需启用的可选能力。

API key 只写入当前 Windows 用户的 Credential Manager。非秘密配置位于被忽略的
`artifacts/harness_ui/config.json`。

## 使用

在 UI 顶部输入一个公开接口名并点击“生成测试”。页面会持续显示七个阶段：环境配置、
接口解析、GLM-5.2 生成、审查与修改、执行批准、SDK 实测和结果报告。

“模型输出与 Harness 产物”区域列出当前 session 内可预览的 Markdown、JSON、C++、
Python 和日志。内容按纯文本渲染，不执行模型返回的 HTML 或脚本。

对方案有修改时，在“自然语言审查意见”中描述需求。满意后点击“批准并执行 SDK 测试”；
宿主会把批准绑定到当前不可变候选、Prompt、审查包、轮次和 runner。执行失败可点击
“重试执行”。

## 离线包内容与验证

`offline_bundle/manifest.json` 记录：

- CPython 3.11.9 Windows x64 embeddable；
- CMake 4.3.3 Windows x64 portable；
- Visual C++ x64 Redistributable；
- runtime 和测试开发所需的全部 CPython 3.11 Windows wheels；
- 每个文件的精确大小、SHA-256 和来源。

安装不调用网络，也不修改系统 Python。完整细节见
[离线 UI 部署指南](SGGK_HARNESS_离线UI部署指南.md)。

## 安全边界

- Web 服务只绑定 loopback、校验本机 Host，不监听局域网地址；
- 修改请求要求随机 CSRF token；
- API key 不进入 JSON、日志和模型产物；
- 产物预览限定在当前 session，拒绝路径穿越和非文本文件；
- 模型不能指定命令、runner、cwd、环境变量、凭据或执行权限；
- 生产外网 profile 只调用锁定的 SiliconFlow GLM-5.2 endpoint，不存在 provider、模型或
  endpoint 自动 fallback；
- 外网任务必须显式标记为 `public_interface`；SGGK 源码与源码证据始终留在本机，不会
  发送到 SiliconFlow。需要源码诊断时必须显式切换到受控的 legacy `intranet` profile。

维护者如需底层诊断、campaign、ABC 数据或门禁工具，参阅
`test_harness/README.md` 和 `docs/ABC_DATASET_LARGE_TEST_PLAN.md`。普通用户无需调用这些
命令行工具。
