param([string]$Startup = 'C:\OEM\on-logon.ps1')
$ErrorActionPreference = 'Stop'
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run once in an elevated Windows PowerShell.' }
    $text = [IO.File]::ReadAllText($Startup)
    if ($text.Contains('# VManager Runtime 1.1 hook')) { Write-Host 'Hook already installed.'; exit 0 }
    $launch = '(?m)^python\s+\$pythonScriptFile\s+--port\s+\$pythonServerPort\s*\r?$'
    if ([regex]::Matches($text, $launch).Count -ne 1) { throw 'Unknown WAA launch command. No change made; integrate the update call manually before the actual Agent launch.' }
    if ($text -notmatch '(?i)\$scriptFolder\s*=\s*["'']C:\\oem["'']') { throw 'Startup does not target C:\OEM. No change made; inspect the actual WAA startup first.' }
    $backup = "$Startup.before-runtime11-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N')).bak"
    Copy-Item -LiteralPath $Startup -Destination $backup
    $update = Join-Path $PSScriptRoot 'Update-Runtime.ps1'
    if (-not (Test-Path $update)) { throw "Missing $update" }
    Copy-Item $update 'C:\OEM\Update-Runtime.ps1' -Force
    $prefix = @'
# VManager Runtime 1.1 hook
# Resolve Python in the SAME account/PATH as the original WAA task, and use it below.
$vmanagerPython = (& python -c "import sys; print(sys.executable)")
if ($LASTEXITCODE -ne 0 -or -not $vmanagerPython) { throw 'Cannot resolve the original WAA Python' }
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File 'C:\OEM\Update-Runtime.ps1' -Python "$vmanagerPython"
if ($LASTEXITCODE -ne 0) { throw 'Runtime update failed; Agent startup blocked. See C:\ProgramData\VManagerRuntime\update.log' }

'@
    $updated = $prefix + "`r`n" + [regex]::Replace($text, $launch, '& $vmanagerPython $pythonScriptFile --port $pythonServerPort')
    [IO.File]::WriteAllText("$Startup.new", $updated, (New-Object Text.UTF8Encoding($true)))
    Move-Item "$Startup.new" $Startup -Force
    Write-Host "Installed hook in $Startup; existing WAA task and main.py invocation retained."
    Write-Host "Backup: $backup"
    Write-Host "Restore: Copy-Item -LiteralPath '$backup' -Destination '$Startup' -Force"
} catch {
    Write-Error $_
    exit 1
}
