param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,

    [string]$PythonVersion = "3.12.10",

    [string]$OutputPath = $null
)

$ErrorActionPreference = "Stop"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Newer PowerShell versions choose a suitable protocol automatically.
}

$ToolsDir = Join-Path $ProjectDir "tools"
$PortableDir = Join-Path $ToolsDir "python-embed"
$PythonExe = Join-Path $PortableDir "python.exe"
$StampFile = Join-Path $PortableDir ".standterm-python-embed-$PythonVersion"

function Test-PortablePython {
    param([string]$PythonPath)

    if (-not (Test-Path $PythonPath)) {
        return $false
    }
    & $PythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Enable-EmbeddedSite {
    param([string]$PythonDir)

    $PthFile = Get-ChildItem -Path $PythonDir -Filter "python*._pth" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $PthFile) {
        return
    }

    $PthLines = Get-Content -Path $PthFile.FullName
    $PthLines = $PthLines | ForEach-Object {
        if ($_ -eq "#import site") { "import site" } else { $_ }
    }
    if ($PthLines -notcontains "Lib\site-packages") {
        $PthLines += "Lib\site-packages"
    }
    Set-Content -Path $PthFile.FullName -Value $PthLines -Encoding ASCII
}

function Ensure-Pip {
    param([string]$PythonPath)

    & $PythonPath -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    $TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("standterm-get-pip-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
    try {
        $GetPipPath = Join-Path $TempDir "get-pip.py"
        Write-Host "[*] Installing pip into embedded Python..."
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath
        & $PythonPath $GetPipPath --no-warn-script-location
        if ($LASTEXITCODE -ne 0) {
            throw "get-pip.py failed with exit code $LASTEXITCODE."
        }

        & $PythonPath -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "pip is not available after bootstrap."
        }
    } finally {
        Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
    }
}

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64" -and $env:PROCESSOR_ARCHITEW6432 -ne "AMD64") {
    Write-Error "Automatic embedded Python fallback currently supports Windows x64 only. Install Python 3.10+ manually, then rerun run.bat."
    exit 1
}

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

if (Test-PortablePython $PythonExe) {
    Write-Host "[*] Using existing embedded Python: $PythonExe"
    Enable-EmbeddedSite $PortableDir
    Ensure-Pip $PythonExe
    Set-Content -Path $StampFile -Value $PythonVersion -Encoding ASCII
    if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
        Set-Content -Path $OutputPath -Value $PythonExe -Encoding ASCII
    }
    Write-Output $PythonExe
    exit 0
}

if (Test-Path $PortableDir) {
    Write-Host "[*] Existing embedded Python directory is incomplete; rebuilding: $PortableDir"
    Remove-Item -Recurse -Force $PortableDir
}
New-Item -ItemType Directory -Force -Path $PortableDir | Out-Null

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("standterm-python-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

try {
    $ZipName = "python-$PythonVersion-embed-amd64.zip"
    $ZipUrl = "https://www.python.org/ftp/python/$PythonVersion/$ZipName"
    $ZipPath = Join-Path $TempDir $ZipName
    $GetPipPath = Join-Path $TempDir "get-pip.py"

    Write-Host "[*] Downloading embedded Python $PythonVersion..."
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath

    Write-Host "[*] Extracting embedded Python..."
    Expand-Archive -Path $ZipPath -DestinationPath $PortableDir -Force

    Enable-EmbeddedSite $PortableDir
    Ensure-Pip $PythonExe

    Set-Content -Path $StampFile -Value $PythonVersion -Encoding ASCII
    if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
        Set-Content -Path $OutputPath -Value $PythonExe -Encoding ASCII
    }
    Write-Output $PythonExe
} finally {
    Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue
}
