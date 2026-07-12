# SGGK Harness 全新 Windows 电脑部署与使用指南

本文面向第一次接触本仓库的测试人员和开发人员，目标是在一台全新的 Windows x64 电脑上完成以下工作：

1. 从 GitHub 或离线 Git bundle 获得仓库；
2. 安装或离线部署构建工具和 Python 依赖；
3. 接入本机 SGGK SDK、许可证和运行时 DLL；
4. 接入内网 Qwen OpenAI-compatible Message API；
5. 只输入一个 SGGK public function，通过中文多轮审查后批准并自动执行真实 SDK 测试；
6. 找到生成的测试代码、测试用例、执行记录、中文审查报告和故障证据；
7. 在无公网环境中接入 ABC 数据，并从小样本逐步扩大到分片 campaign；
8. 使用本机源码快照增强测试，同时确保源码任务只发送到内网模型。

本文中的盘符和目录只是示例。请根据本机磁盘、SDK 版本和组织规范替换，不要求使用任何特定绝对路径。

---

## 1. 先理解四条边界

### 1.1 模型只生成候选，不直接执行 SDK

正式生成链路是：

```text
SGGK public function
  -> Harness 自动解析接口、源码证据和内部测试任务
  -> Qwen Message API 生成 1～8 个独立候选
  -> 固定代码做 JSON contract、schema、DSL/recipe 等批准前门禁
  -> 生成第 1 轮中文审查文档
  -> 用户 comment，Qwen 解释并分类为修改、问题、拒绝或批准
  -> 涉及修改时生成不可变新候选/新轮次并再次审查
  -> 用户提交明确同意执行的自然语言 comment
  -> 真实 SGGK runner 或隔离插件构建和执行
  -> 复现、triage、replay、TopoTrack、final_report.zh-CN.md
```

模型不能提供命令、runner 路径、工作目录、环境变量、shell、SDK 链接参数或任意本地路径。所有执行权限都由固定宿主代码掌握。

### 1.2 `harness.ps1` 是普通用户唯一入口

普通用户只使用仓库根目录的：

```text
harness.ps1
```

它调用底层 `test_harness/tools/sggk_harness.py`，统一管理 session、任务 ID、候选、轮次、哈希、审批绑定、runner 和恢复状态。用户不填写测试表单，不指定 manifest、run-id、JSON、hash 或 runner 路径。

不要把聊天窗口中复制的 JSON、人工改写的模型 JSON 或低层 transport 调试结果直接交给 compiler/runner，当作正式模型产物。底层 raw Message pipeline 只由 session orchestrator 调用；维护者诊断入口统一列在高级内部附录。

### 1.3 不保留并行的表单或 saved-output 执行入口

环境构建、smoke、批量数据和源码上下文都由固定宿主能力接入统一
session。不要使用独立 wrapper 执行保存的模型 JSON，也不要在 review
session 之外复制、批准或运行候选。

新机完成环境构建后，普通使用顺序固定为：

1. 构建 runner；
2. `harness.ps1 start <public-function>`；
3. 通过 `comment` 迭代；满意后提交 `.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"`；
4. Harness 自动执行并输出最终中文报告。

### 1.4 `artifacts/` 是本地证据，不会自动进入 Git

`build/`、`artifacts/`、SDK、DLL、LIB、许可证和日志都被 `.gitignore` 排除。测试结束后如需交付或长期保存，应复制整个 run 目录，并保留其中的 SHA-256、provenance、review packet 和报告；不要只复制一张截图或单个失败 recipe。

---

## 2. 推荐目录布局

推荐把仓库、SDK、源码和离线介质分开：

```text
<工作根目录>/
  SGGK_Test_SDK/                 # Git 仓库
    .venv/                       # Python 虚拟环境
    build/                       # 本机构建，Git 忽略
    artifacts/                   # 本地证据，Git 忽略
      harness_sessions/          # start/comment/execute 的完整 session
      datasets/abc/              # Harness 可访问的 ABC/SGT 数据
      internal/                  # Harness 自动管理的 Prompt、候选和门禁证据
  vendor/
    <SDK版本>/SGGK/              # SGGK SDK 根目录
      include/
      x64-win/
      sggk.lic
  source/
    <本机源码快照>/               # 可选，只读源码根
  offline/
    installers/                  # Git、Python、CMake、VS 离线介质
    wheelhouse/                  # Python wheels
    certificates/               # 内网 CA PEM
    dataset_archives/            # ABC 压缩包或已解压数据
```

目录规则：

- `SGGK_SDK_DIR` 可以指向仓库外部的 SDK。
- `SGGK_SOURCE_ROOT` 可以指向仓库外部的只读源码快照；其他源码路径参数只供维护者诊断。
- Harness 自动管理的 runner、manifest、正式输出和 session 证据必须位于仓库根目录内。
- 普通 recipe 引用的 STEP、IGES、SGT 应放在仓库的 `artifacts/`、`test_harness/fixtures/` 或受支持的 SDK sample 目录下。
- 外部 ABC 缓存可通过 `materialize_input_assets.py` 复制或 hardlink 到仓库 `artifacts/`。
- 大规模运行会产生深目录和大量文件。建议使用较短的 ASCII 工作路径，并预留足够磁盘空间。

---

## 3. 获取仓库

### 3.1 可以访问 GitHub 时

```powershell
$WorkRoot = "<工作根目录>"
New-Item -ItemType Directory -Force $WorkRoot | Out-Null
Set-Location $WorkRoot

git clone https://github.com/XintZhang1/SGGK_Test_SDK.git
Set-Location .\SGGK_Test_SDK
git switch main
git pull --ff-only
git status --short --branch
git log -1 --oneline
```

预期：

- 当前分支是 `main`；
- `git status` 没有意外修改；
- 能看到最新 commit。

如果仓库需要认证，优先使用组织批准的 Git Credential Manager、SSH key 或短期 token。不要把 token 写入仓库文件。

### 3.2 完全离线时使用 Git bundle

在一台可访问 GitHub 的受控电脑上：

```powershell
$Transfer = "<传输介质目录>"
git clone --mirror https://github.com/XintZhang1/SGGK_Test_SDK.git .\SGGK_Test_SDK.mirror.git
git -C .\SGGK_Test_SDK.mirror.git bundle create "$Transfer\SGGK_Test_SDK.bundle" --all
git bundle verify "$Transfer\SGGK_Test_SDK.bundle"
Get-FileHash "$Transfer\SGGK_Test_SDK.bundle" -Algorithm SHA256
```

把 bundle 及其 SHA-256 通过批准的介质传入内网。在新电脑上：

