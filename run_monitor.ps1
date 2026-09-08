param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectDir = Split-Path `
    -Parent `
    $MyInvocation.MyCommand.Path

$PythonExe = Join-Path `
    $ProjectDir `
    '.venv\Scripts\python.exe'

$MonitorPy = Join-Path `
    $ProjectDir `
    'monitor.py'

$EnvFile = Join-Path `
    $ProjectDir `
    '.env'

$StateFile = Join-Path `
    $ProjectDir `
    'state\seen_global.json'

$LogDir = Join-Path `
    $ProjectDir `
    'logs'

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    exit 10
}

if (-not (Test-Path -LiteralPath $MonitorPy -PathType Leaf)) {
    exit 11
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    exit 12
}

# Production is forbidden if bootstrap state has disappeared.
# This protects against accidentally treating all visible jobs as new.
if (-not $DryRun) {
    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        exit 13
    }
}

if (-not (Test-Path -LiteralPath $LogDir -PathType Container)) {
    New-Item `
        -ItemType Directory `
        -Path $LogDir `
        -Force |
        Out-Null
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$LogFile = Join-Path `
    $LogDir `
    (
        'production_' +
        (Get-Date -Format 'yyyyMMdd') +
        '.log'
    )

$ModeName = 'PRODUCTION'

if ($DryRun) {
    $ModeName = 'DRY_RUN'
}

$Header = (
    "`r`n" +
    ('=' * 78) +
    "`r`nRUN START " +
    (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') +
    "`r`nMODE=" +
    $ModeName +
    "`r`n" +
    ('=' * 78)
)

Add-Content `
    -LiteralPath $LogFile `
    -Value $Header `
    -Encoding utf8

$Arguments = @(
    $MonitorPy
)

if ($DryRun) {
    $Arguments += '--dry-run'
}

& $PythonExe @Arguments 2>&1 |
    ForEach-Object {

        $Line = "$_"

        Add-Content `
            -LiteralPath $LogFile `
            -Value $Line `
            -Encoding utf8

        Write-Output $Line
    }

$ExitCode = $LASTEXITCODE

$Footer = (
    "RUN END " +
    (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') +
    " EXIT_CODE=" +
    $ExitCode
)

Add-Content `
    -LiteralPath $LogFile `
    -Value $Footer `
    -Encoding utf8

exit $ExitCode
