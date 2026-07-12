param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("start", "comment", "status", "show", "retry")]
    [string]$Command,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Value
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $RepoRoot "test_harness\tools\sggk_harness.py"

if (($Command -eq "start" -or $Command -eq "comment") -and (!$Value -or $Value.Count -eq 0)) {
    throw "$Command requires one public-function name or one natural-language comment."
}

$Arguments = @($Launcher, $Command)
if ($Value) {
    $Arguments += ($Value -join " ")
}

& python @Arguments
exit $LASTEXITCODE