```powershell
$Bundle = "<传输介质目录>\SGGK_Test_SDK.bundle"
Get-FileHash $Bundle -Algorithm SHA256
git bundle verify $Bundle

Set-Location "<工作根目录>"
git clone $Bundle SGGK_Test_SDK
Set-Location .\SGGK_Test_SDK
git switch main

# 仅为未来恢复网络连接后的 fetch/pull 设置正式远端；当前不会联网。
git remote set-url origin https://github.com/XintZhang1/SGGK_Test_SDK.git
git status --short --branch
```

只传 ZIP 会丢失 branch、commit、tag 和可审计历史；需要可复现版本时应使用 bundle。

---

## 4. Windows 工具与版本

### 4.1 必需工具

| 工具 | 要求 | 用途 |
| --- | --- | --- |
| Windows | x64 | 当前 CMake、SDK lib/bin 布局为 Windows x64 |
| Git for Windows | 组织支持版本 | clone、bundle、版本记录 |
| Python | 3.11+ x64 | Prompt、schema、campaign、报告与 Message client |
| CMake | **4.2+** | `Visual Studio 18 2026` generator 从 4.2 开始可用 |
| Visual Studio / Build Tools | 2026，Desktop development with C++ | MSVC x64、Windows SDK、v145 toolset |
| PowerShell | 7 推荐；5.1 可用 | Windows 命令和生成的 shard 脚本 |
| `tar` | 能读取 `.7z` | 使用 ABC 压缩包时解压 |
| `curl` | 可选 | 仅公网下载 ABC 时使用；完全离线可不使用 |

### 4.2 制作 Visual Studio 离线 layout

在联网电脑下载与许可证相符的 Visual Studio 2026 或 Build Tools bootstrapper。下面以 bootstrapper 文件名为占位符：

```powershell
& ".\<VS-bootstrapper>.exe" `
  --layout "<传输介质目录>\VS2026Layout" `
  --lang zh-CN en-US `
  --add Microsoft.VisualStudio.Workload.NativeDesktop `
  --includeRecommended
```

把整个 `VS2026Layout` 目录传入内网。目标机从 layout 中运行安装程序，选择/确认：

- Desktop development with C++；
- MSVC x64/x86 build tools；
- Windows SDK；
- v145 toolset。

安装后验证：

```powershell
$VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
& $VsWhere -latest -products * -requires Microsoft.VisualStudio.Workload.NativeDesktop -property displayName
& $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath

cmake --help | Select-String "Visual Studio 18 2026"
```

如果最后一条找不到 generator，通常是 CMake 版本低于 4.2，而不是 C++ 项目本身出错。建议单独安装 CMake 4.2+，不要依赖未知版本的 IDE 内置 CMake。

### 4.3 Python wheelhouse：联网电脑准备

仓库根目录已有 `requirements-offline.txt`。在与目标机相同 Windows x64、相同 Python 大/小版本的联网电脑上运行：

```powershell
Set-Location .\SGGK_Test_SDK
$Wheelhouse = "<传输介质目录>\wheelhouse"
New-Item -ItemType Directory -Force $Wheelhouse | Out-Null

py -3.11 -m pip download `
  --only-binary=:all: `
  --dest $Wheelhouse `
  -r .\requirements-offline.txt

Get-ChildItem $Wheelhouse -File |
  Get-FileHash -Algorithm SHA256 |
  Export-Csv "<传输介质目录>\wheelhouse.sha256.csv" -NoTypeInformation -Encoding UTF8
```

`pip download` 会一并下载间接依赖，例如 `attrs`、`jsonschema-specifications`、`referencing`、`rpds-py`、`typing-extensions`、`colorama`、`iniconfig`、`packaging`、`pluggy` 和 `pygments`。不要只复制四个顶层 wheel。

`Pillow`、`rpds-py` 等 wheel 与 Python ABI/系统架构相关。最稳妥的方法是下载电脑与目标电脑都使用 Python 3.11 x64，或两边都使用同一个已验证版本。

### 4.4 目标机离线安装 Python 依赖

```powershell
Set-Location "<工作根目录>\SGGK_Test_SDK"

py -3.11 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install `
  --no-index `
  --find-links "<传输介质目录>\wheelhouse" `
  -r .\requirements-offline.txt

python --version
python -m pip check
python -c "import jsonschema, PIL; print('Python dependencies OK')"
```

不要在离线安装命令中省略 `--no-index`；否则 pip 可能等待不可达的公网源。

---

## 5. PowerShell 和中文 UTF-8

仓库、中文报告和 JSON 均使用 UTF-8。PowerShell 7 通常无需额外处理。Windows PowerShell 5.1 建议在当前会话执行：

```powershell
chcp 65001 > $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
```

PowerShell 5.1 查看中文文件时显式指定编码：

```powershell
Get-Content -Encoding UTF8 .\docs\SGGK_HARNESS_全新电脑部署与使用指南.md
```

建议：

- 工作路径优先使用短 ASCII 目录，避免原生工具对长路径或特殊字符处理不一致；
- 如果组织允许，可启用 Git 长路径：`git config --global core.longpaths true`；
- 不要通过 PowerShell 的默认 ANSI 编码重写 JSON；Python 工具会按 UTF-8/UTF-8 with BOM 读取。

---

## 6. 接入 SGGK SDK

### 6.1 SDK 根目录必须具备的内容

`SGGK_SDK_DIR` 必须指向包含以下内容的根目录：

```text
<SGGK_SDK_DIR>/
  include/Foundation/init.h
  sggk.lic
  x64-win/
    lib/       # Release import libraries
    bin/       # Release runtime DLL
    libd/      # Debug import libraries
    bind/      # Debug runtime DLL
```

如果只运行 Release，可以不在手册流程中使用 Debug，但离线 SDK 包最好保持完整，避免之后切换配置时再次传输。

### 6.2 当前会话设置

```powershell
$env:SGGK_SDK_DIR = "<SDK根目录>"

Test-Path "$env:SGGK_SDK_DIR\include\Foundation\init.h"
Test-Path "$env:SGGK_SDK_DIR\x64-win\lib"
Test-Path "$env:SGGK_SDK_DIR\x64-win\bin"
Test-Path "$env:SGGK_SDK_DIR\sggk.lic"
```

四项都应为 `True`。如果 SDK 还包含可供内网模型分析的源代码，可另设：

```powershell
$env:SGGK_SOURCE_ROOT = "<只读源码根目录>"
Test-Path $env:SGGK_SOURCE_ROOT
```

不要把 API key、SDK 或源码路径提交到 Git。对于非敏感 SDK 路径，如需持久化，可使用用户环境变量；API key 建议只放当前会话或组织密钥系统。

### 6.3 配置和构建

```powershell
Set-Location "<工作根目录>\SGGK_Test_SDK"
.\.venv\Scripts\Activate.ps1

Push-Location .\test_harness
cmake --list-presets
cmake --fresh --preset windows-local
cmake --build --preset windows-release
Pop-Location
```

