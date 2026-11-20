<#
  uninstall-quiesce.ps1 -- stop every process that keeps the program directory
  from being deleted.  Called by the NSIS uninstaller BEFORE RMDir, and again
  between delete retries.

  Why this file exists (CI run 44741303657, package job):
  The uninstaller only killed processes whose ExecutablePath/CommandLine held the
  program dir.  The agent itself runs from the data dir venv
  (%LOCALAPPDATA%\HuolongAgent\publish-worker\.venv\Scripts\python.exe), which
  loads python311.dll and the stdlib .pyd files from the program dir
  runtime\DLLs through pyvenv.cfg.  That process matched neither filter, so after
  uninstall the runtime tree stayed behind ("卸载后仍残留 21 项").

  Matched (never anything outside our own two directories):
    * ExecutablePath under the program dir  (runtime\python.exe, panel, uninstall)
    * ExecutablePath under the data dir     (venv python used by the agent)
    * CommandLine containing either dir     (wscript supervisor, Chrome with a
                                             user-data-dir inside the data dir)
  Killed parents first (wscript/cscript/cmd) so a supervisor cannot relaunch its
  child while we are terminating it; then everything else.  Three rounds.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Program,
  [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'HuolongAgent\publish-worker'),
  [int]$Rounds = 3
)

$ErrorActionPreference = 'Continue'
$prog = $Program.TrimEnd('\')
$dat = $DataDir.TrimEnd('\')
$selfPid = $PID
$parentPid = 0
try { $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$selfPid").ParentProcessId } catch { }

function Get-ProductProcesses {
  $hits = @()
  foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    if ($p.ProcessId -eq $selfPid -or $p.ProcessId -eq $parentPid -or $p.ProcessId -eq 0) { continue }
    $exe = $p.ExecutablePath
    $cmd = $p.CommandLine
    $hit = $false
    if ($exe -and ($exe.StartsWith($prog, [StringComparison]::OrdinalIgnoreCase) -or
                   $exe.StartsWith($dat, [StringComparison]::OrdinalIgnoreCase))) {
      $hit = $true
    } elseif ($cmd -and ($cmd.IndexOf($prog, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                         $cmd.IndexOf($dat, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
      $hit = $true
    }
    if ($hit) { $hits += $p }
  }
  return $hits
}

for ($round = 1; $round -le $Rounds; $round++) {
  $targets = @(Get-ProductProcesses)
  if ($targets.Count -eq 0) { Write-Output "quiesce round $round: nothing left to stop"; break }

  # Parents before children: a live supervisor relaunches the agent it watches.
  $parents = @($targets | Where-Object { $_.Name -match '^(wscript|cscript|cmd|powershell|pwsh)\.exe$' })
  $rest = @($targets | Where-Object { $_.Name -notmatch '^(wscript|cscript|cmd|powershell|pwsh)\.exe$' })

  foreach ($p in $parents) {
    Write-Output ("quiesce: stop supervisor " + $p.Name + " pid=" + $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  if ($parents.Count -gt 0) { Start-Sleep -Milliseconds 500 }
  foreach ($p in $rest) {
    Write-Output ("quiesce: stop " + $p.Name + " pid=" + $p.ProcessId + " exe=" + $p.ExecutablePath)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 700
}

$left = @(Get-ProductProcesses)
if ($left.Count -gt 0) {
  foreach ($p in $left) {
    Write-Output ("WARN still running: " + $p.Name + " pid=" + $p.ProcessId + " exe=" + $p.ExecutablePath)
  }
  exit 3
}
exit 0
