param(
  [string]$Branch = "codex/abc-dataset-harness",
  [string]$SdkDir = $env:SGGK_SDK_DIR,
  [string]$BuildDir = ".\build\test_harness",
  [string]$Config = "Release",
  [string]$Generator = "Visual Studio 18 2026",
  [string]$Platform = "x64",
  [string]$Python = "python",
  [string]$Out = ".\artifacts\interface_distillation_windows",
  [string]$ModelOutputRoot = ".\artifacts\model_outputs",
  [string]$AbcFetchRoot = ".\artifacts\abc_fetch_smoke",
  [string]$SourceRoot = $env:SGGK_SOURCE_ROOT,
  [int]$Jobs = 1,
  [int]$Timeout = 180,
  [switch]$SkipGitPull,
  [switch]$SkipBuild,
  [switch]$SkipBaseSmoke,
  [switch]$SkipExampleCheck,
  [switch]$SkipExecute,
  [switch]$SkipApiSmoke,
  [switch]$SkipAbcSample,
  [switch]$SkipSourceScan,
  [switch]$SeedExampleModelOutputs,
  [switch]$FailOnFailures
)

$ErrorActionPreference = "Stop"

if (-not $SkipBuild -and [string]::IsNullOrWhiteSpace($SdkDir)) {
  throw "SdkDir is required for builds. Pass -SdkDir or set SGGK_SDK_DIR."
}
if (-not $SkipSourceScan -and [string]::IsNullOrWhiteSpace($SourceRoot)) {
  if (-not [string]::IsNullOrWhiteSpace($SdkDir)) {
    $SourceRoot = Join-Path $SdkDir "include"
  } else {
    throw "SourceRoot is required for source scans. Pass -SourceRoot or set SGGK_SOURCE_ROOT."
  }
}

function Resolve-RepoRoot {
  if ($PSScriptRoot) {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
  }
  return (Get-Location).Path
}

function New-StepRecord {
  param(
    [string]$Name,
    [string]$Status,
    [int]$ExitCode,
    [string]$LogPath,
    [string]$Command
  )
  [pscustomobject]@{
    name = $Name
    status = $Status
    exit_code = $ExitCode
    log = $LogPath
    command = $Command
  }
}

function Get-SafeName {
  param([string]$Name)
  $safe = $Name -replace "[^A-Za-z0-9_.-]+", "_"
  if ([string]::IsNullOrWhiteSpace($safe)) {
    return "step"
  }
  return $safe
}

function Invoke-Native {
  param(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [int[]]$AcceptableExitCodes = @(0)
  )
  $safeName = Get-SafeName $Name
  $logPath = Join-Path $script:LogDir "$safeName.log"
  $commandText = "$FilePath $($Arguments -join ' ')"
  Write-Host "[$Name]"
  Write-Host "  $commandText"
  & $FilePath @Arguments *> $logPath
  $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  if (Test-Path $logPath) {
    Get-Content $logPath | ForEach-Object { Write-Host $_ }
  }
  $ok = $AcceptableExitCodes -contains $exitCode
  $status = if ($ok) { "ok" } else { "failed" }
  $script:StepRecords += New-StepRecord -Name $Name -Status $status -ExitCode $exitCode -LogPath $logPath -Command $commandText
  if (-not $ok) {
    throw "$Name failed with exit code $exitCode"
  }
}

function Resolve-RunnerPath {
  param([string]$BuildRoot, [string]$RunConfig)
  $candidates = @(
    (Join-Path $BuildRoot "$RunConfig\sggk_case_runner.exe"),
    (Join-Path $BuildRoot "sggk_case_runner.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return (Resolve-Path $candidate).Path
    }
  }
  throw "sggk_case_runner.exe not found under $BuildRoot"
}

function Write-Summary {
  param([string]$SummaryPath, [string]$ReportPath)
  $summary = [pscustomobject]@{
    generated_at = (Get-Date).ToString("s")
    failed = $script:RunFailed
    failure_message = $script:FailureMessage
    branch = $Branch
    sdk_dir = $SdkDir
    build_dir = $BuildDir
    config = $Config
    runner = $script:RunnerPath
    out = $Out
    model_output_root = $ModelOutputRoot
    abc_fetch_root = $AbcFetchRoot
    source_root = $SourceRoot
    steps = $script:StepRecords
  }
  $summary | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $SummaryPath

  $lines = @(
    "# Interface Distillation Windows Run",
    "",
    "- Generated: ``$($summary.generated_at)``",
    "- Failed: ``$($summary.failed)``",
    "- Failure: ``$($summary.failure_message)``",
    "- Branch: ``$Branch``",
    "- Runner: ``$($script:RunnerPath)``",
    "- Output: ``$Out``",
    "",
    "## Steps",
    ""
  )
  foreach ($step in $script:StepRecords) {
    $lines += "- ``$($step.name)`` status=``$($step.status)`` exit=``$($step.exit_code)`` log=``$($step.log)``"
  }
  $lines += ""
  $lines += "## Reports"
  $lines += ""
  $lines += "- Distillation report: ``$(Join-Path $Out 'interface_distillation_report.md')``"
  $lines += "- Distillation summary: ``$(Join-Path $Out 'interface_distillation_summary.json')``"
  $lines += "- ABC sample report: ``$(Join-Path $Out 'abc_sample_smoke\abc_sample_smoke_report.md')``"
  $lines += "- Source attack tasks: ``$(Join-Path $Out 'source_attack_tasks\source_attack_task_manifest.md')``"
  $lines | Set-Content -Encoding UTF8 $ReportPath
}