使用 `--fresh` 可以避免旧 `CMakeCache.txt` 继续引用另一套 SDK。成功后应存在：

```powershell
$Runner = ".\build\test_harness\Release\sggk_case_runner.exe"
$Extractor = ".\build\test_harness\Release\sggk_topology_extract.exe"

Test-Path $Runner
Test-Path $Extractor
Test-Path ".\build\test_harness\Release\sggk.lic"
Get-ChildItem ".\build\test_harness\Release" -Filter "SGGK_*.dll" | Select-Object -First 5 Name
```

CMake 会把 Release DLL 和 `sggk.lic` 复制到 exe 旁。不要手工从其他 SDK 版本混入 DLL。

### 6.4 不经过模型的基础 SDK smoke

```powershell
& $Runner `
  --recipe .\test_harness\recipes\boolean_smoke.json `
  --out .\artifacts\bootstrap_sdk_smoke

$LastExitCode
Get-Content -Encoding UTF8 .\artifacts\bootstrap_sdk_smoke\boolean_smoke\report\status.json
Get-Content -Encoding UTF8 .\artifacts\bootstrap_sdk_smoke\boolean_smoke\report\validation.json
```

通过标准不是只有进程返回 0，还应确认：

- SDK status 成功；
- TopoCheck 成功；
- `validation.json` 的实际 Oracle 成功；
- 产物目录中有输入、输出和报告。

---

## 7. 接入内网 Qwen Message API

### 7.1 环境变量

```powershell
$env:SGGK_QWEN_BASE_URL = "https://<内网主机>/v1"
$env:SGGK_QWEN_MODEL = "<服务端实际模型ID>"
$env:SGGK_QWEN_API_KEY = "<可选Bearer-token>"
$env:SGGK_QWEN_CA_BUNDLE = "<可选CA-PEM文件>"
```

说明：

- `BASE_URL` 可以是 `https://host/v1`；客户端会追加 `/chat/completions`。
- 如果配置值已经以 `/chat/completions` 结尾，客户端不会重复追加。
- 内网 profile 的 token 可为空；如果服务需要 token，当前客户端使用 `Authorization: Bearer ...`。
- 内网自签或私有 CA 使用 PEM 文件，并通过 `SGGK_QWEN_CA_BUNDLE` 指定。
- URL 不得包含用户名、密码、query 或 fragment。
- 不要把 key 写入 Prompt、README、命令脚本或 artifact 名称。

SiliconFlow 只是在开发或联调时可替换的 Message API endpoint。它与内网服务使用相同的请求、响应、严格 JSON、鉴权脱敏和固定门禁 contract；切换时只更换上述连接配置，不选择另一套用户流程，不走 provider 专用代码，也不会在内网失败时自动回退到 SiliconFlow。

### 7.2 配置校验，不显示密钥

```powershell
python -c "from test_harness.authoring_gateway import load_gateway_config; print(load_gateway_config('intranet').public_metadata())"
```

输出会包含 profile、model、endpoint 的 SHA-256 和 `api_key_present`，不会打印 key 或原始 endpoint。

可选网络检查：

```powershell
$ApiUri = [Uri]$env:SGGK_QWEN_BASE_URL
Test-NetConnection $ApiUri.Host -Port $(if ($ApiUri.Port -gt 0) { $ApiUri.Port } else { 443 })
```

### 7.3 服务端必须返回严格 JSON content

客户端调用 OpenAI-compatible chat completions。响应至少要满足：

```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "{\"kind\":\"flat_recipe\",\"recipe\":{\"api\":\"check_sgt\",\"case_id\":\"message_shape_example\",\"source_file\":\"artifacts/example.sgt\",\"expectations\":{}},\"notes\":[\"仅用于说明响应封装\"]}"
      }
    }
  ]
}
```

上例只说明 HTTP 响应外壳和字符串转义；其中 `artifacts/example.sgt` 是不可直接执行的示意路径。真实 `content` 还必须符合当前 task 的 output contract、schema 和本机资产门禁。

注意：

- `message.content` 必须是字符串；
- 去掉字符串外壳后必须正好是一个 JSON object；
- 不能带 Markdown code fence、解释文字、第二个 JSON、重复 key、`NaN` 或 `Infinity`；
- `finish_reason=length` 或拒答会被判失败；
- reasoning 内容不是候选，不会作为正式产物保存；
- 默认先尝试 `json_schema`/`json_object`，服务明确拒绝结构化输出时会有界降级。

如果服务不接受 `response_format` 或要求显式关闭思考字段，`sggk_harness.py` 会按受控配置处理兼容性；这不会放宽 `message.content` 必须为严格 JSON 的门禁。底层 transport 参数只属于高级内部诊断，不进入普通用户命令。

---

## 8. 普通用户：三条命令完成一次测试

以下命令均在仓库根目录执行。完成第 3～7 节的环境和 SDK 配置后，普通用户不再创建表单、Prompt、manifest、run-id 或 runner 命令。

### 8.1 `start`：只输入 public function

```powershell
.\harness.ps1 start api_boolean
```

也可以输入命名空间限定名或完整 public signature。Harness 会自动：

- 在 SDK public headers、能力注册和可用源码证据中解析接口；
- 区分重载，推导输入构造、关键风险、Oracle、正例和负例；
- 创建内部 session、任务、候选、轮次、表单和 manifest；
- 通过 Qwen Message API 生成候选并运行批准前固定门禁；
- 写出第 1 轮中文审查文档，并把该任务设为当前活动 session。

正常输出示例：

```text
已创建 SGGK 测试审查任务。

目标接口：api_boolean
当前状态：等待第 1 轮审查

系统已自动生成任务 ID、候选 ID、轮次和完整性哈希，无需手工填写。
第 1 轮审查文档：<artifact-path>

下一步：
  提交意见：.\harness.ps1 comment "你的意见"
  批准执行：.\harness.ps1 comment "这一版可以开始执行真实测试。"
```

如果函数名对应多个重载，Harness 会在第 1 轮报告中列出候选签名和当前推断。用户仍然只需提交自然语言 comment，例如“选择第二个重载”，不需要填写 JSON。

### 8.2 `comment`：只提交自然语言意见

```powershell
.\harness.ps1 comment "增加退化输入、近容差相交和空结果检查"
```

系统会原样保存 comment 及其哈希绑定，并通过同一 Message API contract 让 Qwen 输出中文解释。每条 comment 都形成不可变事件，但不一定创建候选轮：

- `revise`：意见涉及代码、用例、参数、Oracle 或范围修改，生成完整替换候选和不可变新轮次；
- `question`：在当前轮保存 Qwen 中文回答，不修改候选；
- `reject`：终止当前任务，不执行 SDK；
- 明确批准 comment：绑定并执行当前最新轮次，不生成新候选轮。用户仍只提交自然语言，不使用独立审批命令。

