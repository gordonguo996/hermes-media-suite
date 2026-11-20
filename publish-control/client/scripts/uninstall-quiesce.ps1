<#
  uninstall-quiesce.ps1 -- stop every process that keeps the program directory
  from being deleted.  Called by the NSIS uninstaller BEFORE RMDir and again
  between delete retries; writes a log so a silent uninstall still leaves
  evidence of what it found.

  Why this file exists (CI 44741303657 / 44743050990, package job):
  uninstall left runtime\ + 19 DLLs behind, byte-identical set on both runs.
  The agent runs from the data dir venv
  (publish-worker\.venv\Scripts\python.exe) and loads python311.dll plus the
  stdlib .pyd files from the program dir runtime\DLLs via pyvenv.cfg, so the
  first fix (match exe/cmdline against the program dir) was not enough.

  Three matching rules, all scoped to our own directories -- nothing outside
  %LOCALAPPDATA%\Programs\HuolongAgent and %LOCALAPPDATA%\HuolongAgent is ever
  touched:
    1. ExecutablePath under the program dir (runtime python/panel/uninstall)
    2. ExecutablePath under the data dir    (venv python running the agent)
    3. CommandLine naming either dir        (wscript supervisor, Chrome with a
                                             user-data-dir in the data dir)
    4. Loaded module under the program dir  (last resort: the process holding
                                             runtime\DLLs, whatever its paths)
  Parents (wscript/cscript/cmd/powershell) die before their children, so a live
  supervisor cannot relaunch the agent mid-uninstall.  Three rounds, then a
  survivor list and exit 3.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Program,
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'HuolongAgent\publish-worker'),
  [string]$LogFile = '',
  [int]$Rounds = 3
)

$ErrorActionPreference = 'Continue'
$prog = $Program.TrimEnd('\')
$dat = $DataDir.TrimEnd('\')
$selfPid = $PID
$parentPid = 0
try { $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$selfPid").ParentProcessId } catch { }

function Write-Log([string]$m) {
  Write-Output $m
  if ($LogFile) {
    try { Add-Content -LiteralPath $LogFile -Value ((Get-Date).ToString('o') + ' ' + $m) -Encoding UTF8 -ErrorAction Stop } catch { }
  }
}

# Processes that could hold our files.  Kept narrow: walking Modules is the most
# expensive part and only these names can plausibly map our DLLs.
$CANDIDATE = '^(python|pythonw|python3|publish-assistant|agent|wscript|cscript|cmd|powershell|pwsh|chrome|msedge)\.exe$'

function Test-PathHit([string]$path) {
  if (-not $path) { return $false }
  return ($path.StartsWith($prog, [StringComparison]::OrdinalIgnoreCase) -or
          $path.StartsWith($dat, [StringComparison]::OrdinalIgnoreCase))
}

function Test-ModuleHit([int]$processId) {
  try {
    $p = Get-Process -Id $processId -ErrorAction Stop
    foreach ($m in $p.Modules) {
      $f = $m.FileName
      if ($f -and $f.StartsWith($prog, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
  } catch { }
  return $false
}

function Get-ProductProcesses {
  $hits = @()
  foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    if ($p.ProcessId -eq $selfPid -or $p.ProcessId -eq $parentPid -or $p.ProcessId -eq 0) { continue }
    $why = ''
    if (Test-PathHit $p.ExecutablePath) {
      $why = 'exe:' + $p.ExecutablePath
    } elseif ($p.CommandLine -and ((Test-PathHit $p.CommandLine) -or
              $p.CommandLine.IndexOf($prog, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
              $p.CommandLine.IndexOf($dat, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
      $why = 'cmdline'
    } elseif ($p.Name -match $CANDIDATE -and (Test-ModuleHit $p.ProcessId)) {
      $why = 'module'
    }
    if ($why) { $hits += [pscustomobject]@{ Process = $p; Why = $why } }
  }
  return $hits
}

Write-Log ("quiesce start program=" + $prog + " data=" + $dat)

for ($round = 1; $round -le $Rounds; $round++) {
  $targets = @(Get-ProductProcesses)
  if ($targets.Count -eq 0) { Write-Log "quiesce round $round`: nothing left to stop"; break }

  # Parents before children: a live supervisor relaunches the agent it watches.
  $parents = @($targets | Where-Object { $_.Process.Name -match '^(wscript|cscript|cmd|powershell|pwsh)\.exe$' })
  $rest = @($targets | Where-Object { $_.Process.Name -notmatch '^(wscript|cscript|cmd|powershell|pwsh)\.exe$' })

  foreach ($t in $parents) {
    Write-Log ("quiesce round $round`: stop supervisor " + $t.Process.Name + " pid=" + $t.Process.ProcessId + " (" + $t.Why + ")")
    Stop-Process -Id $t.Process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  if ($parents.Count -gt 0) { Start-Sleep -Milliseconds 500 }
  foreach ($t in $rest) {
    Write-Log ("quiesce round $round`: stop " + $t.Process.Name + " pid=" + $t.Process.ProcessId + " (" + $t.Why + ")")
    Stop-Process -Id $t.Process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 700
}

$left = @(Get-ProductProcesses)
if ($left.Count -gt 0) {
  foreach ($t in $left) {
    Write-Log ("quiesce WARN still running: " + $t.Process.Name + " pid=" + $t.Process.ProcessId + " (" + $t.Why + ")")
  }
  Write-Log "quiesce exit 3 (survivors)"
  exit 3
}
Write-Log "quiesce exit 0"
exit 0
