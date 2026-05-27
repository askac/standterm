param(
    [Alias("d")]
    [string]$Dir = $null
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/askac/standterm.git"
if ([string]::IsNullOrWhiteSpace($Dir)) {
    if ($env:STANDTERM_DIR) {
        $InstallDir = $env:STANDTERM_DIR
    } else {
        $InstallDir = Join-Path $HOME "standterm"
    }
} else {
    $InstallDir = $Dir
}

Write-Host "========================================"
Write-Host "   StandTerm Installer (Windows)"
Write-Host "========================================"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[!] ERROR: git is required but not found."
    Write-Host "    Install with: winget install --id Git.Git -e"
    exit 1
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[!] ERROR: python is required but not found."
    Write-Host "    Install with: winget install --id Python.Python.3.12 -e"
    Write-Host "    Then reopen PowerShell so python is on PATH."
    exit 1
}

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "[*] Existing installation found at: $InstallDir"
    Write-Host "[*] Pulling latest changes..."
    git -C $InstallDir pull --ff-only
} else {
    Write-Host "[*] Installing to: $InstallDir"
    git clone $RepoUrl $InstallDir
}

$RunBat = Join-Path $InstallDir "run.bat"
if (-not (Test-Path $RunBat)) {
    Write-Host "[!] ERROR: run.bat was not found after install: $RunBat"
    exit 1
}

Write-Host "[+] Done. Launching StandTerm..."
Write-Host ""
& $RunBat
