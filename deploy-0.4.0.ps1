param(
    [string]$MinecraftServerDir
)

$ErrorActionPreference = "Stop"
$ProjectDir = $PSScriptRoot
$ConfigLoader = Join-Path $ProjectDir "deploy\load-deploy-config.ps1"
. $ConfigLoader
$DeployTarget = Get-BridgeDeployTarget -ProjectDir $ProjectDir
$RemoteTarget = "$($DeployTarget.SshUser)@$($DeployTarget.SshHostName)"
$SshPort = $DeployTarget.SshPort

if ([string]::IsNullOrWhiteSpace($MinecraftServerDir)) {
    $MinecraftServerDir = Read-Host "Remote Minecraft server directory (example: /opt/minecraft)"
}
if (-not $MinecraftServerDir.StartsWith("/")) {
    throw "MinecraftServerDir must be an absolute Linux path."
}

$BackendPackage = Join-Path $ProjectDir "dist\matrix-serverchat-backend-0.4.0.tar.gz"
$PluginJar = Join-Path $ProjectDir "build\libs\matrix-chat-bridge-0.4.0.jar"
$RemoteInstaller = Join-Path $ProjectDir "deploy\remote-install-0.4.0.sh"

foreach ($File in @($BackendPackage, $PluginJar, $RemoteInstaller)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Missing deployment file: $File"
    }
}

Write-Host "Uploading 0.4.0 files. Enter the root SSH password when prompted." -ForegroundColor Cyan
& scp -P $SshPort $BackendPackage $PluginJar $RemoteInstaller "${RemoteTarget}:/tmp/"
if ($LASTEXITCODE -ne 0) {
    throw "SCP upload failed with exit code $LASTEXITCODE"
}

$DirectoryBase64 = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($MinecraftServerDir)
)
Write-Host "Running the remote installer. SSH may prompt for the password again." -ForegroundColor Cyan
& ssh -p $SshPort $RemoteTarget "bash /tmp/remote-install-0.4.0.sh $DirectoryBase64"
if ($LASTEXITCODE -ne 0) {
    throw "Remote installation failed with exit code $LASTEXITCODE"
}

Write-Host "Deployment completed. Follow the remote output to restart Minecraft and pair the token." -ForegroundColor Green
