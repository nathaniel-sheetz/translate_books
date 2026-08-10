<#
.SYNOPSIS
    Control the TranslateBooksReader service task.

.DESCRIPTION
    Under the scheduled task there is no auto-reload: after a code edit the
    service must be bounced before the change is live. `restart` is that, in
    one command. `dev` stops the service and runs the reloading dev server in
    the foreground instead.

    `install` registers the task from the definition in this script, so the
    service is reproducible instead of hand-made in the Task Scheduler GUI.
    `status` audits the live task against that same definition and names
    anything that has drifted.

.EXAMPLE
    scripts\reader.ps1 restart
    scripts\reader.ps1 status
    scripts\reader.ps1 log
    scripts\reader.ps1 install
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'dev', 'log', 'install', 'spec')]
    [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'TranslateBooksReader'
$TaskDescription = 'Always-on reader for translate_books. Managed by scripts/reader.ps1.'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Port = 5000
$HealthUrl = "http://127.0.0.1:$Port/healthz"
$LogDir = Join-Path $RepoRoot 'logs'
$LogPath = Join-Path $LogDir 'web_ui.log'
$ServePath = Join-Path $RepoRoot 'scripts\serve.py'

function Get-ReaderListener {
    <#
        Identify the server by who holds the port, not by command line: the
        task runs in session 0, where an unelevated Win32_Process query returns
        an empty CommandLine and every match silently fails.

        Bind address tells the two entry points apart - serve.py binds
        127.0.0.1, the dev server binds 0.0.0.0.
    #>
    try {
        $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        return $null
    }
    foreach ($c in $conns) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($null -eq $proc) { continue }
        if ($c.LocalAddress -eq '0.0.0.0') { $kind = 'dev server (0.0.0.0)' }
        else { $kind = 'service (loopback)' }
        [PSCustomObject]@{
            ProcessId = $c.OwningProcess
            Name      = $proc.ProcessName
            Address   = $c.LocalAddress
            Kind      = $kind
        }
    }
}

function Test-ReaderHealth {
    try {
        return Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    } catch {
        return $null
    }
}

