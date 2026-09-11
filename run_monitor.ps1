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

$NativeId = (
    [System.Diagnostics.Process]::GetCurrentProcess().Id.ToString() +
    '_' +
    [Guid]::NewGuid().ToString('N')
)

$StdoutFile = Join-Path `
    $LogDir `
    ("native_stdout_" + $NativeId + ".tmp")

$StderrFile = Join-Path `
    $LogDir `
    ("native_stderr_" + $NativeId + ".tmp")

$ExitCode = 98
$LauncherResult = 'UNKNOWN'

try {
    $Arguments = @(
        $MonitorPy
    )

    if ($DryRun) {
        $Arguments += '--dry-run'
    }

    $Process = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $StdoutFile `
        -RedirectStandardError $StderrFile `
        -WindowStyle Hidden `
        -Wait `
        -PassThru

    $ExitCode = $Process.ExitCode

    $StdoutLines = @()
    $StderrLines = @()

    if (Test-Path -LiteralPath $StdoutFile -PathType Leaf) {
        $StdoutLines = @(
            Get-Content `
                -LiteralPath $StdoutFile `
                -Encoding UTF8
        )
    }

    if (Test-Path -LiteralPath $StderrFile -PathType Leaf) {
        $StderrLines = @(
            Get-Content `
                -LiteralPath $StderrFile `
                -Encoding UTF8
        )
    }

    foreach ($Line in $StdoutLines) {
        Add-Content `
            -LiteralPath $LogFile `
            -Value $Line `
            -Encoding utf8

        Write-Output $Line
    }

    if ($StderrLines.Count -gt 0) {
        Add-Content `
            -LiteralPath $LogFile `
            -Value '--- PYTHON STDERR ---' `
            -Encoding utf8

        Write-Output '--- PYTHON STDERR ---'

        foreach ($Line in $StderrLines) {
            Add-Content `
                -LiteralPath $LogFile `
                -Value $Line `
                -Encoding utf8

            Write-Output $Line
        }
    }

    $RunResultLines = @(
        $StdoutLines |
        Where-Object {
            "$_" -match '^RUN_RESULT='
        }
    )

    if ($RunResultLines.Count -gt 0) {
        $LauncherResult = (
            "$($RunResultLines[-1])" -replace '^RUN_RESULT=', ''
        )
    }
    elseif ($ExitCode -eq 0) {
        $LauncherResult = 'PROCESS_EXIT_0_NO_RUN_RESULT'
    }
    else {
        $LauncherResult = (
            'PROCESS_EXIT_' +
            $ExitCode
        )
    }
}
catch {
    $ExitCode = 98
    $LauncherResult = 'LAUNCHER_EXCEPTION'

    $ErrorLine = (
        'LAUNCHER_ERROR=' +
        $_.Exception.GetType().Name +
        ': ' +
        $_.Exception.Message
    )

    Add-Content `
        -LiteralPath $LogFile `
        -Value $ErrorLine `
        -Encoding utf8

    Write-Output $ErrorLine
}
finally {
    $LauncherLine = (
        'LAUNCHER_RUN_RESULT=' +
        $LauncherResult
    )

    Add-Content `
        -LiteralPath $LogFile `
        -Value $LauncherLine `
        -Encoding utf8

    Write-Output $LauncherLine

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

    Write-Output $Footer

    Remove-Item `
        -LiteralPath $StdoutFile `
        -Force `
        -ErrorAction SilentlyContinue

    Remove-Item `
        -LiteralPath $StderrFile `
        -Force `
        -ErrorAction SilentlyContinue
}

exit $ExitCode