$repoRoot = Resolve-RepoRoot
Push-Location $repoRoot
try {
  $script:StepRecords = @()
  $script:RunFailed = $false
  $script:FailureMessage = ""
  $outRoot = New-Item -ItemType Directory -Force -Path $Out
  $script:LogDir = (New-Item -ItemType Directory -Force -Path (Join-Path $outRoot.FullName "logs")).FullName
  $summaryPath = Join-Path $outRoot.FullName "windows_run_summary.json"
  $reportPath = Join-Path $outRoot.FullName "windows_run_report.md"
  $script:RunnerPath = ""

  if (-not $SkipGitPull) {
    Invoke-Native -Name "git_fetch" -FilePath "git" -Arguments @("fetch", "origin")
    Invoke-Native -Name "git_checkout" -FilePath "git" -Arguments @("checkout", $Branch)
    Invoke-Native -Name "git_pull" -FilePath "git" -Arguments @("pull", "--ff-only")
  }

  if (-not $SkipBuild) {
    Invoke-Native -Name "cmake_configure" -FilePath "cmake" -Arguments @(
      "-S", ".\test_harness",
      "-B", $BuildDir,
      "-DSGGK_SDK_DIR=$SdkDir",
      "-G", $Generator,
      "-A", $Platform
    )
    Invoke-Native -Name "cmake_build" -FilePath "cmake" -Arguments @(
      "--build", $BuildDir,
      "--config", $Config,
      "--parallel"
    )
  }

  $script:RunnerPath = Resolve-RunnerPath -BuildRoot $BuildDir -RunConfig $Config

  if (-not $SkipBaseSmoke) {
    Invoke-Native -Name "base_boolean_smoke" -FilePath $script:RunnerPath -Arguments @(
      "--recipe", ".\test_harness\recipes\boolean_smoke.json",
      "--out", ".\artifacts"
    )
  }

  if (-not $SkipExampleCheck) {
    Invoke-Native -Name "distillation_example_check" -FilePath $Python -Arguments @(
      ".\test_harness\tools\run_interface_distillation.py",
      "--out", ".\artifacts\interface_distillation_examples",
      "--model-output-root", ".\artifacts\interface_distillation_example_model_outputs",
      "--seed-example-model-outputs",
      "--check-model-outputs",
      "--require-model-outputs"
    )
  }

  if (-not $SkipExecute) {
    $executeArgs = @(
      ".\test_harness\tools\run_interface_distillation.py",
      "--out", $Out,
      "--model-output-root", $ModelOutputRoot,
      "--runner", $script:RunnerPath,
      "--execute",
      "--jobs", "$Jobs",
      "--timeout", "$Timeout"
    )
    if (-not $SkipApiSmoke) {
      $executeArgs += "--api-smoke"
    }
    if ($SeedExampleModelOutputs) {
      $executeArgs += "--seed-example-model-outputs"
    }
    if (-not $SkipAbcSample) {
      $executeArgs += @("--abc-sample-smoke", "--abc-fetch-root", $AbcFetchRoot)
    }
    if (-not $SkipSourceScan) {
      $executeArgs += @("--source-root", $SourceRoot)
    }
    if ($FailOnFailures) {
      $executeArgs += "--fail-on-failures"
    }
    Invoke-Native -Name "distillation_execute" -FilePath $Python -Arguments $executeArgs -AcceptableExitCodes @(0, 2)
  }

  Write-Summary -SummaryPath $summaryPath -ReportPath $reportPath
  Write-Host "summary=$summaryPath"
  Write-Host "report=$reportPath"
}
catch {
  $script:RunFailed = $true
  $script:FailureMessage = $_.Exception.Message
  if ($summaryPath -and $reportPath) {
    Write-Summary -SummaryPath $summaryPath -ReportPath $reportPath
    Write-Host "summary=$summaryPath"
    Write-Host "report=$reportPath"
  }
  throw
}
finally {
  Pop-Location
}
