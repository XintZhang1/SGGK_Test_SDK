# SGGK Harness 生成代码与测试用例审查指南

## 1. 目的与适用范围

本指南规定 Harness 生成的 adapter、schema、smoke、negative、recipes、cases 及其 provenance 如何进入中文多轮审查，并在用户批准后自动执行真实 SGGK SDK 测试。目标是让普通用户只表达测试对象和自然语言意见，同时让固定宿主保存完整、可恢复、可复核的证据链。

适用产物包括：

- Message API 返回并由固定宿主筛选的候选；
- 固定模板物化的 adapter 代码及注册信息；
- 测试定义、schema、正例、负例、smoke 和批量 recipes；
- 批准后的构建、执行、语义重放和故障定位报告；
- 与上述文件绑定的 session、轮次、comment、批准和 provenance 证据。

模型输出始终是不可信候选。GLM-5.2 可以解释意见和修订候选，但不能声明固定门禁通过、不能批准轮次，也不能启动 runner。

## 2. 普通用户审查流程

普通用户只使用仓库根目录的统一入口。底层实现为 `test_harness/tools/sggk_harness.py`。

### 2.1 开始一个 public function

```powershell
.\harness.ps1 start api_boolean
```

Harness 自动解析函数声明、重载、能力、源码证据、风险、输入构造和 Oracle，并创建第 1 轮审查报告。用户不填写表单，不提供 task/session/run/candidate ID、round、hash、manifest、JSON、runner 或并行参数。

### 2.2 提交自然语言 comment

```powershell
.\harness.ps1 comment "增加退化输入、近容差相交和空结果检查"
```

每条 comment 都必须：

1. 以原文保存，不能由模型改写后替代；
2. 绑定当前最新轮次，由固定宿主生成内部哈希；
3. 通过同一个 Message API contract 交给 GLM-5.2 解释；
4. 形成不可变 comment 事件；
5. 被明确分类为内部 `decision=revise|question|reject|approve`；这些是模型响应枚举，不是 CLI 命令。

不同语义的结果不同：

- `revise`：涉及代码、用例、参数、Oracle、范围或假设修改，必须生成完整替换候选、不可变新轮次和新中文报告；
- `question`：把模型中文回答保存在当前轮，不修改候选；
- `reject`：保存拒绝原因并终止 session，不执行 SDK；
- `decision=approve`：仅当原始 comment 明确同意执行时，固定宿主才绑定并执行当前最新轮次，不生成新候选轮；普通用户仍只使用 `comment`。

GLM-5.2 对所有 comment 的中文解释至少包含意见理解、语义分类和分类理由。`revise` 还必须包含：

- 本轮采纳项；
- 未采纳项及具体原因；
- 相对上一轮的代码、用例、参数、Oracle 和风险覆盖变化；
- 未解决问题和需要用户判断的假设。

`decision=question` 应给出当前轮可验证的中文回答；`decision=reject` 应复述拒绝范围和未执行事实；`decision=approve` 应说明批准的是当前哪一轮及“尚不代表执行通过”。这些值都只存在于内部 Message API JSON。

### 2.3 批准并自动执行

```powershell
.\harness.ps1 comment "这一版可以开始执行真实测试。"
```

用户通过上面的明确批准 `comment` 同意执行。内部 `decision=approve` 只作用于当前最新轮次，并且不生成新候选轮；它不是 CLI 命令。Harness 会形成明确批准 comment 事件，保存 GLM-5.2 对批准语义的中文解释；固定宿主随后验证轮次链、候选、源码 contract 和受审产物，写入不可变批准事件，并自动运行真实 SGGK SDK 构建、测试、Oracle、triage、replay 和适用的故障定位。用户不复制或编辑任何批准 JSON，也不手工启动 runner。

执行完成后必须生成：

```text
<session-root>/execution/round_NNNN/attempt_NNNN/final_report.zh-CN.md
```

### 2.4 查看和恢复

```powershell
.\harness.ps1 status
.\harness.ps1 show
.\harness.ps1 retry
```

