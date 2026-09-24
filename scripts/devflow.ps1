[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'status', 'logs', 'stop', 'backup', 'help')]
    [string]$Command = 'help',
    [string]$Repository,
    [ValidateRange(1024, 65535)]
    [int]$Port = 3000,
    [switch]$Demo,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PreviousProjectPath = $env:DEVFLOW_PROJECT_PATH
$PreviousPort = $env:DEVFLOW_PORT

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE)." }
}
function Read-Git([string[]]$GitArgs) {
    $result = & git --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=NUL -C $Repository @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Git check failed: use an ordinary repository root with a committed HEAD.' }
    return $result
}

try {
    if ($Command -eq 'help') {
        Write-Host 'Usage: ./scripts/devflow.ps1 start -Repository C:/projects/example [-Port 3000]'
        Write-Host '       ./scripts/devflow.ps1 start -Demo'
        Write-Host '       ./scripts/devflow.ps1 status|logs|stop [-Demo]'
        Write-Host '       ./scripts/devflow.ps1 backup -OutputDirectory C:/backups/devflow [-Demo]'
        exit 0
    }
    $env:DEVFLOW_PORT = [string]$Port
    if ($Demo -and $Repository) { throw 'Choose -Demo OR -Repository, not both.' }
    if ($Command -ne 'start' -and $Repository) { throw '-Repository is only used with start.' }
    if ($Command -ne 'backup' -and $OutputDirectory) { throw '-OutputDirectory is only used with backup.' }
    if (!(Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker with the Compose plugin; on Windows enable Linux containers.' }
    $version = (Invoke-Docker compose version --short | Out-String).Trim().TrimStart('v').Split('-')[0]
    if ([version]$version -lt [version]'2.24.4') { throw 'Docker Compose 2.24.4 or newer is required.' }
    $os = (Invoke-Docker info --format '{{.OSType}}' | Out-String).Trim()
    if ($os -ne 'linux') { throw 'Start a local Docker daemon using Linux containers.' }
    $project = if ($Demo) { 'devflow-demo' } else { 'devflow' }
    $compose = @('compose', '--project-name', $project, '--project-directory', $Root, '-f', (Join-Path $Root 'compose.yaml'))
    if ($Demo) { $compose += @('-f', (Join-Path $Root 'compose.demo.yaml')) }

    if ($Command -eq 'start' -and !$Demo) {
        if (!$Repository) { throw 'Real mode requires start -Repository <clean committed Git repository>.' }
        if (!(Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git and make git available on PATH.' }
        $Repository = (Resolve-Path -LiteralPath $Repository).Path
        if (!(Test-Path -LiteralPath (Join-Path $Repository '.git') -PathType Container)) {
            throw 'Use a repository root containing a .git directory; linked worktrees and bare repositories are unsupported.'
        }
        # Do not inspect source status/index: clean filters can execute even with hooks/fsmonitor disabled.
        # The isolated import preview, not this startup check, decides repository cleanliness.
        $null = Read-Git @('rev-parse', '--verify', 'HEAD^{commit}')
        $env:DEVFLOW_PROJECT_PATH = $Repository.Replace('\', '/')
        Write-Host 'Git diagnostic: repository and committed HEAD found. Cleanliness is NOT checked at startup; the safe import preview must accept the source before import.'
    }
    # Lifecycle commands do not need the source checkout to still exist.
    if ($Command -ne 'start' -or $Demo) { $env:DEVFLOW_PROJECT_PATH = $Root.Replace('\', '/') }
    Invoke-Docker @compose config --quiet
    switch ($Command) {
        'start' {
            $running = @(Invoke-Docker @compose ps --status running --quiet frontend)
            if (!$running.Count) {
                $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('127.0.0.1'), $Port)
                try { $listener.Start() }
                catch { throw "Port 127.0.0.1:$Port is occupied. Choose another -Port or stop the other service first." }
                finally { $listener.Stop() }
            }
            Write-Host "Docker/Compose diagnostics passed. Only frontend port 127.0.0.1:$Port is published; backend 8000 stays internal."
            Invoke-Docker @compose up --build --detach --wait --wait-timeout 180
            Write-Host "Open http://127.0.0.1:$Port (use this exact origin)."
        }
        'status' { Invoke-Docker @compose ps --all }
        'logs' { Invoke-Docker @compose logs --tail 200 --follow }
        'stop' {
            Invoke-Docker @compose stop --timeout 60
            Write-Host 'Stopped; all volumes retained. No network access or image pull is needed.'
        }
        'backup' {
            if (!$OutputDirectory) { throw 'backup requires -OutputDirectory <directory outside the imported repository>.' }
            $null = New-Item -ItemType Directory -Force -Path $OutputDirectory
            $destination = (Resolve-Path -LiteralPath $OutputDirectory).Path
            $ids = @(Invoke-Docker @compose ps --all --quiet backend)
            if ($ids.Count -ne 1) { throw 'Backup requires an existing backend container. Use stop, not down, before backup.' }
            $container = (Invoke-Docker inspect $ids[0] | Out-String | ConvertFrom-Json)[0]
            $volume = @($container.Mounts | Where-Object { $_.Type -eq 'volume' -and $_.Destination -eq '/var/lib/devflow' })
            if ($volume.Count -ne 1) { throw 'Cannot identify the database/workspace volume.' }
            $image = $container.Image
            Invoke-Docker @compose stop --timeout 60
            $writers = @(Invoke-Docker ps --quiet --filter "volume=$($volume[0].Name)")
            if ($writers.Count) { throw 'Another running container uses the data volume. Stop it before retrying backup.' }
            Write-Warning 'Provider secrets are EXCLUDED. Re-enter credentials after restore. Archive contains project code, prompts and database records; protect it. Keep all writers stopped until backup completes.'
            $code = @"
import datetime, os, pathlib, tarfile
name = 'devflow-data-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz'
path = pathlib.Path('/backup') / name
with path.open('xb') as raw:
    try:
        os.fchmod(raw.fileno(), 0o600)
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            archive.add('/data', arcname='data', recursive=True)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
print('Backup complete: ' + name)
"@
            Invoke-Docker run --rm --pull=never --network none --read-only --user 0:0 --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --mount "type=volume,source=$($volume[0].Name),target=/data,readonly" --mount "type=bind,source=$destination,target=/backup" --entrypoint python $image -c $code
            Write-Host 'Stack remains stopped. Database and workspaces were archived together; no credential or control volume was mounted.'
        }
    }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    $env:DEVFLOW_PROJECT_PATH = $PreviousProjectPath
    $env:DEVFLOW_PORT = $PreviousPort
}