凡生成新轮次，其中文报告至少包含：

- 用户原始意见；
- Qwen 对意见的理解；
- 采纳项；
- 未采纳项及原因；
- 相对上一轮的代码、用例、参数、Oracle 和风险覆盖变化；
- 当前仍未解决的假设或限制。

用户可以连续提交多条 comment。系统自动定位当前活动 session，用户不提供 session ID、round 或 hash。Qwen 对问题的回答和拒绝/批准解释也必须作为当前轮不可变证据保存。

### 8.3 明确批准 `comment`：批准最新轮次并自动执行

```powershell
.\harness.ps1 comment "这一版可以开始执行真实测试。"
```

Harness 只允许批准当前最新且完整的轮次。Qwen 返回的内部 `decision=approve` 不是 CLI 命令，也不生成新候选轮；固定宿主还会独立确认原始 comment 包含明确执行同意，随后验证轮次链、候选、源码证据和受审产物的内部哈希，写入不可变批准事件，然后自动启动真实 SGGK SDK 构建、用例执行、Oracle、triage、replay 和必要的故障定位。

完成后必须输出：

```text
<session-root>/execution/round_NNNN/attempt_NNNN/final_report.zh-CN.md
```

如果执行失败，批准记录仍保留，但状态必须是 `execution_failed`，不能显示为测试通过。最终报告应写明失败阶段、复现入口和证据路径。

### 8.4 辅助命令

```powershell
.\harness.ps1 status   # 查看当前 session、最新轮次和执行状态
.\harness.ps1 show     # 显示当前最新中文审查文档/最终报告路径
.\harness.ps1 retry    # 重试当前已批准轮次的失败或中断执行
```

`retry` 不重新解释 comment，也不创建新轮次。测试方案需要变化时使用 `comment`。要切换到另一个 public function，先完成当前任务，或提交“拒绝并结束当前任务”使其进入终止状态，再重新使用 `start`。

---

## 9. Session、轮次和批准语义

### 9.1 状态机

```text
generating -> awaiting_comment
awaiting_comment -> interpreting_comment
interpreting_comment -> awaiting_comment             # question
interpreting_comment -> rejected                     # reject
interpreting_comment -> generating -> awaiting_comment # revise，新轮次
interpreting_comment -> executing                    # 明确批准 comment；内部 decision=approve
executing -> completed | execution_failed
generating -> generation_failed
```

用户只看到中文状态和报告路径。内部 ID、round index、父轮次哈希、comment 哈希、候选哈希、审批哈希和执行哈希全部由 `test_harness/tools/sggk_harness.py` 管理。

### 9.2 不可变轮次

- `start` 创建第 1 轮。
- 每条 `comment` 都在当前轮保存原文、Qwen 中文解释和不可变事件。
- 只有 `revise` 才从当前最新轮次派生下一轮，并保留上一轮全部证据。
- 内部 `decision=question` 不修改候选，`decision=reject` 终止，`decision=approve` 绑定当前轮并执行；这些枚举不是 CLI 命令，也不创建候选轮。
- Qwen 不能静默修改方案；凡涉及修改，必须在中文解释中逐项说明并生成新轮次。
- 只有最新轮次可以批准；旧轮次永远不能被重新标记为当前批准版本。
- 已完成任务后再提交涉及修改的 comment，会使旧批准成为历史记录，并为新轮次重新进入待审查状态。
- 模型无权写入批准状态；固定宿主只有在内部结果为 `decision=approve` 且原始 comment 明确同意执行时才会产生批准。

### 9.3 批准与真实执行边界

批准前允许进行 contract、schema、DSL、静态分析、受控物化和其他不改变 SDK 真实状态的固定门禁。真实 SGGK SDK case、批量 runner、回归复现和故障调查只能从当前最新轮次的有效批准事件启动。

执行启动前必须再次验证批准绑定的轮次仍是最新轮次。任何受审文件、源码 contract、生成代码或用例发生变化，都必须形成新轮次，旧批准不能自动沿用。

### 9.4 SiliconFlow 与内网的一致性

Session 不记录或暴露 provider 专用用户流程。内网 Qwen 和 SiliconFlow 测试 endpoint 都必须实现相同 Message API contract；SiliconFlow 只用于模拟或联调同模型环境。更换 endpoint 不改变 `start / comment` 命令、轮次语义、固定门禁或最终报告格式。

---

## 10. 生成代码、测试用例、记录和报告在哪里

### 10.1 总路径图

```text
artifacts/harness_sessions/
  active.json                              # 当前活动 session 指针；用户不编辑
  <session-id>/
    session.json                           # 内部 session 状态；用户不编辑
    events/000001.json                     # start/comment/approval/execute 事件链
    resolution/resolution.json             # public function 解析结果

    rounds/0001/
      internal/api_test_form.json          # Harness 推导的内部请求
      prompt/authoring_prompt.md            # 固定 Prompt
      prompt/model_task_manifest.json       # 内部任务清单
      candidate/candidate.json              # Message API 候选
      candidate/candidate.provenance.json   # 候选来源和门禁证据
      pipeline/                             # 批准前生成和固定门禁
      review/review_subject_digest.json     # 脱敏审查上下文
      review/第1轮测试方案审查.zh-CN.md      # 用户主要阅读的第 1 轮报告
      round_manifest.json                   # 不可变轮次哈希链
      comments/<comment-hash>/
        user_comment.txt                    # 用户意见原文
        interpretation.json                 # Qwen 对意见的中文理解

    rounds/0002/                            # revise comment 生成的新候选轮
      internal/api_test_form.json
      prompt/model_task_manifest.json
      candidate/candidate.json
      pipeline/
      review/第2轮测试方案审查.zh-CN.md      # 用户主要阅读的第 2 轮报告
      round_manifest.json
      comments/<approval-comment-hash>/     # 明确批准 comment 的解释事件；不再建候选轮
        user_comment.txt
        interpretation.json

    approval/
      round_0002_<comment-tag>.json                       # Harness 写入的内部批准事件
      round_0002_<comment-tag>.execution_manifest.json    # 只供批准后执行的绑定副本

    execution/round_0002/attempt_0001/
      execution_result.json                 # 本次批准后执行汇总
      final_report.zh-CN.md                 # 本次执行的最终中文报告
      pipeline/                             # cases/triage/replay/定位证据
```

普通用户通常只阅读当前轮次的 `第N轮测试方案审查.zh-CN.md` 和完成后的 `final_report.zh-CN.md`。其余文件用于恢复、审计和缺陷复现，由 Harness 自动创建和验证。

### 10.2 单个 SDK case