- `status` 显示当前目标、最新轮次和状态，不要求用户提供 ID。
- `show` 显示当前最新审查报告或最终报告。
- `retry` 只重试当前已批准轮次的失败或中断执行，不修改测试方案。
- 方案需要变化时必须提交明确的修改 comment，让系统生成新轮次。
- 要测试另一个 public function，应先完成当前任务，或通过 comment 明确拒绝并终止它，然后重新 `start`。

## 3. Session、轮次和状态边界

### 3.1 状态机

```text
generating -> awaiting_comment
awaiting_comment -> interpreting_comment
interpreting_comment -> awaiting_comment               # question
interpreting_comment -> rejected                       # reject
interpreting_comment -> generating -> awaiting_comment # revise，新轮次
interpreting_comment -> executing                      # 明确批准 comment；内部 decision=approve
executing -> completed | execution_failed
generating -> generation_failed
```

| 状态 | 含义 | 允许的用户操作 |
| --- | --- | --- |
| `awaiting_comment` | 当前最新轮次已生成并通过批准前固定门禁 | `comment`（包括明确批准执行的自然语言 comment） |
| `interpreting_comment` | GLM-5.2 正在解释并分类当前 comment | `status` |
| `generating` | 正在生成第 1 轮或修改后的新轮次 | `status` |
| `rejected` | 用户已拒绝当前任务，未执行 SDK | `show` |
| `executing` | 正在执行真实 SGGK SDK 测试 | `status` |
| `completed` | 执行结束并生成最终报告 | `show` |
| `execution_failed` | 执行失败、中断或基础设施不完整 | `show`、`retry` 或以修改 comment 修订方案 |
| `generation_failed` | 候选生成或批准前固定门禁失败 | `status`，由维护者诊断后重新开始/恢复 |

机器门禁状态、审查状态和真实执行状态必须分开记录。批准前门禁通过不代表用户批准；用户批准不代表真实测试通过；执行失败不能被批准状态覆盖。

### 3.2 不可变性规则

1. `start` 创建第 1 轮；每条 comment 都创建不可变事件，但只有 `revise` 创建下一候选轮。
2. 内部 `decision=question|reject|approve` 都不生成新候选轮；它们不是用户命令。
3. 只有最新轮次可以批准。
4. 已完成任务后提交涉及修改的 comment，会创建新轮次并使旧批准只保留为历史证据。
5. 候选、代码、schema、recipe、源码 contract 或报告内容变化时必须形成新轮次。
6. 执行前必须再次验证批准绑定轮次仍是最新轮次。
7. GLM-5.2 无权写入最终批准状态；固定宿主只有在模型将 comment 解释为 `decision=approve` 且宿主独立检测到明确执行同意后，才会产生批准。
8. 用户不接触内部 ID、round index 或 hash；这些值仍必须完整保存供恢复和审计。

### 3.3 批准前与批准后门禁

批准前可以执行 JSON contract、schema、DSL/recipe 静态验证、受控物化、编译可行性检查及其他不会启动正式 SDK case 的固定门禁。真实 SDK runner、批量测试、回归复现和故障调查只能在最新轮次获得有效批准后启动。

### 3.4 Message API endpoint

外网版本只连接显式配置的 SiliconFlow GLM-5.2 endpoint，并统一使用 Message API contract、轮次协议、中文解释 schema 和固定门禁。不存在失败后的 provider/model 自动 fallback；`proprietary_source` 任务仍只允许获准的 `intranet` profile。

## 4. 报告和内部证据

### 4.1 每轮中文审查报告

每个 `rounds/<round>/review/第<round>轮测试方案审查.zh-CN.md` 应帮助用户快速确认：

- 目标 public function、命名空间、签名和重载是否正确；
- 测试目的、输入、预期行为和 Oracle 是否明确；
- 风险假设是否落实为实际参数变化或调用序列；
- 生成代码、schema、正例、负例和 recipes 的变化；
- 若由修改 comment 派生，本轮的 comment 原文和模型中文解释；
- 当前状态是否等待 comment 或批准。

