# SGGK Harness 离线 UI 部署指南

## 离线包边界

仓库中的 `offline_bundle/` 已包含 Windows x64 所需的 CPython 3.11 embeddable、
便携 CMake、Visual C++ x64 Redistributable 以及 Harness 的全部 Python runtime/dev
wheels。每个二进制文件都记录在 `offline_bundle/manifest.json`，安装时会校验大小、
SHA-256 和关键 Python import。

目标电脑只假设已经安装 Visual Studio 2022（含“使用 C++ 的桌面开发”工作负载）。
SGGK SDK 二进制、头文件、许可和可选源码属于公司资产，不在仓库离线包内，需通过
公司批准的介质另行导入。

## 新电脑安装

1. 把完整仓库和 SGGK SDK 拷贝到目标电脑；模型调用和 ABC 下载需要能够访问外网。
2. 双击 `install_offline.cmd`。脚本只读取本仓库，不访问网络；运行时安装到
   `.offline_runtime/`，不会修改系统 Python。
3. 双击 `SGGK_Harness_UI.cmd`。默认浏览器会打开 `http://127.0.0.1:8765/`。
4. 在“外网模型与本机路径”中确认 SiliconFlow 地址、`zai-org/GLM-5.2`，填写 API key、
   可选 CA、SDK 目录和构建后的 runner 路径。可在同一页面指定 NX 安装根目录。
5. 需要 ABC 语料时，先在 UI 生成全量计划，确认磁盘空间后启动下载；也可检查并绑定含
   可验证 `dataset_index.json` 的已有 fetch 根目录，或直接绑定该索引文件。需要 NX 时
   先看静态检测，明确点击后才会运行真实探针。
6. 核心环境检查通过后，输入 public function，后续生成、审查、批准、SDK 实测和
   报告查看均在 UI 中完成。

API key 优先读取当前 Windows 用户的 Credential Manager，也可读取
`SILICONFLOW_API_KEY` 环境变量；配置 JSON 只保存在忽略的
`artifacts/harness_ui/config.json`。Web 服务只绑定 loopback，POST 请求使用随机
CSRF token，模型产物按纯文本显示，不执行其中的 HTML 或脚本。

`vc_redist.x64.exe` 作为修复性安装包保留在 `offline_bundle/archives/`。目标机已有
完整 VS2022 时通常不必运行；若原生 DLL 报运行库缺失，再由管理员按公司策略安装。