function Wait-ReaderStopped {
    param([int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-ReaderListener)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-ReaderHealthy {
    param([int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-ReaderHealth) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-PythonPath {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "python is not on PATH. A task action needs an absolute interpreter path."
    }
    # The Store alias is a stub that only works in an interactive session; as a
    # service it exits immediately and explains nothing.
    if ($cmd.Source -like '*\WindowsApps\*') {
        throw "python resolves to the Microsoft Store alias ($($cmd.Source)). Install a real Python first."
    }
    return $cmd.Source
}

function Get-DesiredTask {
    <#
        The single description of what the task should be: `install` writes from
        it and `status` audits against it, so the two cannot drift apart.

        Transcribed from the task as originally registered by hand, with one
        deliberate change. That task carried DisallowStartIfOnBatteries=true, so
        a reboot on battery left the reader down - the exact case Step 3 of
        docs/design/tailscale.md promises to survive.
    #>
    $action = New-ScheduledTaskAction `
        -Execute (Get-PythonPath) `
        -Argument "`"$ServePath`"" `
        -WorkingDirectory $RepoRoot

    $trigger = New-ScheduledTaskTrigger -AtStartup
    # A minute of slack so the network stack and Tailscale are up first.
    $trigger.Delay = 'PT1M'

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 60 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    # No cmdlet parameter exists for this one, and it matters: the service
    # should not be hard-killed in the middle of writing a translation.
    $settings.AllowHardTerminate = $false

    return [PSCustomObject]@{
        Action   = $action
        Trigger  = $trigger
        Settings = $settings
    }
}

function Test-TaskDrift {
    <#
        Return one human-readable string per mismatch between the registered
        task and Get-DesiredTask; nothing at all when they agree.

        Deliberately tolerant: `status` is the command you run when things are
        already broken, so a missing property has to produce a finding rather
        than a traceback.
    #>
    param($Task)

    $findings = @()
    $desired = Get-DesiredTask

    $action = @($Task.Actions)[0]
    if ($null -eq $action) {
        return @('task has no action')
    }

    if ([string]::IsNullOrWhiteSpace($action.WorkingDirectory)) {
        $findings += 'WorkingDirectory not set - cwd-relative paths resolve under system32'
    } elseif ($action.WorkingDirectory.TrimEnd('\') -ne $RepoRoot.TrimEnd('\')) {
        $findings += "WorkingDirectory is '$($action.WorkingDirectory)', expected '$RepoRoot'"
    }

    # Catches a task left pointing at an old clone after the repo moved.
    if ($action.Arguments -notlike "*$ServePath*") {
        $findings += "Arguments do not point at $ServePath (found: $($action.Arguments))"
    }

    $s = $Task.Settings
    if ($s.DisallowStartIfOnBatteries) {
        $findings += 'DisallowStartIfOnBatteries=true - will not start after a reboot on battery'
    }
    if ($s.StopIfGoingOnBatteries) {
        $findings += 'StopIfGoingOnBatteries=true - the reader dies when you unplug'
    }
    if ("$($s.ExecutionTimeLimit)" -ne "$($desired.Settings.ExecutionTimeLimit)") {
        $findings += "ExecutionTimeLimit=$($s.ExecutionTimeLimit), expected $($desired.Settings.ExecutionTimeLimit) (no limit)"
    }
    if ("$($s.MultipleInstances)" -ne "$($desired.Settings.MultipleInstances)") {
        $findings += "MultipleInstances=$($s.MultipleInstances), expected $($desired.Settings.MultipleInstances)"
    }
    if ($s.RestartCount -ne $desired.Settings.RestartCount) {
        $findings += "RestartCount=$($s.RestartCount), expected $($desired.Settings.RestartCount)"
    }
    if ("$($s.RestartInterval)" -ne "$($desired.Settings.RestartInterval)") {
        $findings += "RestartInterval=$($s.RestartInterval), expected $($desired.Settings.RestartInterval)"
    }

    $boot = @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' })
    if ($boot.Count -eq 0) {
        $findings += 'no at-startup trigger - the reader will not come back after a reboot'
    }

    return $findings
}

function Install-Reader {
    $user = "$env:USERDOMAIN\$env:USERNAME"

    if (-not (Test-Path $ServePath)) {
        throw "Not found: $ServePath"
    }
    $python = Get-PythonPath
    # Without waitress the task registers cleanly and then dies on every single
    # start, leaving one line in a log nobody is watching. Far cheaper here.
    & $python -c "import waitress" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "$python cannot import waitress. Run: pip install -r requirements.txt"
    }

    Write-Host "Python:    $python"
    Write-Host "Serve:     $ServePath"
    Write-Host "Start in:  $RepoRoot"
    Write-Host "Run as:    $user (run whether logged on or not)"

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $drift = @(Test-TaskDrift -Task $existing)
        if ($drift.Count -gt 0) {
            Write-Host "Fixing:" -ForegroundColor Cyan
            foreach ($d in $drift) { Write-Host "  - $d" -ForegroundColor Cyan }
        }

        if (-not (Test-Path $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir | Out-Null
        }
        $backup = Join-Path $LogDir "task-backup-$TaskName-$(Get-Date -Format 'yyyyMMdd-HHmmss').xml"
        Export-ScheduledTask -TaskName $TaskName | Set-Content -Path $backup -Encoding utf8
        Write-Host "Backup:    $backup"
        Write-Host "  restore: Register-ScheduledTask -Xml (Get-Content '$backup' -Raw) -TaskName $TaskName -User $user -Password <pw> -Force"
    }

    if (Get-ReaderListener) { Stop-Reader }

    $desired = Get-DesiredTask
    # "Run whether user is logged on or not" means a stored password: the
    # packages this app needs live in per-user site-packages, so the task has to
    # carry the user's own identity (see docs/design/tailscale.md, Step 3).
    $secure = Read-Host "Windows password for $user" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Description $TaskDescription `
            -Action $desired.Action `
            -Trigger $desired.Trigger `
            -Settings $desired.Settings `
            -User $user `
            -Password $plain `
            -RunLevel Limited `
            -Force | Out-Null
    } catch {
        # The raw COM failure here is unreadable, and both likely causes are
        # things the caller can act on.
        Write-Host "Registration failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Usually one of: wrong password, or '$user' lacks the 'Log on as a batch job' right" -ForegroundColor Yellow
        Write-Host "(secpol.msc -> Local Policies -> User Rights Assignment)" -ForegroundColor Yellow
        throw
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        Remove-Variable plain -ErrorAction SilentlyContinue
    }

    Write-Host "Registered $TaskName." -ForegroundColor Green
    Start-Reader
}

function Stop-Reader {
    schtasks /end /tn $TaskName
    if (Wait-ReaderStopped) {
        Write-Host "Reader stopped." -ForegroundColor Green
    } else {
        Write-Host "Process still running after 30s - check Task Scheduler." -ForegroundColor Red
    }
}

function Start-Reader {
    schtasks /run /tn $TaskName
    if (Wait-ReaderHealthy) {
        Write-Host "Reader is up." -ForegroundColor Green
    } else {
        Write-Host "Started, but no health response yet. Check $LogPath" -ForegroundColor Yellow
    }
}

switch ($Command) {
    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "Task:      '$TaskName' is not registered." -ForegroundColor Yellow
            Write-Host "Config:    register it with 'scripts\reader.ps1 install'" -ForegroundColor Yellow
        } else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host "Task:      $TaskName [$($task.State)]"
            Write-Host "Last run:  $($info.LastRunTime)  result 0x$('{0:X}' -f $info.LastTaskResult)"

            try {
                $drift = @(Test-TaskDrift -Task $task)
                if ($drift.Count -eq 0) {
                    Write-Host "Config:    ok" -ForegroundColor Green
                } else {
                    foreach ($d in $drift) {
                        Write-Host "Config:    $d" -ForegroundColor Yellow
                    }
                    Write-Host "Config:    fix with 'scripts\reader.ps1 install'" -ForegroundColor Yellow
                }
            } catch {
                Write-Host "Config:    could not audit - $($_.Exception.Message)" -ForegroundColor Red
            }
        }

        $listeners = @(Get-ReaderListener)
        if ($listeners.Count -eq 0) {
            Write-Host "Process:   nothing listening on port $Port" -ForegroundColor Yellow
        } else {
            foreach ($l in $listeners) {
                Write-Host "Process:   PID $($l.ProcessId) $($l.Name) - $($l.Kind)"
            }
        }

        $health = Test-ReaderHealth
        if ($null -eq $health) {
            Write-Host "Health:    no response from $HealthUrl" -ForegroundColor Red
        } else {
            Write-Host "Health:    ok, version $($health.version)" -ForegroundColor Green
        }
    }

    'start' { Start-Reader }

    'stop' { Stop-Reader }

    'restart' {
        Stop-Reader
        Start-Reader
    }

    'dev' {
        if (Get-ReaderListener) {
            Write-Host "Stopping the service first (port $Port)..." -ForegroundColor Cyan
            Stop-Reader
        }
        Write-Host "Dev server: BOOKS_DEBUG=1, auto-reload, binding 0.0.0.0:5000." -ForegroundColor Cyan
        $env:BOOKS_DEBUG = '1'
        Push-Location $RepoRoot
        try {
            python -m web_ui.app
        } finally {
            Pop-Location
            Remove-Item Env:\BOOKS_DEBUG -ErrorAction SilentlyContinue
        }
    }

    'install' { Install-Reader }

    'spec' {
        # The desired definition, as data. Lets the tests assert on it without
        # registering anything, and answers "what would install write?" by hand.
        $desired = Get-DesiredTask
        $action = $desired.Action
        [PSCustomObject]@{
            TaskName                   = $TaskName
            Execute                    = $action.Execute
            Arguments                  = $action.Arguments
            WorkingDirectory           = $action.WorkingDirectory
            TriggerClass               = $desired.Trigger.CimClass.CimClassName
            TriggerDelay               = $desired.Trigger.Delay
            MultipleInstances          = "$($desired.Settings.MultipleInstances)"
            ExecutionTimeLimit         = $desired.Settings.ExecutionTimeLimit
            RestartCount               = $desired.Settings.RestartCount
            RestartInterval            = $desired.Settings.RestartInterval
            DisallowStartIfOnBatteries = $desired.Settings.DisallowStartIfOnBatteries
            StopIfGoingOnBatteries     = $desired.Settings.StopIfGoingOnBatteries
            AllowHardTerminate         = $desired.Settings.AllowHardTerminate
        } | ConvertTo-Json
    }

    'log' {
        if (-not (Test-Path $LogPath)) {
            Write-Host "No log yet at $LogPath" -ForegroundColor Yellow
        } else {
            Get-Content $LogPath -Tail 60
        }
    }
}