报告不是批准证明。编辑 Markdown 不改变轮次、内部哈希或批准状态。用户只能通过修改语义的 `comment` 创建修订，通过明确同意执行的 `comment` 批准当前最新轮次。

### 4.2 内部 review packet 与轮次链

每轮内部证据至少包括：

- `internal/api_test_form.json`：Harness 根据 public function 推导的内部请求；
- `prompt/model_task_manifest.json`：内部 Message 任务；
- `candidate/candidate.json`：Message API 候选；
- 如当前轮收到 comment，`comments/<comment-hash>/user_comment.txt` 与 `interpretation.json` 保存原文和模型中文解释；
- `pipeline/` 中的 `review_packet.json`：结构化、机器可复核的固定审查包；
- `review/第<round>轮测试方案审查.zh-CN.md`：面向用户的中文报告；
- `round_manifest.json`：父轮次、comment、候选、产物和报告的哈希链。

内部 packet 应记录任务、模型 contract、候选、来源引用、用例、产物、批准前门禁和完整性信息。Harness 自动校验这些字段；普通用户不核对、不复制、不填写哈希。

### 4.3 批量 recipe 审查

批量 recipe 仍应生成机器索引和中文汇总，覆盖 recipe 总数、API 分布、Oracle 覆盖、重复输入、风险分布和抽样预览。它们属于当前 session 和轮次的一部分。

以下情况应全量审查，而不是只抽样：

- 新 API 或新 adapter 首次进入测试；
- schema、固定模板或执行器发生变化；
- 涉及删除、所有权转移、跨对象状态或容差边界；
- 批准前门禁出现新的失败类型；
- provenance 不完整或来源绑定发生变化。

用户若只想保留批量集合的子集，应提交 comment 说明筛选目标，由 Harness 生成新轮次和新索引。不得手工增删索引行后沿用旧批准。

### 4.4 最终中文报告

`final_report.zh-CN.md` 至少应包含：

- 目标 public function 和最终批准轮次；
- 各轮 comment、模型理解和主要变化摘要；
- 实际执行的代码、用例和 Oracle 覆盖；
- 构建、SDK status、TopoCheck、validation 和稳定性结果；
- 失败分类、复现路径、triage/replay/TopoTrack/failure bundle；
- 未解决风险和能力缺口；
- 完整 session 证据目录。

`execution_failed` 时仍必须生成最终报告或失败报告，并明确说明“批准记录存在，但测试未通过”。

## 5. 分项审查清单

### 5.1 Adapter

- [ ] 目标 API、函数签名、SDK header、模块和 adapter archetype 与受信 intake 一致。
- [ ] 只使用固定宿主允许的模板和注册入口；模型没有提供 C++、构建命令、链接参数或任意路径。
- [ ] 参数转换、单位、容差、枚举和默认值符合接口语义。
- [ ] 输入对象的所有权、生命周期、空值和失败清理路径明确。
- [ ] 返回状态和 SDK 错误被转换为稳定、可断言的结果。
- [ ] runtime registry 中存在目标 adapter，且 registry 哈希与构建报告一致。
- [ ] 隔离构建通过，产物来自当前 candidate 和当前 SDK 输入。

### 5.2 Schema

- [ ] 必填字段、类型、枚举、数值范围和数组长度与 adapter 实现一致。
- [ ] 未声明字段按策略拒绝，不能静默吞掉拼写错误。
- [ ] 正例覆盖最小合法输入和代表性完整输入。
- [ ] 负例覆盖缺字段、错类型、越界、非法枚举、额外字段和互斥条件。
- [ ] schema 版本、API 身份和 capability 声明彼此一致。
- [ ] schema 不能携带命令、runner、环境变量、网络地址或宿主文件路径。

### 5.3 Smoke

