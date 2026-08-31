<#
.SYNOPSIS
    Control the TranslateBooksNightly scheduled task.

.DESCRIPTION
    The nightly pass reviews every in-scope book's pending annotations, applies
    the safe subset, and leaves the rest in /review-inbox. This script is the
    only definition of the task: `install` registers it from `Get-DesiredTask`
    and `status` audits the live task against that same definition, so the two
    cannot drift.

    Sibling to scripts/reader.ps1, with three deliberate differences:

      * ExecutionTimeLimit is PT2H, not PT0S. The reader is a service and must
        never be killed; this is a batch and needs a ceiling, matched to
        `automation.deadline_minutes` in app_config.json.
      * A daily trigger at 06:30 rather than at-startup.
      * No restart-on-failure. A failed night is a night's work missed, not an
        outage, and the next run picks up exactly where it stopped (`prepare`
        keeps drafts, `fanout` skips finished ones, `commit` merges by key).

.EXAMPLE
    scripts\nightly.ps1 install
    scripts\nightly.ps1 status
    scripts\nightly.ps1 run
    scripts\nightly.ps1 log
    scripts\nightly.ps1 spec
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'install', 'run', 'log', 'spec')]
    [string]$Command = 'status'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'TranslateBooksNightly'
$TaskDescription = 'Nightly annotation pass for translate_books. Managed by scripts/nightly.ps1.'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $RepoRoot 'logs'
$NightlyLog = Join-Path $LogDir 'nightly.jsonl'
$DriverPath = Join-Path $RepoRoot 'scripts\daily_pass.py'
$DigestDir = Join-Path $RepoRoot 'reports\nightly'

# 06:30 local. Late enough that an overnight machine is settled, early enough
# that the inbox is ready before the working day.
$TriggerTime = '06:30'

# Deliberately $false. With WakeToRun=$true the machine wakes at 06:30 to run
# this; with $false a sleeping laptop starts the pass at its next wake instead
# (StartWhenAvailable), so the inbox may not be ready when you sit down. Flip
# this one line to change the tradeoff -- Test-TaskDrift audits whichever you
# pick, so the live task and this file stay in agreement either way.
$WakeToRun = $false

function Get-PythonPath {
    <#
        A task action needs an absolute interpreter path: under the scheduler
        there is no PATH worth relying on, and the Store alias is a stub that
        exits immediately and explains nothing.
    #>
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "python is not on PATH. A task action needs an absolute interpreter path."
    }
    if ($cmd.Source -like '*\WindowsApps\*') {
        throw "python resolves to the Microsoft Store alias ($($cmd.Source)). Install a real Python first."
    }
    return $cmd.Source
}

function Get-DesiredSettings {
    <#
        Machine-independent, so `status` can audit without resolving an
        interpreter -- the same split reader.ps1 uses, for the same reason: the
        audit must work on a shell where python is missing.
    #>
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    # IgnoreNew plus the driver's own repo lock (logs/.nightly.lock) is belt and
    # braces: the scheduler stops a second *task* instance, the lock stops a
    # hand-run `daily_pass.py` from interleaving with the scheduled one.
    $settings.WakeToRun = $WakeToRun
    return $settings
}

function Get-DesiredTask {
    $action = New-ScheduledTaskAction `
        -Execute (Get-PythonPath) `
        -Argument "`"$DriverPath`"" `
        -WorkingDirectory $RepoRoot

    $trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime

    return [PSCustomObject]@{
        Action   = $action
        Trigger  = $trigger
        Settings = Get-DesiredSettings
    }
}

function Test-TaskDrift {
    <#
        One human-readable string per mismatch between the registered task and
        Get-DesiredTask; nothing when they agree. Tolerant by design: `status`
        is what you run when something is already wrong, so a missing property
        must produce a finding rather than a traceback.
    #>
    param($Task)

    $findings = @()
    $desiredSettings = Get-DesiredSettings

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
    if ($action.Arguments -notlike "*$DriverPath*") {
        $findings += "Arguments do not point at $DriverPath (found: $($action.Arguments))"
    }

    $s = $Task.Settings
    if ("$($s.ExecutionTimeLimit)" -ne "$($desiredSettings.ExecutionTimeLimit)") {
        $findings += "ExecutionTimeLimit=$($s.ExecutionTimeLimit), expected $($desiredSettings.ExecutionTimeLimit)"
    }
    if ("$($s.MultipleInstances)" -ne "$($desiredSettings.MultipleInstances)") {
        $findings += "MultipleInstances=$($s.MultipleInstances), expected $($desiredSettings.MultipleInstances)"
    }
    if (-not $s.StartWhenAvailable) {
        $findings += 'StartWhenAvailable=false - a missed run is never made up'
    }
    if ($s.DisallowStartIfOnBatteries) {
        $findings += 'DisallowStartIfOnBatteries=true - will not run on battery'
    }
    if ($s.StopIfGoingOnBatteries) {
        $findings += 'StopIfGoingOnBatteries=true - the pass dies when you unplug'
    }
    if ($s.WakeToRun -ne $desiredSettings.WakeToRun) {
        $findings += "WakeToRun=$($s.WakeToRun), expected $($desiredSettings.WakeToRun)"
    }

    $daily = @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' })
    if ($daily.Count -eq 0) {
        $findings += 'no daily trigger - the pass will never start on its own'
    } else {
        $at = ([datetime]$daily[0].StartBoundary).ToString('HH:mm')
        if ($at -ne $TriggerTime) {
            $findings += "daily trigger at $at, expected $TriggerTime"
        }
    }

    return $findings
}

