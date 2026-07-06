# SGGK Test SDK Harness

This repository contains the standalone SGGK SDK test harness.

The SGGK SDK itself is intentionally not included. Keep the SDK, license files, build outputs, and generated artifacts outside git. Build locally by passing `SGGK_SDK_DIR` to CMake, for example:

```powershell
cmake -S .\test_harness -B .\build\test_harness `
  -DSGGK_SDK_DIR="C:/Develop/SGGK_Agent/SGK1.4.10/SGGK" `
  -G "Visual Studio 18 2026" `
  -A x64
```

See `test_harness/README.md` for the runner workflow.
