# SGGK Test SDK Harness

This repository contains the standalone SGGK SDK test harness.

> **首次部署请先阅读：[SGGK Harness 全新 Windows 电脑部署与使用指南（中文）](docs/SGGK_HARNESS_全新电脑部署与使用指南.md)**<br>
> 包含 GitHub/离线 bundle、离线依赖、SGGK SDK、内网 Qwen Message API、逐轮审查、ABC 数据和故障排查。

配套中文手册：

- [生成代码与测试用例审查指南](docs/SGGK_HARNESS_生成代码与测试用例审查指南.md)
- [源码风险驱动测试指南](docs/SGGK_HARNESS_源码风险驱动测试指南.md)

普通用户只使用根目录入口。输入一个 SGGK public function，Harness 会自行管理测试表单、任务 ID、候选、轮次、哈希、runner 和 JSON 证据：

```powershell
.\harness.ps1 start api_boolean
.\harness.ps1 comment "增加退化输入、近容差相交和空结果检查"
.\harness.ps1 comment "这一版可以开始执行真实测试。"
```

`start` 通过同一个 Message API contract 生成第 1 轮中文审查文档；每条 `comment` 都会保留原文并形成不可变事件，由 Qwen 给出中文理解。涉及方案修改的 comment 会生成新的不可变候选/审查轮次；问题留在当前轮回答，拒绝会终止任务。当 comment 被解释为明确批准且宿主也检测到清晰的执行同意时，Harness 绑定并执行当前最新轮次，随后自动写出 `final_report.zh-CN.md`。辅助命令为 `status`、`show` 和 `retry`。普通用户不需要填写表单，也不需要提供 ID、hash、round、manifest、JSON 或 runner 路径。

The SGGK SDK itself is intentionally not included. Keep the SDK, license files, build outputs, and generated artifacts outside git. Point `SGGK_SDK_DIR` at the local SDK and use the checked-in preset:

```powershell
$env:SGGK_SDK_DIR = "C:\path\to\SGGK"
Push-Location .\test_harness
cmake --preset windows-local
cmake --build --preset windows-release
Pop-Location
```

The authoring path is fully Message API based:

```text
public function -> parallel Qwen candidates -> fixed pre-review gates
                -> immutable Chinese review rounds -> user approval
                -> real SDK/plugin execution -> final Chinese report
                -> qualification/replay/TopoTrack -> candidate bug report
```

Start with:

- `test_harness/HARNESS_ARCHITECTURE.md` for trust boundaries and the complete data flow;
- `test_harness/MESSAGE_API_ENDPOINTS.md` for the identical intranet/simulator Message API contract and endpoint configuration;
- `test_harness/INTERFACE_TEST_MATRIX.md` for built-in and plugin API coverage;
- `test_harness/README.md` for the user workflow plus advanced runner and campaign internals;
- `docs/ABC_DATASET_LARGE_TEST_PLAN.md` for staged large-scale execution.
