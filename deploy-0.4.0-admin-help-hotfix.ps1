$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
$ConfigLoader = Join-Path $ProjectDir "deploy\load-deploy-config.ps1"
. $ConfigLoader
$DeployTarget = Get-BridgeDeployTarget -ProjectDir $ProjectDir
$RemoteTarget = "$($DeployTarget.SshUser)@$($DeployTarget.SshHostName)"
$SshPort = $DeployTarget.SshPort
$Package = Join-Path $ProjectDir "dist\matrix-serverchat-backend-0.4.0-admin-help-hotfix.tar.gz"
$Installer = Join-Path $ProjectDir "deploy\remote-install-admin-help-hotfix.sh"

foreach ($File in @($Package, $Installer)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Missing hotfix file: $File"
    }
}

Write-Host "Uploading the bot backend hotfix. Enter the SSH password when prompted." -ForegroundColor Cyan
& scp -P $SshPort $Package $Installer "${RemoteTarget}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    throw "SCP upload failed with exit code $LASTEXITCODE"
}

Write-Host "Installing the hotfix. SSH may prompt for the password again." -ForegroundColor Cyan
& ssh -p $SshPort $RemoteTarget "bash /tmp/remote-install-admin-help-hotfix.sh"
if ($LASTEXITCODE -ne 0) {
    throw "Remote hotfix installation failed with exit code $LASTEXITCODE"
}

Write-Host "Hotfix completed. Send #help in the Matrix room to verify it." -ForegroundColor Green
