# SGGK Harness 源码风险驱动测试指南

## 1. 目标

源码风险驱动测试用于把实现中的分支、阈值、错误处理、对象状态和资源边界转化为可执行测试。标准闭环为：

> 通用源码扫描 → `source_review` → 内网 Qwen → 固定门禁 → 测试增强 → 执行与反馈

源码只能提供风险线索，不能直接证明缺陷。模型输出只能是测试候选；固定宿主负责规范化、验证、编译、执行、选择和证据持久化。

## 2. 不可覆盖的数据边界

源码驱动任务只能使用 `intranet` profile 和内网 Qwen。

任何源码、源码摘要、源码片段均不得离开内网。该禁令同样覆盖由源码派生的：

- 文件路径、符号名、行号和调用关系；
- 控制流描述、风险摘要和失败假设；
- `source_review`、扫描发现、任务 prompt 和上下文包；
- 片段哈希、来源引用及可反推出实现信息的元数据；
- 调试过程中检索到的任何源码证据。

不得启用任何允许外发源码证据的兼容开关。外部模拟器只可用于完全不包含源码及源码派生信息的独立协议测试或公开合成用例。

若 profile 不是 `intranet`、来源边界无法确认、任务包混入源码信息或日志可能外发，流程必须 fail closed，不得降级到外部端点，也不得人工复制粘贴继续。

## 3. 角色与信任边界

| 组件 | 责任 | 信任限制 |
| --- | --- | --- |
| 固定源码扫描器 | 在受信本地根目录内发现候选风险并生成有界报告 | 只提供线索，不判定缺陷 |
| 任务构建器 | 选择风险、截取有界上下文、生成来源引用和片段哈希 | 不执行模型生成命令，不扩展来源根目录 |
| 内网 Qwen | 阅读受控任务并提出 `source_review`、测试 DSL 或扩展需求 | 输出不可信，不能执行工具或修改源码 |
| 固定门禁 | 验证输出结构、来源绑定、schema、能力范围和执行条件 | 所有命令、路径和 runner 由宿主拥有 |
| 测试执行器 | 进程隔离执行 recipes 并生成报告 | 不因模型声明而跳过 Oracle 或安全限制 |
| 用户自然语言复核 | 核对风险理解、覆盖设计和证据，提交 comment | 不能覆盖机器门禁失败或手工填写批准状态 |

## 4. 阶段一：通用源码扫描

### 4.1 扫描范围

扫描应从明确的只读、受信源码快照开始。记录根目录、快照版本、扫描参数、排除目录和报告 SHA-256。默认排除构建目录、历史 artifacts、依赖缓存和生成输出，避免把旧测试结果误当成实现证据。

通用扫描至少关注：

- 临界比较、容差、尺寸阈值和数值常量；
- 空值、空集合、索引、枚举、状态码和异常分支；
- 创建、复制、转移、删除、失败清理和所有权变化；
- 多对象组合、顺序依赖、重复调用和状态复用；
- 成功返回但结果可能为空、不完整或不可用的路径；
- 跳过校验、忽略错误、临时分支和不可达假设；
- 大输入、深序列、超时、内存和并发边界；
- 导入、导出、序列化和往返一致性；
- 已有注释与实际控制流不一致的高风险位置。

扫描分数用于排序，不是缺陷严重性。相同位置的同类发现应去重；高风险分支优先生成任务，低价值命中保留在本地报告中。

### 4.2 本地扫描示例

```powershell
python .\test_harness\tools\scan_source_risks.py `
  <受信源码根目录> `
  --out .\artifacts\source_risk_scan `
  --context-lines 1 `
  --max-findings 500

python .\test_harness\tools\build_source_attack_tasks.py `
  .\artifacts\source_risk_scan `
  --out .\artifacts\source_attack_tasks `
  --min-severity medium `
  --context-lines 12 `
  --max-tasks 120