```text
cases/<case-id>/
  manifest.json
  run_state.json
  input/
    recipe.json
    target.sgt
    tool.sgt
  output/
    result_*.sgt
    error_entity_*.sgt
    topo_check_error_*.sgt
  report/
    status.json
    topo_check.json
    topo_track.json
    topo_track_summary.json
    input_provenance.json
    input_topology_index.json
    input_properties.json
    properties.json
    validation.json
    preview.png
  debug_geometry/*.sgt
```

具体文件随 API 和执行结果变化：import/check 类 case 不一定有 target/tool，失败 case 不一定有 result，`preview.png`、debug geometry、replay、reduction 和调查目录也只在相应步骤启用或有证据时出现。以 `task_summary.json` 和各级 manifest 实际列出的路径为准。

### 10.3 中文审查报告怎么用

运行 `harness.ps1 show` 获取当前最新报告路径。报告应首先用中文回答：

- Harness 解析到了哪个 public function 和重载；
- Qwen 计划生成什么代码和测试用例；
- 输入几何、参数、容差、调用顺序和 Oracle 如何覆盖风险；
- 相对上一轮新增、删除或修改了什么；
- 若该轮由修改 comment 派生，包含 comment 原文、Qwen 理解、采纳项和未采纳原因；
- 哪些假设、能力缺口或风险仍需用户判断。

内部 `decision=question|reject|approve` 不创建候选轮；对应的 Qwen 中文回答/解释保存在当前轮的 comment 事件目录中，这些 decision 值不是 CLI 命令。轮次报告可以提供内部证据和哈希的折叠式索引，但不要求用户复制、绑定或编辑它们。需要调整时运行修改 `comment`；满意时运行 `.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"`。批准记录由 Harness 自动创建，任何受审内容变化都会形成新轮次并使旧批准不再适用于当前方案。

批量 recipe 的索引和汇总报告也由同一 session 管理。若只想批准子集，应通过 comment 要求缩小或拆分范围，等待新轮次报告，而不是手工编辑索引。

### 10.4 最小审查顺序

1. 运行 `harness.ps1 show`，阅读当前最新轮次中文报告。
2. 确认目标函数/重载、风险、测试用例和 Oracle 符合意图。
3. 有任何疑问就运行 `comment`：问题会留在当前轮回答；修改意见会生成下一轮并展示差异。
4. 满意后运行 `.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"`；不要手工启动 runner。
5. 执行结束后阅读 `final_report.zh-CN.md`；失败时再深入 triage、replay、TopoTrack 和 failure bundle。

高级代码审查者可以继续检查 round manifest、normalized candidate、生成 adapter/schema/recipe 和 provenance，但这些不是普通用户完成一次审查所必需的操作。

---

## 11. 使用本机源码增强测试

这条链路适用于有权访问的本机 SGGK C/C++ 源码。普通用户仍然只输入 public function；源码扫描、任务封装和 Message API 请求由 Harness 在同一个 session 内完成。

### 11.1 配置只读源码根

```powershell
$env:SGGK_SOURCE_ROOT = "<只读源码根目录>"
```

随后仍使用相同入口：

```powershell
.\harness.ps1 start api_boolean
```

Harness 会围绕目标接口寻找容差比较、状态分支、空值、拓扑修改、相交/布尔/offset、退化和距离关系等风险，并只把受控、有界的源码片段发送到允许接收源码的内网 endpoint。SiliconFlow 或其他外部测试 endpoint 不得接收源码 excerpt；这不是自动 fallback 场景。

scanner finding 是启发式候选，不是漏洞结论。相关风险、来源引用、假设和测试增强必须进入第 1 轮中文报告，经过修改 comment 和明确批准 comment 后才能执行真实 SDK 测试。

### 11.2 源码增强轮次应展示什么

一个可审查的源码增强输出应至少说明：

- `source_contract_sha256`：宿主签发的任务、finding、来源范围和当前源码内容契约；
- `source_refs`：只使用宿主签发的 opaque ID、行范围和内容哈希，不接受模型自报路径；
- `source_review.summary`：源码风险机制摘要；
- `risky_branches`：每个关键条件分支引用来源 ID；
- `failure_hypotheses`：至少两个可证伪假设，每项引用分支 ID；
- `test_enhancements`：引用假设 ID 和真实 `case_id`，说明几何、参数、容差和 Oracle；
- 来源→分支→假设→增强→case 引用图完整，当前源码变化后旧证据自动失效；
- 批准前固定门禁、当前中文审查轮次，以及批准后的真实 SDK 执行结果。

所有源码 Prompt、任务、轮次、正式输出和调查报告都属于受保护 artifact，应按源码保密级别存储和传输。底层 scanner、source task builder 和 raw Message pipeline 命令只放在本文高级内部附录中。

---

## 12. ABC 数据：联网准备和完全离线接入

本章是数据管理员为 Harness 准备、审计和扩容 ABC/SGT 数据的流程，不改变普通用户的三条命令。普通用户批准 session 后，`sggk_harness.py` 会使用已配置的 runner 和已登记数据；本章出现的 runner、jobs、shard 和目录参数只用于数据集验收与大规模基础设施运维。

### 12.1 ABC 数据在本仓库中的角色

ABC 不是只用于 import smoke。推荐覆盖：

- 复杂 STEP 导入和 TopoCheck；
- 导入后 SGT 的 `loaded_sgt` recut；
- imported body 与 cylinder/sphere/cone/torus/generated tool 的布尔；
- sweep、support-sweep、extrude、revolve、thicken、pre-boolean 生成体；
- 精确接触、`±1e-5`、`±1e-2` 和大坐标 sibling；
- STEP/IGES roundtrip 属性漂移；
- 分片、resume、triage、preview、geometry audit 和稳定复现。

### 12.2 离线介质应携带什么

优先携带已经解压并生成 index 的目录：

```text
abc_cache/
  dataset_index.json
  complex_dataset_index.json
  cad_feature_profile.json
  files/**/*.step
  full_complex_import/**/output/result_*.sgt   # 如果已经做过导入
```

如果只能携带官方压缩包，还必须携带：

```text
abc_fetch_offline/
  manifests/
    step_v00.txt
    meta_v00.txt
    size.yml
    md5.yml
  downloads/
    abc_####_step_v00.7z
    abc_####_meta_v00.7z
```

只有 archive 而没有 `manifests/` 时，fetch helper 会尝试联网补 manifest。完全离线环境必须两者都带。

当前 fetch helper 下载 STEP 和 meta，不提供 IGES。IGES/IGS 测试需要另行传入经过授权的本地数据。

### 12.3 使用已缓存压缩包，禁止下载

先把目录复制到仓库内，例如：

```text
artifacts/datasets/abc_fetch_offline/
```

然后：

