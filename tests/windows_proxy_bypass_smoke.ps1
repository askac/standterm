param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $Root "scripts\windows_proxy_bypass.ps1"
$TestKey = "HKCU:\Software\StandTerm\ProxyBypassSmoke-" + [Guid]::NewGuid().ToString("N")
$SessionId = "proxy-smoke-" + [Guid]::NewGuid().ToString("N")
$StateFile = ""
$OriginalOverride = "localhost;<local>"

try {
    New-Item -ItemType Directory -Force -Path $TestKey | Out-Null
    New-ItemProperty -Path $TestKey -Name "ProxyEnable" -PropertyType DWord -Value 1 | Out-Null
    New-ItemProperty -Path $TestKey -Name "ProxyOverride" -PropertyType String -Value $OriginalOverride | Out-Null

    $inspect = & $Helper `
        -Action Inspect `
        -HostAddress "172.20.1.2" `
        -InternetSettingsPath $TestKey | ConvertFrom-Json
    if ($inspect.status -ne "bypass_needed") {
        throw "Expected bypass_needed, got $($inspect.status)."
    }

    $apply = & $Helper `
        -Action Apply `
        -HostAddress "172.20.1.2" `
        -SessionId $SessionId `
        -InternetSettingsPath $TestKey | ConvertFrom-Json
    if ($apply.status -ne "applied") {
        throw "Expected applied, got $($apply.status)."
    }
    $StateFile = $apply.state_file
    $appliedOverride = (Get-ItemProperty -Path $TestKey -Name "ProxyOverride").ProxyOverride
    if ($appliedOverride -ne "$OriginalOverride;172.20.1.2") {
        throw "Unexpected applied override."
    }

    $restore = & $Helper `
        -Action Restore `
        -SessionId $SessionId `
        -StateFile $StateFile `
        -InternetSettingsPath $TestKey | ConvertFrom-Json
    if ($restore.status -ne "restored") {
        throw "Expected restored, got $($restore.status)."
    }
    $StateFile = ""
    $restoredOverride = (Get-ItemProperty -Path $TestKey -Name "ProxyOverride").ProxyOverride
    if ($restoredOverride -ne $OriginalOverride) {
        throw "ProxyOverride was not restored."
    }

    $externalSessionId = $SessionId + "-external"
    $externalApply = & $Helper `
        -Action Apply `
        -HostAddress "172.20.1.2" `
        -SessionId $externalSessionId `
        -InternetSettingsPath $TestKey | ConvertFrom-Json
    if ($externalApply.status -ne "applied") {
        throw "Expected second apply, got $($externalApply.status)."
    }
    $StateFile = $externalApply.state_file
    Set-ItemProperty -Path $TestKey -Name "ProxyOverride" -Value "external-change"
    $externalRestore = & $Helper `
        -Action Restore `
        -SessionId $externalSessionId `
        -StateFile $StateFile `
        -InternetSettingsPath $TestKey | ConvertFrom-Json
    if ($externalRestore.status -ne "changed_externally") {
        throw "Expected changed_externally, got $($externalRestore.status)."
    }
    $StateFile = ""
    if ((Get-ItemProperty -Path $TestKey -Name "ProxyOverride").ProxyOverride -ne "external-change") {
        throw "External ProxyOverride change was overwritten."
    }

    Write-Host "windows_proxy_bypass_smoke: ok"
} finally {
    if (-not [string]::IsNullOrWhiteSpace($StateFile) -and (Test-Path -LiteralPath $StateFile)) {
        Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $TestKey -Recurse -Force -ErrorAction SilentlyContinue
}
