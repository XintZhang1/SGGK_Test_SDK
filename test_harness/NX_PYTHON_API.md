# NX Python API 后端与 Harness 契约

## 目标与边界

`test_harness.nx` 为 Windows 上的 Siemens NX Python API 提供三类能力：

1. 静态检测 NX 安装、`run_journal.exe` 与 Python 运行时证据；
2. 在隔离子进程中验证 `import NXOpen` 和 `NXOpen.Session.GetSession()`；
3. 通过白名单目录、参数限制、无 shell 命令和强制超时运行 Harness 管理的 Python journal。

导入 `test_harness.nx` 或执行静态检测时不会导入 `NXOpen`、启动 NX、连接许可证服务器或读取零件。
静态文件证据不能证明 NX Python API 可调用，只有显式执行的运行时探针会返回 `verified`。

NX journal 仍以当前 Windows 用户权限运行，能够访问该用户可访问的文件和 NX 数据。路径白名单防止 UI/命令边界注入，
但不是恶意 Python 代码沙箱；外部请求不得直接提交任意 journal 路径或源码。

## Python API

静态检测：

```python
from test_harness.nx import detect_nx_environment

report = detect_nx_environment(
    explicit_roots=[r"C:\Program Files\Siemens\NX2512"],
)
```

`explicit_roots` 可接受 NX 安装根目录、`NXBIN`/`UGII` 目录或其中的可执行文件。只要传入显式目录，
该配置就是权威选择；无效目录不会静默回退到其他已安装版本。未配置时按以下只读来源发现：

- `UGII_BASE_DIR`、`UGII_ROOT_DIR`、`NX_ROOT_DIR`；
- Windows HKLM 中常见的 Unigraphics Solutions/Siemens NX 安装项；
- `PATH` 中的 `run_journal.exe`/`ugraf.exe`；
- Program Files 下 Siemens 的直接子目录。

隔离探针：

```python
from test_harness.nx import probe_nx_python

report = probe_nx_python(
    explicit_roots=[configured_nx_root] if configured_nx_root else [],
    timeout_seconds=120,
)
```

探针只执行仓库内固定的 `test_harness/nx/runtime_probe.py`。父进程不会导入 NX 模块；
子进程输出使用随机 nonce 验证，stdout/stderr 只保留末尾 64 KiB，超时时终止 `run_journal.exe` 进程树。
探针可能启动后台 NX 并占用许可证，因此不得在 UI 启动、`/api/state` 轮询或静态健康检查中自动执行。

Harness journal：

```python
from test_harness.nx import execute_nx_journal

report = execute_nx_journal(
    repo_root / "test_harness" / "nx_journals" / "session_smoke.py",
    allowed_roots=[repo_root / "test_harness" / "nx_journals"],
    arguments=[case_id],
    explicit_roots=[configured_nx_root] if configured_nx_root else [],
    timeout_seconds=300,
)
```

只接受真实存在、解析后仍位于白名单根目录内的 `.py` 文件；最多 32 个参数，每个参数最多 4096 字符且不能含 NUL。
命令始终以参数数组和 `shell=False` 执行。Harness 应只调用仓库审查过或本地生成后经过验证的 journal。

## CLI

```powershell
python test_harness/tools/nx_runtime.py detect
python test_harness/tools/nx_runtime.py detect --nx-root "C:\Program Files\Siemens\NX2512"
python test_harness/tools/nx_runtime.py probe --timeout 120
python test_harness/tools/nx_runtime.py measure-step `
  --step artifacts/abc_step_import_smoke/_source/example.step `
  --measurement-out artifacts/nx_sggk_compare/example/nx_measurement.json `
  --timeout 300
python test_harness/tools/nx_runtime.py run `
  --journal test_harness/nx_journals/session_smoke.py `
  --allow-root test_harness/nx_journals `
  --arg case-001 `
  --timeout 300