- [ ] smoke 调用的是目标 API，而不是语义相近的其他 API。
- [ ] 输入可以在隔离环境中确定性构造，不依赖历史执行残留。
- [ ] 至少有一个结果 Oracle；仅检查“调用成功”不充分。
- [ ] Oracle 覆盖关键属性、数量、关系、距离、状态或往返一致性中的适用项。
- [ ] 执行超时、崩溃、SDK 错误和 Oracle 失败能被区分。
- [ ] 需要稳定性证明时，重复执行的语义哈希和运行时注册证据一致。

### 5.4 Negative

- [ ] 每个关键 schema 约束至少有一个针对性负例。
- [ ] 每条负例只改变必要条件，能明确定位被验证的规则。
- [ ] 预期是受控拒绝或明确错误，而不是崩溃、挂起或未定义行为。
- [ ] 负例不会误用不存在的 API、无关输入或宿主权限来制造失败。
- [ ] 正例与负例不存在相同输入、相反预期的矛盾。

### 5.5 Recipes

- [ ] `case_id` 唯一、稳定，并能关联来源风险或任务 ID。
- [ ] target/tool、输入资产、调用顺序和参数组合与测试目标一致。
- [ ] 数值边界使用成组的低于/等于/高于阈值变体，而非单点过拟合。
- [ ] expectations 至少包含一个可观测且可重复的 Oracle。
- [ ] recipe 不含命令、runner、环境变量、网络地址或越权路径。
- [ ] 资源规模、重复次数和超时有上限。
- [ ] 失败 recipe 只有在稳定同签名重放后才进入正式复现资产。

### 5.6 Cases

- [ ] 正常、边界、退化、顺序变化和组合变化按目标风险覆盖。
- [ ] 每个 case 的输入摘要、Oracle 摘要和失败假设可以一一对应。
- [ ] case 之间相互独立；顺序改变不应改变结果，除非测试目标就是状态序列。
- [ ] 批量集合没有大量同构重复项挤占覆盖预算。
- [ ] 新失败先进入资格判定和稳定重放，不直接升级为已确认缺陷。
- [ ] 审查预览包含高风险、复杂输入、负例和批准前机器失败项。

### 5.7 Provenance

- [ ] profile、model、run、candidate、role 和 selection policy 完整。
- [ ] prompt、message content、candidate、正式输出和关键报告均有 SHA-256。
- [ ] `authoring_accepted` 只能由固定 Message Harness 宿主写入。
- [ ] session、活动轮次和父轮次链一致，每条 comment 原文都有独立绑定。
- [ ] 模型 comment 解释、candidate、packet、中文报告和 round manifest 指向同一轮次。
- [ ] 批准前 fixed gate 与批准后构建、执行、runtime registry 和语义重放状态分开保存。
- [ ] approval event 由固定宿主生成，并绑定批准时的最新轮次。
- [ ] 执行开始前再次验证批准轮次仍是最新轮次；任何产物变化都会使旧批准失效。
- [ ] 凭据、授权头、模型推理文本和未脱敏供应商响应未被持久化。

## 6. 推荐审查顺序

普通用户：

1. `start <public-function>` 后运行 `show`，核对目标接口、风险、用例和 Oracle。
2. 有任何缺口就运行 `comment "..."`；问题留在当前轮回答，修改意见会生成新轮次和差异。
3. 按需重复 comment，直到当前最新候选轮符合预期。
4. 运行 `.\harness.ps1 comment "明确同意当前方案，可以开始执行真实测试。"`，等待 Harness 自动完成真实 SDK 执行。
5. 阅读 `final_report.zh-CN.md`；测试失败时再查看复现和定位证据。

Harness 固定宿主和高级审计者还应验证 provenance、round manifest、packet/索引、adapter/schema 一致性、正负例、fixed gate、approval event 及批准后执行绑定。普通用户不需要读取或填写内部 ID 和哈希。

最终交付应同时回答三个问题：批准前机器为什么认为方案可审查，用户批准的是哪一个不可变轮次，以及批准后的真实 SGGK SDK 执行得到了什么结果。
