function Get-BridgeDeployTarget {
    param([Parameter(Mandatory = $true)][string]$ProjectDir)

    $ConfigPath = Join-Path $ProjectDir "deploy.local.psd1"
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Missing deploy.local.psd1. Copy deploy.local.example.psd1 and fill in your SSH target."
    }
    $Config = Import-PowerShellDataFile -LiteralPath $ConfigPath
    if ([string]::IsNullOrWhiteSpace([string]$Config.SshHostName)) {
        throw "SshHostName is required in deploy.local.psd1."
    }
    if ([int]$Config.SshPort -lt 1 -or [int]$Config.SshPort -gt 65535) {
        throw "SshPort must be between 1 and 65535."
    }
    if ([string]::IsNullOrWhiteSpace([string]$Config.SshUser)) {
        throw "SshUser is required in deploy.local.psd1."
    }
    return [pscustomobject]@{
        SshHostName = [string]$Config.SshHostName
        SshPort = [int]$Config.SshPort
        SshUser = [string]$Config.SshUser
    }
}