```powershell
$Chunk = 27  # 替换为离线介质中实际存在的 chunk

python .\test_harness\tools\fetch_abc_dataset.py `
  --out .\artifacts\datasets\abc_fetch_offline `
  --download-root .\artifacts\datasets\abc_fetch_offline\downloads `
  --chunk $Chunk `
  --skip-download `
  --extract-mode sample `
  --sample-count 50 `
  --run-discovery `
  --run-feature-profile `
  --fail-on-command
```

工具会检查 archive 大小和 MD5，并使用 `tar` 解压。不要为了“先跑起来”使用 `--no-verify`，除非这是明确标记的临时探索且不生成正式结论。

### 12.4 从外部已解压缓存 materialize 到 repo

```powershell
python .\test_harness\tools\materialize_input_assets.py `
  --source-abc-root "<外部ABC缓存根>" `
  --target-abc-root .\artifacts\datasets\abc_fetch_smoke `
  --target-step-root .\artifacts\datasets\abc_fetch_smoke\files `
  --target-sgt-root .\artifacts\datasets\abc_imported_sgt `
  --mode copy `
  --overwrite
```

同一 NTFS 卷可使用 `--mode hardlink` 节省空间；跨卷必须使用 `copy`。先用 `--dry-run` 可以查看计划而不写文件。

### 12.5 ABC 小样本验收

```powershell
python .\test_harness\tools\run_abc_sample_smoke.py `
  --fetch-root .\artifacts\datasets\abc_fetch_smoke `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\abc_sample_smoke `
  --top-import-limit 12 `
  --recut-source-limit 4 `
  --recut-limit 12 `
  --timeout 180 `
  --jobs 1
```

查看：

```text
artifacts/abc_sample_smoke/abc_sample_smoke_summary.json
artifacts/abc_sample_smoke/abc_sample_smoke_report.md
artifacts/abc_sample_smoke/previews/
artifacts/abc_sample_smoke/geometry_audit/
artifacts/abc_sample_smoke/triage/
```

### 12.6 分阶段大规模 campaign

不要从新机第一天直接启动 100k。建议门槛：

1. 一个 Qwen 任务、一个候选；
2. 三候选并行选择；
3. 完整 API smoke；
4. ABC 复杂样本 12～100 cases；
5. 单 shard 1,000 cases；
6. 多 shard + resume + merge；
7. 通过 artifact verifier 后再扩大到 100k+。

生成冻结计划：

```powershell
python .\test_harness\tools\plan_large_campaign.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\abc_plan_smoke `
  --profile smoke `
  --dataset-list .\artifacts\datasets\abc_fetch_smoke\complex_dataset_index.json `
  --shards 2 `
  --jobs 1 `
  --timeout 180 `
  --hash-recipes `
  --dataset-audit-require-hashes `
  --profile-cad-features `
  --cad-feature-min-score 8 `
  --corpus-recut-require-exact-bbox-probe `
  --corpus-preserve-input-order `
  --bundle-zip

powershell -ExecutionPolicy Bypass `
  -File .\artifacts\abc_plan_smoke\commands\run_all_with_preflight.ps1
```

合并后优先查看：

```text
artifacts/abc_plan_smoke/merged/campaign_shards_report.md
artifacts/abc_plan_smoke/merged/campaign_verification/campaign_verification.md
artifacts/abc_plan_smoke/merged/oracle_coverage/
artifacts/abc_plan_smoke/merged/previews/
artifacts/abc_plan_smoke/merged/geometry_audit/
artifacts/abc_plan_smoke/merged/bug_registry/
artifacts/abc_plan_smoke/merged/debug_handoff/
artifacts/abc_plan_smoke/merged/bug_record_drafts/drafts.json
```

只有 `campaign_verification` 通过、无 harness/infrastructure error、dataset audit 正常、磁盘充足时才扩大规模。SDK 行为失败可以保留，但必须完成资格判定和稳定复现；不稳定失败留在 inconclusive 证据中，不能直接当作确认 bug。

---

## 13. 新机逐步验收清单

本章用于新机部署管理员验收 Python、SDK、runner、数据和大规模执行基础设施。完成这些一次性 gate 后，普通用户仍只使用 `start / comment`，不需要传 runner 或底层参数。

### Gate 0：版本和磁盘

```powershell
git --version
python --version
cmake --version
cmake --help | Select-String "Visual Studio 18 2026"
Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free
```

通过条件：Python 3.11+、CMake 4.2+、VS18 generator 可见、目标盘空间满足 SDK + 数据 + artifacts。

### Gate 1：Python

```powershell
python -m pip check
python -m pytest -q
python -m ruff check .
python -m compileall -q test_harness
```

通过条件：依赖无破损，测试、lint、compileall 全部成功。

### Gate 2：静态 harness 元数据

```powershell
python .\test_harness\tools\validate_interface_capabilities.py
python .\test_harness\tools\validate_interface_example_packs.py
python .\test_harness\tools\validate_diagnostic_catalog.py --strict-exact
```

通过条件：没有 blocker；宿主生成的内部 IR 经过 JSON Schema；capability、example pack 和诊断目录一致。

### Gate 3：SDK 构建和运行

```powershell
Push-Location .\test_harness
cmake --fresh --preset windows-local
cmake --build --preset windows-release
Pop-Location

& .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe .\test_harness\recipes\boolean_smoke.json `
  --out .\artifacts\acceptance\sdk_smoke
```

通过条件：runner、DLL、license 完整，status/TopoCheck/validation 成功。

### Gate 4：编译进 runner 的 adapter

```powershell
python .\test_harness\tools\validate_plugin_runtime.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --out .\artifacts\acceptance\plugin_runtime

python .\test_harness\tools\run_recipes.py `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --recipe-list .\test_harness\suites\api_smoke_suite.txt `
  --out .\artifacts\acceptance\api_smoke `
  --jobs 1 `
  --timeout 180 `
  --hash-recipes
