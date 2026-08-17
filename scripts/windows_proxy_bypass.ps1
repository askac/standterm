param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Inspect", "Apply", "Restore")]
    [string]$Action,

    [string]$HostAddress = "",

    [string]$SessionId = "",

    [string]$StateFile = "",

    [string]$InternetSettingsPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
)

$ErrorActionPreference = "Stop"
$DefaultInternetSettingsPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

function Write-Result {
    param(
        [string]$Status,
        [string]$ResultStateFile = ""
    )

    [ordered]@{
        status = $Status
        state_file = $ResultStateFile
    } | ConvertTo-Json -Compress
}

function Get-OptionalRegistryValue {
    param([string]$Name)

    $item = Get-ItemProperty -Path $InternetSettingsPath -ErrorAction Stop
    $property = $item.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return [pscustomobject]@{ Exists = $false; Value = $null }
    }
    return [pscustomobject]@{ Exists = $true; Value = $property.Value }
}

function Test-PrivateNonLoopbackAddress {
    param([string]$AddressText)

    $address = $null
    if (-not [Net.IPAddress]::TryParse($AddressText, [ref]$address)) {
        return $false
    }
    if ([Net.IPAddress]::IsLoopback($address)) {
        return $false
    }
    if ($address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        return $false
    }
    $bytes = $address.GetAddressBytes()
    return (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )
}

function Test-HostBypassed {
    param(
        [string]$ProxyOverride,
        [string]$AddressText
    )

    foreach ($entryValue in ($ProxyOverride -split ";")) {
        $entry = $entryValue.Trim()
        if ([string]::IsNullOrWhiteSpace($entry) -or $entry -eq "<local>") {
            continue
        }
        $pattern = [Management.Automation.WildcardPattern]::new(
            $entry,
            [Management.Automation.WildcardOptions]::IgnoreCase
        )
        if ($pattern.IsMatch($AddressText)) {
            return $true
        }
    }
    return $false
}

function Get-ProxyConfiguration {
    $enabledValue = Get-OptionalRegistryValue "ProxyEnable"
    $autoConfigValue = Get-OptionalRegistryValue "AutoConfigURL"
    $autoDetectValue = Get-OptionalRegistryValue "AutoDetect"
    $overrideValue = Get-OptionalRegistryValue "ProxyOverride"
    $manualEnabled = $enabledValue.Exists -and [int]$enabledValue.Value -eq 1
    $autoConfigured = $autoConfigValue.Exists -and -not [string]::IsNullOrWhiteSpace([string]$autoConfigValue.Value)
    $autoDetected = $autoDetectValue.Exists -and [int]$autoDetectValue.Value -eq 1
    return [pscustomobject]@{
        Configured = $manualEnabled -or $autoConfigured -or $autoDetected
        OverrideExists = $overrideValue.Exists
        Override = if ($overrideValue.Exists) { [string]$overrideValue.Value } else { "" }
    }
}

function Publish-InternetSettingsChange {
    if ($InternetSettingsPath -ne $DefaultInternetSettingsPath) {
        return
    }
    if (-not ("StandTerm.WinInetSettings" -as [type])) {
        Add-Type @"
using System;
using System.Runtime.InteropServices;
namespace StandTerm {
    public static class WinInetSettings {
        [DllImport("wininet.dll", SetLastError = true)]
        public static extern bool InternetSetOption(IntPtr internet, int option, IntPtr buffer, int bufferLength);
    }
}
"@
    }
    [StandTerm.WinInetSettings]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null
    [StandTerm.WinInetSettings]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null
}

if ($Action -eq "Restore") {
    if ([string]::IsNullOrWhiteSpace($StateFile) -or -not (Test-Path -LiteralPath $StateFile)) {
        Write-Result "state_missing"
        exit 0
    }
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    if ([string]$state.session_id -ne $SessionId) {
        Write-Result "session_mismatch" $StateFile
        exit 0
    }
    $current = Get-ProxyConfiguration
    if ($current.Override -eq [string]$state.applied_override) {
        if ([bool]$state.original_override_exists) {
            Set-ItemProperty -Path $InternetSettingsPath -Name "ProxyOverride" -Value ([string]$state.original_override)
        } else {
            Remove-ItemProperty -Path $InternetSettingsPath -Name "ProxyOverride" -ErrorAction SilentlyContinue
        }
        Publish-InternetSettingsChange
        Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
        Write-Result "restored"
        exit 0
    }
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    Write-Result "changed_externally"
    exit 0
}

if (-not (Test-PrivateNonLoopbackAddress $HostAddress)) {
    Write-Result "ignored_host"
    exit 0
}

$configuration = Get-ProxyConfiguration
if (-not $configuration.Configured) {
    Write-Result "proxy_disabled"
    exit 0
}
if (Test-HostBypassed $configuration.Override $HostAddress) {
    Write-Result "already_bypassed"
    exit 0
}
if ($Action -eq "Inspect") {
    Write-Result "bypass_needed"
    exit 0
}
if ([string]::IsNullOrWhiteSpace($SessionId)) {
    throw "SessionId is required for Apply."
}

$stateDirectory = Join-Path ([IO.Path]::GetTempPath()) "StandTerm"
New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
$safeSessionId = $SessionId -replace "[^A-Za-z0-9_.-]", "_"
$StateFile = Join-Path $stateDirectory "proxy-bypass-$safeSessionId.json"
$appliedOverride = if ([string]::IsNullOrWhiteSpace($configuration.Override)) {
    $HostAddress
} else {
    $configuration.Override.TrimEnd(";") + ";" + $HostAddress
}
$state = [ordered]@{
    session_id = $SessionId
    host_address = $HostAddress
    original_override_exists = $configuration.OverrideExists
    original_override = $configuration.Override
    applied_override = $appliedOverride
}
$state | ConvertTo-Json -Compress | Set-Content -LiteralPath $StateFile -Encoding UTF8
try {
    Set-ItemProperty -Path $InternetSettingsPath -Name "ProxyOverride" -Value $appliedOverride
    Publish-InternetSettingsChange
} catch {
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    throw
}
Write-Result "applied" $StateFile
