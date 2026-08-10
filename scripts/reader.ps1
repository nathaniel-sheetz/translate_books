<#
.SYNOPSIS
    Control the TranslateBooksReader service task.

.DESCRIPTION
    Under the scheduled task there is no auto-reload: after a code edit the
    service must be bounced before the change is live. `restart` is that, in
    one command. `dev` stops the service and runs the reloading dev server in
    the foreground instead.

.EXAMPLE
    scripts\reader.ps1 restart
    scripts\reader.ps1 status
    scripts\reader.ps1 log
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'dev', 'log')]
    [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'TranslateBooksReader'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Port = 5000
$HealthUrl = "http://127.0.0.1:$Port/healthz"
$LogPath = Join-Path $RepoRoot 'logs\web_ui.log'

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
        } else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host "Task:      $TaskName [$($task.State)]"
            Write-Host "Last run:  $($info.LastRunTime)  result 0x$('{0:X}' -f $info.LastTaskResult)"
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

    'log' {
        if (-not (Test-Path $LogPath)) {
            Write-Host "No log yet at $LogPath" -ForegroundColor Yellow
        } else {
            Get-Content $LogPath -Tail 60
        }
    }
}
