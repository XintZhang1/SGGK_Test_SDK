# ABC STEP：NX / SGGK 固定单例编排

`run_nx_sggk_step_compare.py` 把一个经过 SHA-256 绑定的 ABC STEP 依次送入：

1. `run_corpus.py` 的单例 `step_import`；
2. `nx_runtime.py measure-step` 的固定 NX journal；
3. `compare_nx_sggk_step.py`；
4. 统一 JSON 与中文 Markdown 摘要。

它不接受 STEP 路径或 journal 路径。输入只能来自 `dataset_index.json` 的稳定 `files[index]`；所选条目必须包含 SHA-256，且运行前会重新计算并核对文件内容。

```powershell
python .\test_harness\tools\run_nx_sggk_step_compare.py `
  --dataset-index C:\data\abc_subset\dataset_index.json `
  --index 0 `
  --runner C:\build\Release\sggk_case_runner.exe `
  --nx-root "E:\Program Files\Siemens\DesigncenterNX2512" `
  --out C:\artifacts\nx_sggk_case_000
```

`--runner`、`--nx-root`、`--out` 必须显式提供；`--index` 默认为 `0`。可选参数包括 `--sggk-timeout`、`--nx-timeout`、`--abs-tol` 和 `--rel-tol`。

输出目录包含：

- `binding/selected_dataset_index.json`：本次唯一输入的 SHA-256 绑定；
- `sggk/`：`step_import` case、corpus manifest 和 summary；
- `nx/runtime.json` 与 `nx/measurement.json`；
- `comparison/comparison.json` 与 `comparison/comparison.zh-CN.md`；
- `run_summary.json` 与 `run_summary.zh-CN.md`：输入、命令、返回码、路径、清理记录及最终 outcome。

比较器返回 `0` 时 outcome 为 `comparison_passed`；返回 `2` 时 outcome 为 `comparison_mismatch`。二者都表示编排链路完整执行，因此编排 CLI 返回 `0`，但摘要中的 `comparison_ok` 会明确区分结果。其他阶段失败、产物绑定不一致或比较器返回其他代码时，CLI 返回 `1`。

为避免旧产物被误当作本次证据，首次运行要求 `--out` 为空或不存在。工具会写入所有权标记；再次使用同一目录时，只清理该工具登记的直接子目录和摘要文件，并把清理记录写入新摘要。非空且没有所有权标记的目录会被拒绝。