```

通过条件：plugin manifest 与 runtime registry 一致，API smoke 无基础设施失败，并生成批量中文 recipe 审查报告。

### Gate 5：Qwen 一任务

按第 8 节完成一个最小闭环：

```powershell
.\harness.ps1 start api_boolean
.\harness.ps1 comment "请新增大坐标和 topo_tol 两侧扰动，并解释每个 Oracle 的对应关系"
.\harness.ps1 comment "这一版可以开始执行真实测试。"
```

通过条件：

- `start` 只接收 public function，并生成第 1 轮中文审查文档；
- `comment` 原文被保存为不可变事件，Qwen 给出中文理解，并因明确修改要求生成不可变第 2 轮；
- 最后一条明确批准 comment 只绑定第 2 轮，随后自动启动真实 SDK 执行；
- 固定门禁和 SDK execution 通过，或真实行为失败得到明确分类；
- `final_report.zh-CN.md` 存在，能关联批准轮次、执行结果和证据目录；
- 用户全程没有填写表单、ID、hash、round、JSON、manifest 或 runner。

### Gate 6：源码增强

仅在有源码权限时执行第 11 节。通过条件：source ref 可复核，source review 有摘要/分支/假设/增强，源码 excerpt 只发送到获准的内网 endpoint，并沿用同一自然语言 comment 流程（包括明确批准 comment）。

### Gate 7：ABC sample

按第 12.5 节运行。通过条件：dataset audit 正常，复杂样本被选中，import/recut 有真实 Oracle，preview/geometry audit/triage 齐全。

### Gate 8：大规模 fan-out

只有 Gate 0～7 全部通过，且预估磁盘、timeout、jobs、resume 和 shard merge 都验证后才开始。

---

## 14. 常见故障排查

### 14.1 `Could not create named generator Visual Studio 18 2026`

原因：CMake 低于 4.2，或 VS 2026 未安装。

处理：

```powershell
cmake --version
cmake --help | Select-String "Visual Studio 18 2026"
```

安装 CMake 4.2+，再确认 Native Desktop workload 和 MSVC x64 tools。

### 14.2 `SGGK_SDK_DIR does not point to a valid SGGK SDK`

原因：变量指向 SDK 上层目录、目录拼错或缺少 header。

处理：确保以下文件直接存在：

```powershell
Test-Path "$env:SGGK_SDK_DIR\include\Foundation\init.h"
```

切换路径后使用 `cmake --fresh`。

### 14.3 link 找不到 `SGGK_*`

原因：缺少 `x64-win/lib`、SDK 架构不匹配、Release/Debug 目录混用，或 SDK 包不完整。

处理：检查 `x64-win/lib/*.lib`，使用 x64 Release，禁止混用另一版本 SDK 的 LIB/DLL。

### 14.4 runner 启动时报 DLL 缺失

原因：post-build copy 没完成、杀毒软件隔离、SDK `x64-win/bin` 不完整。

处理：重新 build，检查 exe 同目录 DLL；不要只复制 exe 到别处。

### 14.5 许可证失败

确认：

```powershell
Test-Path "$env:SGGK_SDK_DIR\sggk.lic"
Test-Path ".\build\test_harness\Release\sggk.lic"
```

许可证过期、机器绑定或版本不匹配需要联系 SDK 许可证维护方；不要把许可证提交到 Git 或放进报告包。

### 14.6 `ModuleNotFoundError: jsonschema` 或 `PIL`

原因：未激活 `.venv` 或 wheelhouse 不完整。

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip check
python -m pip install --no-index --find-links "<wheelhouse>" -r .\requirements-offline.txt
```

### 14.7 Qwen 401/403

检查 token 是否为当前会话变量、服务是否使用 Bearer、账号是否有模型权限。不要通过打印整个环境排查；只检查：

```powershell
[bool]$env:SGGK_QWEN_API_KEY
$env:SGGK_QWEN_MODEL
```

### 14.8 TLS/证书错误

把内网 CA chain 保存为 PEM：

```powershell
$env:SGGK_QWEN_CA_BUNDLE = "<CA-PEM>"
Test-Path $env:SGGK_QWEN_CA_BUNDLE
```

不要用关闭证书验证的方式绕过。

### 14.9 Qwen 404

检查 base URL 是否正确：

- 推荐：`https://host/v1`；
- 或完整：`https://host/v1/chat/completions`；
- 不要把 endpoint 配成 `.../chat/completions/chat/completions`；
- 不要附带 query/fragment。

### 14.10 `response_format` 不支持

先确认服务错误明确指出结构化输出字段不支持。普通用户不追加底层 transport 参数；保留当前 session 后联系 Harness 维护者检查受控 Message API 兼容配置。这类兼容处理只移除请求格式提示，不能放宽 `message.content` 必须为严格 JSON 的门禁。

### 14.11 `assistant message.content is not exact JSON`

常见原因：Markdown fence、解释文字、截断、重复 key、多个 JSON、非字符串 content。

处理：

- 联系服务或 Harness 维护者检查服务端与受控客户端的输出 token 上限；
- 确保 system prompt 允许 JSON-only；
- 由维护者在受控 Message API 配置中适配显式思考开关；
- 查看 candidate attempt 的脱敏响应记录和 structured diagnostics；
- 不要人工编辑 candidate 来绕过门禁。

### 14.12 source task 被外部 profile 拒绝

这是安全设计，不是 bug。源码 excerpt 只允许发送到获准的内网 endpoint。要测试 SiliconFlow 等外部兼容 endpoint，请取消 `SGGK_SOURCE_ROOT` 并创建一个不含源码的新 session；不要复制、删改或伪造源码任务来绕过边界。

### 14.13 `must stay inside repository root`

原因：Harness 内部解析到仓库外 runner、dataset、manifest 或输出路径。

处理：

- runner 使用 `build/test_harness/Release`；
- Prompt、输出和 bounded campaign dataset 放在 repo 的 `artifacts/`；
- 外部数据通过 materializer copy/hardlink 到 `artifacts/`；
- 源码根例外：通过专门的 `--bug-source-root`/scanner 参数传入。

### 14.14 输入资产存在，但模型仍不能使用

普通用户先通过 comment 说明希望使用的资产及其测试目的。Harness 会在新轮次中报告资产是否存在、后缀/API 是否匹配，以及为什么接受或拒绝。维护者需要复现内部 intake 时，使用附录 A 中的隔离诊断方式。

### 14.15 当前任务或正式输出已存在

普通用户不要处理内部任务 ID 或覆盖参数：

- 查看当前任务：`.\harness.ps1 status`；
- 继续修改当前方案：`.\harness.ps1 comment "..."`；
- 重试已批准但失败/中断的执行：`.\harness.ps1 retry`；
- 测试另一个 public function：先完成或通过 comment 拒绝当前任务，再运行 `.\harness.ps1 start <public-function>`。

Harness 必须自动生成新 ID、保留旧 session 和证据，不允许普通用户通过覆盖文件解决冲突。

### 14.16 中文显示乱码

使用 PowerShell 7，或 PowerShell 5.1 的 `Get-Content -Encoding UTF8`；不要根据乱码内容重写文件。

### 14.17 ABC `--skip-download` 仍尝试联网

原因：`<out>/manifests` 缺少 `step_v00.txt`、`meta_v00.txt`、`size.yml` 或 `md5.yml`。

处理：把 manifest 与 archive 一起传入，并保持第 12.2 节目录结构。

### 14.18 campaign 很慢或磁盘快速增长

先停止扩大规模，不删除仍在写入的 run。检查：

- shard 数和 `--jobs`；
- 每 case 的 SGT、preview、debug geometry；
- replay/reduction 数量；
- `Get-PSDrive` 剩余空间；
- 是否启用了 `--resume`；
- 小规模 `campaign_verification` 是否已通过。

### 14.19 `comment` 提示没有活动任务

运行 `.\harness.ps1 status`。如果没有活动 session，先运行 `.\harness.ps1 start <public-function>`；不要自己寻找并填写历史 session ID。若已有 session 但活动指针损坏，应保留 `artifacts/harness_sessions/` 并联系维护者恢复，禁止删除历史轮次来“重来”。

### 14.20 明确批准 `comment` 提示当前轮次已变化

这表示批准前检查发现新的 comment、候选、源码 contract 或受审产物。运行 `.\harness.ps1 show` 阅读最新轮次；确认后重新提交明确的批准 comment。不要覆盖 round manifest 或复用旧批准事件。

---

## 15. 测试交付建议

普通交付以整个 review session 为单位，不由用户手工拼装单个 JSON。至少应包含：

```text
Git commit SHA
SGGK SDK 版本标识
Qwen Message API contract + model ID（不含 endpoint/token 明文）
用户输入的 public function
全部不可变审查轮次及用户 comment 原文
批准事件及其绑定的最新轮次
final_report.zh-CN.md
内部 Prompt/正式输出/provenance/哈希链
compiled recipe / plugin code及其 schema、正例、负例
case manifest/input/output/report
失败时的 triage/replay/TopoTrack/failure bundle
campaign 时的 dataset audit、shard merge 和 campaign verification
```

建议将整个 run 根目录压缩并生成 SHA-256：

```powershell
Compress-Archive -Path "<run目录>\*" -DestinationPath "<交付目录>\sggk_harness_run.zip"
Get-FileHash "<交付目录>\sggk_harness_run.zip" -Algorithm SHA256
```

压缩前确认包中不含 API key、许可证或未经批准外发的源码。源码任务 artifact 应在内网按源码保密级别保存。

---

## 16. 日常入口速查

| 目的 | 入口 |
| --- | --- |
| 开始测试一个 public function | `.\harness.ps1 start <public-function>` |
| 提交自然语言审查意见 | `.\harness.ps1 comment "<意见>"` |
| 批准最新轮次并自动执行 | `.\harness.ps1 comment "这一版可以开始执行真实测试。"` |
| 查看当前状态 | `.\harness.ps1 status` |
| 查看最新报告 | `.\harness.ps1 show` |
| 重试已批准轮次的执行 | `.\harness.ps1 retry` |
| 底层 session orchestrator | `test_harness/tools/sggk_harness.py` |
| 架构和信任边界 | `test_harness/HARNESS_ARCHITECTURE.md` |
| 同一 Message API contract 的内网/SiliconFlow endpoint 联调 | `test_harness/MESSAGE_API_ENDPOINTS.md` |
| API 能力矩阵 | `test_harness/INTERFACE_TEST_MATRIX.md` |
| 批量 recipe 执行 | `test_harness/tools/run_recipes.py` |
| ABC sample | `test_harness/tools/run_abc_sample_smoke.py` |
| 大规模计划 | `test_harness/tools/plan_large_campaign.py` |
| campaign 验证 | `test_harness/tools/verify_campaign_artifacts.py` |
| 详细 runner/campaign 参数 | `test_harness/README.md` |

新电脑上请始终按“工具和依赖 → SDK build → 无模型 smoke → `start` → comment 审查轮次 → 明确批准 comment 自动执行 → ABC sample → 分片 campaign”的顺序推进。任何前置 gate 不通过时，先修复环境或证据链，不要直接扩大测试规模。

---

## 附录 A：高级内部 intake 与 raw Message pipeline

本附录只供 Harness 维护者调试固定宿主。普通用户不要按照本附录创建任务，也不要用这些命令绕过 `start / comment`。任何通过 raw pipeline 生成的产物都不构成用户批准；真实 SDK 执行仍必须由 session orchestrator 在最新轮次批准后启动。

### A.1 内部 API form

权威 schema：

```text
test_harness/forms/api_test_form.schema.json
```

`sggk_harness.py` 会根据 public function、SDK header、能力注册、源码风险和可用资产自动推导并保存 form。常见内部字段包括 `request_id`、`target_api`、`test_goal`、`risk_summary`、`geometry`、`tolerance_focus`、`oracles`、`expected_behavior`、`case_count`、`run_profile` 和 `input_assets`。

这些字段是机器协议和故障诊断接口，不是用户问卷。维护者检查内部 form 时应确认：接口名来自真实 public header；风险落实到参数或调用序列；至少存在一个真实结果 Oracle；资产位于允许范围内；ID、路径和 hash 由宿主生成。

### A.2 内部 form、task 和 Prompt pack 复现

仅在复现 intake 或固定门禁故障时运行：

```powershell
python .\test_harness\tools\build_api_test_task.py `
  .\artifacts\internal_debug\forms\request.json `
  --strict `
  --out .\artifacts\internal_debug\tasks\request.json

python .\test_harness\tools\build_model_prompt_pack.py `
  --forms-dir .\artifacts\internal_debug\forms `
  --manifest .\artifacts\internal_debug\forms\00_manifest.json `
  --out .\artifacts\internal_debug\prompt_pack `
  --max-prompt-chars 60000
```

这里的 `request_id`、form manifest、Prompt manifest 和输出目录应从已有 session 证据复制到隔离诊断目录，不应让普通用户重新填写。

### A.3 raw Message pipeline 诊断

以下命令只重现批准前候选生成和固定门禁，故意不传 `--execute`：

```powershell
python .\test_harness\tools\run_message_harness_pipeline.py `
  --profile intranet `
  --candidate-count 1 `
  --candidate-parallelism 1 `
  .\artifacts\internal_debug\prompt_pack\model_task_manifest.json
```

raw pipeline 不管理活动 session、用户 comment、不可变审查轮次或批准事件。不得把它的 exit code、正式输出或 `review_status=awaiting_natural_language_comment` 当成用户批准，也不得直接把诊断产物交给 runner。

### A.4 内部源码任务诊断

源码增强 session 失败时，维护者可以在受保护环境中单独复现 scanner 和 source task builder：

```powershell
python .\test_harness\tools\scan_source_risks.py `
  $env:SGGK_SOURCE_ROOT `
  --out .\artifacts\internal_debug\source_scan `
  --max-findings 120 `
  --max-seeds 30

python .\test_harness\tools\build_source_attack_tasks.py `
  .\artifacts\internal_debug\source_scan `
  --out .\artifacts\internal_debug\source_tasks `
  --max-tasks 40 `
  --context-lines 12
```

scanner finding 仍只是启发式候选。源码任务只能进入获准的内网 Message API endpoint；不得为了使用 SiliconFlow 测试 endpoint 而降低源码保密边界。

### A.5 统一执行边界

维护者诊断只允许读取已有固定门禁证据；真实 SDK 执行仍必须由已批准
session 启动。仓库不再提供独立的 saved-output 批量执行 wrapper。
