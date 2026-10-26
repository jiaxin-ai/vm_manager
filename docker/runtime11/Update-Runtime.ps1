param([Parameter(Mandatory=$true)][string]$Python,
      [string]$Share = '\\host.lan\Runtime')
$ErrorActionPreference = 'Stop'
$root = 'C:\OEM'
$meta = 'C:\ProgramData\VManagerRuntime'
$locked = $false
$transcribing = $false
$mutex = New-Object Threading.Mutex($false, 'Global\VManagerRuntimeUpdate')
function Log([string]$Message) { Write-Host "$(Get-Date -Format o) $Message" }
function Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$Exe exited with $LASTEXITCODE" }
}
function Mirror-Copy([string]$Source, [string]$Destination) {
    # /E deliberately preserves local recordings, screenshots and other non-package data.
    & robocopy $Source $Destination /E /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Host
    if ($LASTEXITCODE -ge 8) { throw "Copy failed: $Source -> $Destination ($LASTEXITCODE)" }
}
try {
    $locked = $mutex.WaitOne(0)
    if (-not $locked) { throw 'Another runtime update is already running' }
    New-Item -ItemType Directory -Force $meta | Out-Null
    Start-Transcript -Path "$meta\update.log" -Append | Out-Null
    $transcribing = $true
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Agent Python not found: $Python" }
    if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 5000 is already listening. Stop the existing Agent before updating; no process was killed.'
    }
    $identity = & $Python -c 'import sys; print(sys.executable)' 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot run Agent Python: $identity" }
    Log "Agent Python: $identity"
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-Path "$Share\manifest.json") { break }
        Start-Sleep -Seconds 2
    }
    $raw = Get-Content "$Share\manifest.json" -Raw
    $manifest = $raw | ConvertFrom-Json
    if ($manifest.schema -ne 1) { throw 'Unsupported runtime manifest' }
    $statePath = "$meta\state.json"
    $old = $null
    if (Test-Path $statePath) { $old = Get-Content $statePath -Raw | ConvertFrom-Json }
    $dirty = Test-Path "$meta\pending.json"
    $changes = @()
    foreach ($name in @('server', 'trajrec-bundle')) {
        $spec = $manifest.components.$name
        if (-not $spec.hash -or -not $spec.files) { throw "Invalid manifest: $name" }
        if ($dirty -or -not $old -or $old.components.$name.hash -ne $spec.hash -or -not (Test-Path "$root\$name")) { $changes += $name }
    }
    $req = @($manifest.components.server.files | Where-Object { $_.path -eq 'requirements.txt' })
    if ($req.Count -ne 1) { throw 'requirements.txt missing from manifest' }
    $installDeps = $dirty -or -not $old -or $old.requirements -ne $req[0].sha256 -or $old.python -ne "$identity"
    $installRecorder = $dirty -or -not $old -or $old.components.'trajrec-bundle'.hash -ne $manifest.components.'trajrec-bundle'.hash -or $old.python -ne "$identity"
    if ($changes.Count -eq 0 -and -not $installDeps -and -not $installRecorder) {
        Log 'Package hashes unchanged; synchronization and installation skipped.'
        exit 0
    }
    $stamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')
    $stage = "$root\.runtime-stage-$stamp"
    New-Item -ItemType Directory -Force $stage | Out-Null
    foreach ($name in $changes) {
        $dest = "$stage\$name"
        New-Item -ItemType Directory -Force $dest | Out-Null
        if (Test-Path "$root\$name") { Mirror-Copy "$root\$name" $dest }
        $spec = $manifest.components.$name
        $newPaths = @{}
        foreach ($file in $spec.files) {
            if ($file.path -match '(^/|\\|:|(^|/)\.\.(/|$))') { throw "Unsafe manifest path: $($file.path)" }
            $newPaths[$file.path] = $true
            $target = Join-Path $dest $file.path
            New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
            Copy-Item -LiteralPath (Join-Path "$Share\$name" $file.path) -Destination $target -Force
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $file.sha256) { throw "Hash mismatch: $name/$($file.path)" }
        }
        if ($old) {
            foreach ($file in $old.components.$name.files) {
                if (-not $newPaths.ContainsKey($file.path)) {
                    $obsolete = Join-Path $dest $file.path
                    if (Test-Path -LiteralPath $obsolete -PathType Leaf) { Remove-Item -LiteralPath $obsolete -Force }
                }
            }
        }
        Log "Staged and verified $name"
    }
    if ((Get-Content "$Share\manifest.json" -Raw) -ne $raw) { throw 'Share changed during synchronization; retry on next startup' }
    # Durable failure marker: never treat an interrupted/failed installation as a successful release.
    $raw | Set-Content "$meta\pending.json" -Encoding UTF8
    foreach ($name in $changes) {
        if (Test-Path "$root\$name") { Move-Item "$root\$name" "$root\$name.before-$stamp" }
        Move-Item "$stage\$name" "$root\$name"
    }
    if ($installDeps) {
        Log 'Installing changed requirements with the Agent Python'
        Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', "$root\server\requirements.txt")
    }
    if ($installRecorder) {
        Log 'Installing changed Recorder bundle'
        # The existing installer copies a routes module into AgentDir. Preserve the supplied
        # Agent source exactly; do not use -PatchAgent, even if future bundle routes differ.
        $route = "$root\server\trajrec_routes.py"
        $routeBackup = "$meta\routes-$stamp.bak"
        $hadRoute = Test-Path $route
        if ($hadRoute) { Copy-Item $route $routeBackup }
        try {
            Checked "$PSHOME\powershell.exe" @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$root\trajrec-bundle\recorder\integration\harnessstudio\install_recorder.ps1", '-Wheels', "$root\trajrec-bundle\wheels", '-Python', $Python)
        } finally {
            if ($hadRoute) { Copy-Item $routeBackup $route -Force }
            elseif (Test-Path $route) { Remove-Item $route -Force }
        }
    }
    $state = @{ components = $manifest.components; requirements = $req[0].sha256; python = "$identity" }
    $state | ConvertTo-Json -Depth 10 | Set-Content "$statePath.new" -Encoding UTF8
    Move-Item "$statePath.new" $statePath -Force
    Remove-Item "$meta\pending.json" -Force
    Log 'Runtime ready; returning to the original WAA startup.'
    exit 0
} catch {
    Write-Host "$(Get-Date -Format o) FAILED: $($_ | Out-String)"
    Write-Host 'Agent startup blocked. Fix the error and retry the original WAA startup; see C:\ProgramData\VManagerRuntime\update.log.'
    exit 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
