$ErrorActionPreference = "Stop"

$ProjectDir = $PSScriptRoot
. (Join-Path $ProjectDir "deploy\load-deploy-config.ps1")
$Target = Get-BridgeDeployTarget -ProjectDir $ProjectDir
$Remote = "$($Target.SshUser)@$($Target.SshHostName)"
$BackendPackage = Join-Path $ProjectDir "dist\matrix-serverchat-backend-0.4.9.tar.gz"
$Installer = Join-Path $ProjectDir "install-0.4.9.sh"

foreach ($File in @($BackendPackage, $Installer)) {
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "Missing release file: $File"
    }
}

Write-Host "Uploading MatrixChatBridge 0.4.9 backend to $Remote ..." -ForegroundColor Cyan
& scp -P $Target.SshPort $BackendPackage $Installer "${Remote}:/tmp/"
if ($LASTEXITCODE -ne 0) { throw "SCP upload failed with exit code $LASTEXITCODE" }

Write-Host "Installing the backend. SSH may ask for the password again." -ForegroundColor Cyan
& ssh -p $Target.SshPort $Remote `
    "bash /tmp/install-0.4.9.sh /tmp/matrix-serverchat-backend-0.4.9.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "Remote installation failed with exit code $LASTEXITCODE" }

Write-Host "Backend 0.4.9 installed. Update the plugin JAR on every Minecraft server next." -ForegroundColor Green
