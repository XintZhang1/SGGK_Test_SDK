# NX / SGGK 布尔双向对比工具链

把同一个 SGGK 布尔用例同时交给 SGGK 与 Parasolid（NX），用**同一把
Parasolid 尺子**测量两边结果，给出五类双向判定，不预设任何一方权威。

## 判定类别

| verdict | 含义 |
|---|---|
| `both_correct` | 两内核结果均为封闭实体，且 Parasolid 测量体积/面积/Body 数互相一致 |
| `sggk_correct` | SGGK 结果经 Parasolid 测量为封闭有效实体；Parasolid 布尔失败或结果非封闭 |
| `parasolid_correct` | Parasolid 结果为封闭有效实体；SGGK API 失败或结果经 Parasolid 测量非封闭/无效 |
| `both_wrong` | 两内核均失败或结果均非封闭/无效 |
| `inconclusive` | 证据不足：Parasolid 无法导入（范围/格式限制）、缺测量、或双方各自封闭但测量不一致 |

关键设计：**两边都用 Parasolid 测量**。SGGK 自报的 `properties.json`
只作次要证据；体积/面积/封闭性判定以 Parasolid 测量为准（用户明确要求
"体积面积工具不好用时用 Parasolid 计算"）。封闭性用自由边探针（共享面数
少于 2 的边）判定，直接暴露"近切布尔减得到非封闭 solid"这类问题。

Parasolid 对超大坐标（如 1e6）无法导入时判 `inconclusive` 而非
`both_wrong`——那是 Parasolid 的建模范围限制，不是两个内核都算错。

## 组成

- `test_harness/nx_journals/boolean_measure.py`：固定 NX journal。导入
  target+tool STEP → Parasolid 布尔（unite/subtract/intersect）→ 测量结果
  （体积/面积/Body 数/自由边封闭性）→ 可选导出结果 STEP。数据专用，不接受
  任何代码路径。
- `test_harness/tools/export_case_step.py`：把用例的 `input/target.sgt`、
  `input/tool.sgt`、`output/result_*.sgt` 导出为 STEP。复用成熟的
  `step_roundtrip` 车道，只取导出的 `roundtrip.step`（忽略 roundtrip 的
  导入/比对结果，避免导入侧 SDK 缺陷掩盖成功导出）。
- `test_harness/tools/compare_nx_sggk_boolean.py`：五类判定比较器。
- `test_harness/tools/run_nx_sggk_boolean_compare.py`：编排器，串起
  导出 → Parasolid 布尔 → SGGK 结果 Parasolid 测量 → 判定；支持批量、
  分片、断点续跑。

## 单例用法

```powershell
python .\test_harness\tools\run_nx_sggk_boolean_compare.py `
  --case <boolean-case-artifact-dir> `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --nx-root "E:\Program Files\Siemens\DesigncenterNX2512" `
  --out E:\datasets\abc_runs\nx_boolean\case_001
```

用例目录须含 `input/target.sgt`、`input/tool.sgt`、`report/status.json`、
`report/properties.json`；布尔操作类型从 `input/recipe.json` 的
`boolean_type`（UNION/SUBTRACTION/INTERSECTION）映射。

## 批量用法

```powershell
python .\test_harness\tools\run_nx_sggk_boolean_compare.py `
  --cases-root <cases-dir> `
  --runner .\build\test_harness\Release\sggk_case_runner.exe `
  --nx-root "E:\Program Files\Siemens\DesigncenterNX2512" `
  --out E:\datasets\abc_runs\nx_boolean\batch `
  --shard-count 4 --shard-index 0 --resume
```

`--cases-root` 自动挑出含 target/tool SGT 的子目录；也可用 `--case-list`
指定清单。`--resume` 跳过已有 `comparison.json` 的用例；分片按稳定排序
取模。每个用例写出 `export/`（STEP + SHA-256 清单）、`nx/`（Parasolid 布尔
与 SGGK 结果测量）、`comparison/comparison.json` 与中文 `.md`，批量根目录
写 `batch_summary.json`（各类 verdict 计数）。

## 注意

- NX journal 每次启动有秒级开销，批量全量跑按过夜设计；用分片并行。
- 当前布尔 journal 要求 target、tool 各为单一实体（覆盖全部生成用例与
  recut 单 body 车道）；多 body 装配体会得到
  `NX_BOOLEAN_MULTI_BODY_UNSUPPORTED` 诊断并判 `inconclusive`。
- 大数据集与运行产物始终放 E 盘，不进 git。
