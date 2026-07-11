# SGGK Test SDK Harness

This repository contains the standalone SGGK SDK test harness.

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
parallel Qwen candidates -> fixed gates -> real SDK/plugin execution
                         -> deterministic selection -> atomic promotion
                         -> qualification/replay/TopoTrack -> candidate bug report
```

Start with:

- `test_harness/HARNESS_ARCHITECTURE.md` for trust boundaries and the complete data flow;
- `test_harness/SILICONFLOW_MESSAGE_API_TESTING.md` for intranet and explicit SiliconFlow profiles;
- `test_harness/INTERFACE_TEST_MATRIX.md` for built-in and plugin API coverage;
- `test_harness/README.md` for runner and campaign commands;
- `docs/ABC_DATASET_LARGE_TEST_PLAN.md` for staged large-scale execution.