```

这些输出全部留在受信环境中。不得把扫描报告或任务目录同步到外部模型服务。

## 5. 阶段二：构造 `source_review`

每个源码驱动候选必须包含结构化 `source_review`：

```json
{
  "source_review": {
    "schema_version": 1,
    "task_id": "source_task_001",
    "finding_id": "risk_001",
    "source_contract_sha256": "<宿主签发的契约 SHA-256>",
    "summary": "说明受控片段中的风险控制流，以及为什么需要新增测试。",
    "source_refs": [
      {
        "source_ref_id": "src_0123456789abcdef",
        "line_start": 120,
        "line_end": 148,
        "content_sha256": "<宿主绑定的片段 SHA-256>"
      }
    ],
    "risky_branches": [
      {
        "branch_id": "branch_01",
        "source_ref_ids": ["src_0123456789abcdef"],
        "condition": "阈值两侧进入不同处理路径",
        "risk": "边界分类可能与后续构造使用的容差不一致"
      }
    ],
    "failure_hypotheses": [
      {
        "hypothesis_id": "hyp_01",
        "branch_ids": ["branch_01"],
        "trigger": "输入恰好位于阈值",
        "observable_failure": "返回成功但结果为空或无效"
      },
      {
        "hypothesis_id": "hyp_02",
        "branch_ids": ["branch_01"],
        "trigger": "输入位于阈值两侧的有符号容差带",
        "observable_failure": "拓扑结果在相邻样例间不稳定"
      }
    ],
    "test_enhancements": [
      {
        "enhancement_id": "enh_01",
        "hypothesis_ids": ["hyp_01", "hyp_02"],
        "case_ids": ["case_exact", "case_below", "case_above"],
        "strategy": "生成阈值下方、等于阈值和阈值上方三组用例",
        "perturbations": ["正负几何容差", "正负建模容差"],
        "oracles": ["结果数量", "有限属性", "拓扑有效性"]
      }
    ]
  }
}
```

约束如下：

- `summary` 必须描述控制流风险，不能只改写扫描器类别名称；
- `task_id`、`finding_id`、`source_contract_sha256` 必须与宿主契约完全一致；
- `source_refs` 只能使用宿主签发的 opaque ID，行范围和内容哈希不得改写或增补；
- 每个 `risky_branches` 项必须引用来源 ID 并描述具体条件和风险；
- `failure_hypotheses` 至少包含两个可证伪假设，且每项必须引用已声明分支；
- 每个 `test_enhancements` 项必须引用已声明假设和真实生成的 `case_id`，并列出扰动与 Oracle；
- 每个来源、分支、假设和生成 case 都必须至少被下游关系引用一次，不能形成孤立节点；
- 不确定的符号、路径或行为应标记未知，禁止编造来源证据。

`source_review` 是风险到测试的可追溯桥梁，不是自然语言结论仓库。每条失败假设都应至少对应一个测试增强项和一个可观测 Oracle。

## 6. 阶段三：仅调用内网 Qwen

通过唯一的 review session 入口明确使用 `intranet`。宿主会自动完成定义发现、manifest、候选生成和固定门禁；用户不生成或编辑任务 JSON：

```powershell
$env:SGGK_HARNESS_PROFILE = "intranet"
$env:SGGK_SOURCE_ROOT = "<approved-intranet-source-root>"
.\harness.ps1 start api_boolean
.\harness.ps1 comment "增加源码分支对应的容差两侧、退化输入和可观测 Oracle。"
.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"
```

调用前必须确认：

- endpoint、模型和凭据来自内网 profile；
- manifest 中所有 `source_attack` 任务均指向受信本地任务；
- 不存在外部 fallback；
- 请求、响应、日志和审查产物均保留在内网；
- 模型输出预算、候选数、并行度和修复轮次均有上限。

允许模型输出的内容是声明式测试定义，例如 attack DSL、cluster seed 或 `needs_harness_extension`。模型不得输出或控制命令、runner、工作目录、环境变量、构建参数、网络地址、源码补丁或宿主文件路径。

## 7. 阶段四：固定门禁

固定门禁按顺序验证：

1. Message API 响应恰好包含一个受支持的 JSON 对象；
2. 输出 kind 在任务允许集合内；
3. `source_review` 字段完整，task/finding/契约哈希与任务一致，来源→分支→假设→增强→case 引用图闭合；
4. DSL、cluster 或扩展请求符合固定 schema；
5. API、能力、输入和 Oracle 均属于 Harness 注册范围；
6. 规范化、编译和执行命令全部来自固定宿主；
7. 候选经隔离执行、评分和选择后才可机器接受；
8. 生成 `review_packet.json` 和中文审查报告，状态保持 `awaiting_comment`；用户只提交自然语言意见。

以下情况必须拒绝：

- 模型遗漏或改写来源绑定；
- 当前源码字节、文件哈希或 prompt 与 accepted provenance 不一致；
- 分支、假设、增强或 case ID 引用断裂，或加入未注册来源引用；
- 测试没有可观测 Oracle；
- 生成内容要求任意命令、路径、网络或源码修改；
- 候选使用未注册 API、未支持操作或超出资源上限；
- 仅凭源码描述宣称“已确认缺陷”；
- profile 不是内网。

## 8. 阶段五：测试增强

固定门禁通过后，按 `source_review` 把风险扩展为成组测试，而不是单个偶然样例。

### 8.1 建议的增强维度

- 阈值：低于、等于、高于，以及不同数量级；
- 形态：正常、边界、退化、近重合、极端长宽比；
- 顺序：交换输入顺序、重复调用、失败后重试、序列中插入无关调用；
- 状态：新对象、复制对象、共享对象、已修改对象和清理后的对象；
- schema：最小合法、完整合法、缺字段、错类型、越界和互斥冲突；
- 关系：对称性、单调性、幂等性、往返一致性和结果间约束；
- 资源：有界的大输入、长序列、超时和并行执行；
- 失败语义：受控 SDK 错误、Oracle 失败、崩溃和超时分别分类。

### 8.2 Oracle 要求

每个 case 至少包含一个固定、可重复的结果 Oracle。优先使用：

- 返回状态与错误码组合；
- 结果对象数量和类型；
- 属性、距离、位置或对象关系；
- 输入交换后的对称/非对称预期；
- 重复执行或往返后的语义一致性；
- 输出有效性与后续可操作性。

只验证 API 返回成功不能证明风险分支行为正确。Oracle 的预期值也不能从单次失败输出反向拟合。

## 9. 执行、资格判定与反馈

测试增强后的 recipes 由固定 runner 进程隔离执行，并生成 recipe 审查索引和中文报告。结果按以下顺序处理：

1. 区分 Harness/基础设施错误与 SDK/Oracle 结果；
2. 对失败执行确定性资格判定；
3. 使用至少三次、绑定原始失败签名的重放验证稳定性；
4. 仅对稳定同签名失败执行签名保持的 reduction；
5. 不稳定、签名变化或不可复现结果保留为不确定证据，不生成正式 reproducer；
6. 将实际覆盖、失败类型和遗漏 Oracle 回写到下一轮扫描优先级与 `test_enhancements`。

源码假设与运行结果应形成可追溯矩阵：

| 风险 ID | 来源引用 | 失败假设 | case_id | Oracle | 机器结果 | Harness 审查状态 |
| --- | --- | --- | --- | --- | --- | --- |
| `<risk>` | `<source_ref>` | `<可证伪假设>` | `<case>` | `<oracle>` | `<passed/failed/inconclusive>` | `<awaiting_comment/approved_execution/completed/rejected>` |

## 10. 完成标准

一轮源码风险驱动测试只有在以下条件全部满足时才算完成：

- 扫描范围、版本、参数和报告哈希可追溯；
- 所有源码相关数据始终留在内网；
- 每个候选的 `source_review` 与宿主绑定的来源引用和片段哈希一致；
- 每条高优先级风险至少有一个可执行 case 和一个结果 Oracle；
- 固定门禁、构建、执行和 provenance 证据完整；
- 批量 recipe 索引和中文审查报告已生成；
- 机器候选接受与 Harness 的自然语言审查、明确执行同意及最终执行状态严格分离；不存在人工编辑状态 JSON 的路径；
- 失败只有在稳定同签名重放后才进入正式复现和后续定位。

这套流程的核心不是让模型“阅读源码后猜问题”，而是把实现风险转换为受控、可证伪、可重复且可通过自然语言 comment 复核的测试证据。