```

所有命令向 stdout 输出 JSON；`--out PATH` 可额外写入报告。`ok=false` 时 CLI 返回非零状态。

`measure-step` 不接受 journal 路径，而是固定映射到仓库审查过的
`nx_journals/abc_step_measure.py`。该 journal 在临时毫米制 NX part 中导入一个 STEP，输出输入
SHA-256、NX 版本、body 数量、总面积和总绝对体积，并关闭且不保存临时 part。结果符合
`schemas/nx_step_measurement.schema.json`。

取得同一 STEP 的 SGGK `step_import` case artifact 后，用固定比较器生成 JSON 与中文报告：

```powershell
python test_harness/tools/compare_nx_sggk_step.py `
  --nx-measurement artifacts/nx_sggk_compare/example/nx_measurement.json `
  --sggk-case artifacts/abc_step_import_smoke/example_case `
  --out artifacts/nx_sggk_compare/example `
  --abs-tol 0.01 `
  --rel-tol 1e-5
```

比较器重新计算 SGGK case 内 `input/source.step` 或 `input/source.stp` 的 SHA-256，要求其与 NX
measurement 完全一致；NX 与 SGGK 导入都必须成功，body 数量精确相等。总面积和总绝对体积按
`abs(nx-sggk) <= abs_tol + rel_tol * max(abs(nx), abs(sggk))` 判定。输出符合
`schemas/nx_sggk_step_comparison.schema.json`；比较未通过时返回码为 `2`，输入 artifact 无效时返回
码为 `1`。

## 静态检测 JSON

静态检测符合 `schemas/nx_environment_report.schema.json`，顶层字段如下：

- `schema_version`: 当前为 `1`；
- `operation`: `detect`；
- `ok`: 选中安装是否已具备 `run_journal.exe`，不代表 NXOpen 已验证；
- `status`: `unsupported_platform`、`not_found`、`incomplete` 或 `ready_for_probe`；
- `selected_root`: 当前权威安装根目录；
- `installations`: 每个候选的来源、路径、静态能力和诊断；
- `diagnostics`: 稳定的 `code`、`severity`、`message`、`remediation` 数组。

探针报告的 `status` 为 `verified`、`unavailable`、`invalid_result`、`timed_out` 或 `launch_failed`；
`execution` 包含 `returncode`、`timed_out`、`duration_ms` 及有界输出尾部，`probe` 包含 Python/NXOpen 元数据和错误。
journal 报告的 `status` 为 `completed`、`failed`、`timed_out`、`launch_failed` 或
安装不可用时的 `unavailable`。

## UI 集成建议

设置字段：

```json
{
  "nx_root_dir": "",
  "nx_probe_timeout_seconds": 120.0
}
```

`nx_root_dir` 允许为空；保存时不要求存在，以便配置可移动/暂时离线的安装。探针超时建议限制为 5 到 600 秒。
更换 `nx_root_dir` 后必须清空旧探针结果；缓存的 `verified` 还必须同时匹配当前
`selected_root` 和 `run_journal.exe`，防止把另一版本的结果继续展示。

建议接口：

- `GET /api/nx/environment`：同步静态检测，绝不启动 NX；
- `POST /api/nx/probe`：带现有 CSRF token，通过 `JobManager` 异步执行并返回 `202`；
- `GET /api/state`：返回 `nx.detection` 与缓存的 `nx.probe`，未运行时为 `{"status":"not_run"}`。

UI 将静态的“NX Journal 环境”和显式探针得到的“NX Python API 验证”分开显示；
`ready_for_probe` 不能显示为 Python API 已可用。本机 HTTP 服务只接受当前 loopback
端口的 `127.0.0.1`/`localhost` Host，并对所有写操作继续要求 CSRF token。

不建议提供接收任意路径的 `/api/nx/run`。需要从 UI 启动 NX Harness 任务时，请提交预注册的 task/journal ID，
由后端映射到固定白名单目录和参数 schema。

## 诊断原则

- `NX_INSTALLATION_NOT_FOUND`：未配置且自动发现不到 NX；
- `NX_CONFIGURED_INSTALLATION_INVALID`：用户配置的目录不是可用 NX 安装，不回退其他版本；
- `NX_JOURNAL_RUNNER_MISSING`：NX 存在但未安装/找不到 Programming Tools；
- `NX_RUNTIME_PROBE_TIMEOUT`：启动、定制项或许可证检查超时，进程已终止；
- `NX_PYTHON_API_UNAVAILABLE`：journal 已返回，但 NXOpen 导入或 Session 初始化失败；
- `NX_PYTHON_API_VERIFIED`：固定探针在子进程中取得了 NXOpen Session。

不要仅根据 `NXOpen.pyd` 或 Python DLL 存在就显示“可用”。这些文件只支持“已检测到运行时证据”的提示。
