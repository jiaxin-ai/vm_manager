param([string]$Startup = 'C:\OEM\on-logon.ps1',
      [string]$Python,
      [string]$TaskName = 'WindowsArena_OnLogon')
$ErrorActionPreference = 'Stop'
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run once in an elevated Windows PowerShell.' }
    $text = [IO.File]::ReadAllText($Startup)
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($task.Actions.Count -ne 1 -or $task.Actions[0].Arguments -notlike "*$Startup*") { throw 'Unknown task action; no changes made.' }
    if (-not $Python) {
        $statePath = 'C:\ProgramData\VManagerRuntime\state.json'
        if (Test-Path $statePath) { $Python = (Get-Content $statePath -Raw | ConvertFrom-Json).python }
        if (-not $Python) { throw 'Specify -Python with the existing Agent Python executable; do not guess from PATH.' }
    }
    $Python = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
    if ($Python.Contains("'")) { throw 'Unsupported quote in Python path.' }
    & $Python -S -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0) { throw 'Selected Agent Python cannot run.' }
    $update = Join-Path $PSScriptRoot 'Update-Runtime.ps1'
    if (-not (Test-Path $update)) { throw "Missing $update" }
    $launch = '(?m)^python\s+\$pythonScriptFile\s+--port\s+\$pythonServerPort\s*\r?$'
    if ($text.Contains('# VManager Runtime 1.1 hook')) {
        $start = $text.IndexOf('$scriptFolder')
        if ($start -lt 0 -or $text -notmatch '& \$vmanagerPython \$pythonScriptFile --port \$pythonServerPort') { throw 'Unknown existing hook; no changes made.' }
        $body = $text.Substring($start)
    } else {
        if ([regex]::Matches($text, $launch).Count -ne 1) { throw 'Unknown WAA launch command; no changes made.' }
        $body = [regex]::Replace($text, $launch, '& $vmanagerPython $pythonScriptFile --port $pythonServerPort')
    }
    if ($body -notmatch '(?i)\$scriptFolder\s*=\s*["'']C:\\oem["'']') { throw 'Startup does not target C:\OEM; no changes made.' }
    $stamp = (Get-Date -Format yyyyMMdd-HHmmss) + '-' + [guid]::NewGuid().ToString('N')
    Copy-Item $Startup "$Startup.before-runtime11-$stamp.bak"
    Export-ScheduledTask -TaskName $TaskName | Set-Content "$Startup.task-before-$stamp.xml" -Encoding UTF8
    if (Test-Path 'C:\OEM\Update-Runtime.ps1') { Copy-Item 'C:\OEM\Update-Runtime.ps1' "C:\OEM\Update-Runtime.ps1.before-$stamp.bak" }
    Copy-Item $update 'C:\OEM\Update-Runtime.ps1' -Force
    $prefix = @'
# VManager Runtime 1.1 hook
# Interpreter is fixed at image preparation time; interactive profiles are not required.
$vmanagerPython = '__PYTHON__'
if (-not (Test-Path -LiteralPath $vmanagerPython -PathType Leaf)) { throw 'Configured Agent Python is missing' }
$env:PYTHONUTF8 = '1'
Write-Host "$(Get-Date -Format o) Runtime update starting"
& "$PSHOME\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File 'C:\OEM\Update-Runtime.ps1' -Python $vmanagerPython
if ($LASTEXITCODE -ne 0) { throw 'Runtime update failed; see C:\ProgramData\VManagerRuntime\update.log' }
Write-Host "$(Get-Date -Format o) Runtime update complete; starting Agent"

'@
    $prefix = $prefix.Replace('__PYTHON__', $Python)
    [IO.File]::WriteAllText($Startup, $prefix + "`r`n" + $body, (New-Object Text.UTF8Encoding($true)))
    if ($task.Actions[0].Arguments -notmatch '(?i)(^|\s)-NoProfile(\s|$)') {
        $task.Actions[0].Arguments = '-NoProfile ' + $task.Actions[0].Arguments
        Set-ScheduledTask -InputObject $task | Out-Null
    }
    Write-Host "Installed; Python: $Python; backups: $Startup.*-$stamp.*"
} catch {
    Write-Error $_
    exit 1
}