function Install-Nightly {
    $user = "$env:USERDOMAIN\$env:USERNAME"

    if (-not (Test-Path $DriverPath)) {
        throw "Not found: $DriverPath"
    }
    $python = Get-PythonPath
    # Cheaper to fail here than to register a task that dies every morning at
    # 06:30 leaving one line in a log nobody is watching.
    & $python -c "import sys; sys.path.insert(0, r'$RepoRoot'); import src.actions" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "$python cannot import src.actions from $RepoRoot. Run: pip install -r requirements.txt"
    }

    Write-Host "Python:    $python"
    Write-Host "Driver:    $DriverPath"
    Write-Host "Start in:  $RepoRoot"
    Write-Host "Trigger:   daily at $TriggerTime (WakeToRun=$WakeToRun)"
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

    $desired = Get-DesiredTask
    # Same principal shape as the reader task, and for the same reason: the
    # headless CLIs authenticate against a subscription login that lives in the
    # user profile, so a SYSTEM task would find `claude auth status` logged out.
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
        Write-Host "Registration failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Usually one of: wrong password, or '$user' lacks the 'Log on as a batch job' right" -ForegroundColor Yellow
        Write-Host "(secpol.msc -> Local Policies -> User Rights Assignment)" -ForegroundColor Yellow
        throw
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        Remove-Variable plain -ErrorAction SilentlyContinue
    }

    Write-Host "Registered $TaskName." -ForegroundColor Green
    Write-Host "Dry-run it now with: python scripts\daily_pass.py --dry-run" -ForegroundColor Cyan
}

switch ($Command) {
    'status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "Task:      '$TaskName' is not registered." -ForegroundColor Yellow
            Write-Host "Config:    register it with 'scripts\nightly.ps1 install'" -ForegroundColor Yellow
        } else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Host "Task:      $TaskName [$($task.State)]"
            Write-Host "Last run:  $($info.LastRunTime)  result 0x$('{0:X}' -f $info.LastTaskResult)"
            Write-Host "Next run:  $($info.NextRunTime)"

            try {
                $drift = @(Test-TaskDrift -Task $task)
                if ($drift.Count -eq 0) {
                    Write-Host "Config:    ok" -ForegroundColor Green
                } else {
                    foreach ($d in $drift) {
                        Write-Host "Config:    $d" -ForegroundColor Yellow
                    }
                    Write-Host "Config:    fix with 'scripts\nightly.ps1 install'" -ForegroundColor Yellow
                }
            } catch {
                Write-Host "Config:    could not audit - $($_.Exception.Message)" -ForegroundColor Red
            }
        }

        # A held repo lock means a pass is running right now (or died holding
        # it); the driver breaks a lock older than three hours on its own.
        $repoLock = Join-Path $LogDir '.nightly.lock'
        if (Test-Path $repoLock) {
            Write-Host "Lock:      held - $(Get-Content $repoLock -Raw)" -ForegroundColor Yellow
        } else {
            Write-Host "Lock:      free"
        }

        if (Test-Path $NightlyLog) {
            $last = Get-Content $NightlyLog -Tail 1 | ConvertFrom-Json
            Write-Host "Last pass: $($last.ts)  $($last.totals.committed) reviewed, $($last.totals.applied) applied, $($last.totals.held) held  [$($last.stopped_because)]"
        } else {
            Write-Host "Last pass: none yet ($NightlyLog)"
        }
    }

    'run' {
        # Force one execution now. The task still holds the repo lock, so this
        # cannot overlap a scheduled run already in flight.
        schtasks /run /tn $TaskName
        Write-Host "Started. Watch it with 'scripts\nightly.ps1 log'." -ForegroundColor Green
    }

    'log' {
        if (Test-Path $NightlyLog) {
            Write-Host "--- $NightlyLog (last 10 passes) ---"
            Get-Content $NightlyLog -Tail 10
        } else {
            Write-Host "No pass log yet at $NightlyLog" -ForegroundColor Yellow
        }
        if (Test-Path $DigestDir) {
            $digest = Get-ChildItem $DigestDir -Filter '*.md' | Sort-Object LastWriteTime | Select-Object -Last 1
            if ($digest) {
                Write-Host ""
                Write-Host "--- $($digest.FullName) ---"
                Get-Content $digest.FullName
            }
        }
    }

    'spec' {
        # The desired definition as data: lets the tests assert on it without
        # registering anything, and answers "what would install write?".
        $desired = Get-DesiredTask
        $action = $desired.Action
        $desiredSettings = $desired.Settings
        [PSCustomObject]@{
            TaskName                   = $TaskName
            Execute                    = $action.Execute
            Arguments                  = $action.Arguments
            WorkingDirectory           = $action.WorkingDirectory
            TriggerClass               = $desired.Trigger.CimClass.CimClassName
            TriggerAt                  = $TriggerTime
            MultipleInstances          = "$($desiredSettings.MultipleInstances)"
            ExecutionTimeLimit         = $desiredSettings.ExecutionTimeLimit
            StartWhenAvailable         = $desiredSettings.StartWhenAvailable
            WakeToRun                  = $desiredSettings.WakeToRun
            DisallowStartIfOnBatteries = $desiredSettings.DisallowStartIfOnBatteries
            StopIfGoingOnBatteries     = $desiredSettings.StopIfGoingOnBatteries
        } | ConvertTo-Json
    }

    'install' { Install-Nightly }
}
