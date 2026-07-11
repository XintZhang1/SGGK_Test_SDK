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

See `test_harness/README.md` for the runner workflow.
