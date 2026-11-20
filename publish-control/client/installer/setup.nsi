Unicode true
!ifndef PAYLOAD
  !error "PAYLOAD required"
!endif
!ifndef OUTPUT
  !error "OUTPUT required"
!endif
!ifndef VERSION
  !define VERSION "0"
!endif
!include "LogicLib.nsh"
!include "FileFunc.nsh"

; 固定、不带版本号的安装目录：开机任务、快捷方式、升级都指向同一个位置。
; 数据（账号、授权、日志）在 %LOCALAPPDATA%\HuolongAgent\publish-worker，升级和卸载都不动。
!define APPDIR "$LOCALAPPDATA\Programs\HuolongAgent\publish-assistant"
!define UNKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\HuolongPublishAssistant"

Name "漫剧发布助手"
OutFile "${OUTPUT}"
RequestExecutionLevel user
InstallDir "${APPDIR}"
SilentInstall normal
AutoCloseWindow true
Page instfiles
UninstPage instfiles
ShowInstDetails show
SetCompressor /SOLID lzma
VIProductVersion "1.2.0.0"
VIAddVersionKey /LANG=2052 "ProductName" "漫剧发布助手"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "FileDescription" "漫剧发布助手安装程序"

Section
  DetailPrint "正在准备安装到固定目录：$INSTDIR"
  ; 1. 停掉正在运行、且程序文件位于安装目录内的进程（只动这个目录里的，不碰别的 Python）。
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "$$d=''$INSTDIR''; Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$d, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2"'
  Pop $0

  ; 2. 旧版整体改名留作回滚；删不掉说明有文件被占用，明确报错而不是装一半。
  RMDir /r "$INSTDIR.old"
  ${If} ${FileExists} "$INSTDIR\*.*"
    ClearErrors
    Rename "$INSTDIR" "$INSTDIR.old"
    IfErrors 0 +3
      MessageBox MB_OK|MB_ICONSTOP "旧版本目录无法移动（有程序正在使用其中的文件）。请关闭漫剧发布助手和相关浏览器窗口后重试。目录：$INSTDIR"
      Goto abort_keep_old
  ${EndIf}

  ClearErrors
  CreateDirectory "$INSTDIR"
  SetOutPath "$INSTDIR"
  IfErrors extract_failed
  File /r "${PAYLOAD}\*"
  IfErrors extract_failed

  DetailPrint "正在完成安装与自动授权，请查看安装窗口。"
  IfSilent run_silent
  ExecWait '"$SYSDIR\cmd.exe" /d /c ""$INSTDIR\bootstrap.bat""' $0
  Goto ran
run_silent:
  ExecWait '"$SYSDIR\cmd.exe" /d /c ""$INSTDIR\bootstrap.bat" --silent"' $0
ran:
  IfErrors launch_failed
  IntCmp $0 0 ok failed failed

ok:
  ; 3. 登记卸载入口（只写当前用户）。
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UNKEY}" "DisplayName" "漫剧发布助手"
  WriteRegStr HKCU "${UNKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNKEY}" "NoRepair" 1
  RMDir /r "$INSTDIR.old"
  SetErrorLevel 0
  Goto end

extract_failed:
  System::Call 'kernel32::GetLastError() i .r1'
  MessageBox MB_OK|MB_ICONSTOP "文件展开失败（系统错误 $1）。目标：$INSTDIR。请检查剩余空间、写入权限或文件占用后重试。"
  Goto rollback
launch_failed:
  System::Call 'kernel32::GetLastError() i .r1'
  MessageBox MB_OK|MB_ICONSTOP "无法启动安装窗口（系统错误 $1）。程序：$SYSDIR\cmd.exe；入口：$INSTDIR\bootstrap.bat。请联系服务商。"
  Goto rollback
failed:
  MessageBox MB_OK|MB_ICONSTOP "安装未完成（错误码 $0）。请保留安装窗口中的原因并联系服务商。日志位于 $LOCALAPPDATA\HuolongAgent\publish-worker\logs\install.log。"
rollback:
  ; 新装失败：把旧版本放回去，客户不会因为一次失败的升级丢掉能用的版本。
  RMDir /r "$INSTDIR"
  ${If} ${FileExists} "$INSTDIR.old\*.*"
    Rename "$INSTDIR.old" "$INSTDIR"
  ${EndIf}
abort_keep_old:
  SetErrorLevel 5
  Quit
end:
SectionEnd

Section "Uninstall"
  ; 1. 先拆自启动。守护进程/计划任务会在删除过程中把程序重新拉起来，导致目录删不干净——
  ;    这一步必须排在杀进程和删目录之前。
  ;    真实的自启动名（1.2.178 起）：计划任务 HuolongAgent\Agent，Run 值 HuolongAgentAgent。
  ;    下面 Manju* 是更早版本留下的名字，一并清掉，别让老机器带着自启动残留。
  ${If} ${FileExists} "$INSTDIR\scripts\install-autostart.ps1"
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\scripts\install-autostart.ps1" -Program "$INSTDIR" -DataDir "$LOCALAPPDATA\HuolongAgent\publish-worker" -Remove'
    Pop $0
  ${EndIf}
  nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "HuolongAgent\Agent" /F'
  Pop $0
  nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "ManjuPublishAssistantSupervisor" /F'
  Pop $0
  nsExec::ExecToLog '"$SYSDIR\schtasks.exe" /Delete /TN "ManjuPublishAssistant" /F'
  Pop $0
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "HuolongAgentAgent"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "ManjuPublishAssistantSupervisor"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "ManjuPublishAssistant"

  ; 2. 停掉所有占用安装目录的进程。agent 跑在数据目录的 venv 里
  ;    （publish-worker\.venv\Scripts\python.exe），它通过 pyvenv.cfg 从
  ;    $INSTDIR\runtime\DLLs 加载 python311.dll 和标准库 .pyd——这样的进程既不在安装目录里、
  ;    命令行也可能不带安装目录，老版本两条判据都匹配不到，runtime 目录于是卸载后仍在
  ;    （CI 44741303657：残留 21 项，全是 runtime/DLLs 下的 pyd 和 openssl dll）。
  ;    uninstall-quiesce.ps1 认三种进程：程序目录内的、数据目录内的、命令行里带这两个目录的
  ;    （wscript supervisor、用数据目录存 profile 的 Chrome），先杀父进程再杀子进程，跑三轮。
  ${If} ${FileExists} "$INSTDIR\scripts\uninstall-quiesce.ps1"
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\scripts\uninstall-quiesce.ps1" -Program "$INSTDIR" -DataDir "$LOCALAPPDATA\HuolongAgent\publish-worker" -LogFile "$LOCALAPPDATA\HuolongAgent\publish-worker\logs\uninstall-quiesce.log"'
    Pop $0
  ${EndIf}

  ; 3. 删程序目录。刚杀完进程文件可能还被占着，删不掉就等一会儿重试，最多 4 次；
  ;    每次重试前再清一遍进程，防止守护进程刚把子进程拉起来。
  RMDir /r "$INSTDIR.old"
  StrCpy $1 0
  uninstall_retry:
    RMDir /r "$INSTDIR"
    ${IfNot} ${FileExists} "$INSTDIR\*.*"
      Goto uninstall_done
    ${EndIf}
    IntOp $1 $1 + 1
    ${If} $1 < 4
      ${If} ${FileExists} "$INSTDIR\scripts\uninstall-quiesce.ps1"
        nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\scripts\uninstall-quiesce.ps1" -Program "$INSTDIR" -DataDir "$LOCALAPPDATA\HuolongAgent\publish-worker" -LogFile "$LOCALAPPDATA\HuolongAgent\publish-worker\logs\uninstall-quiesce.log"'
        Pop $2
      ${EndIf}
      ExecWait '"$SYSDIR\cmd.exe" /d /c "timeout /t 3 /nobreak >nul"'
      Goto uninstall_retry
    ${EndIf}
  uninstall_done:
  ; 4. 兜底：删完还在（句柄刚释放、或删除过程中又被拉起），留一个延迟清理脚本，
  ;    卸载器退出后 4 秒再删一次。脚本放 %TEMP%，不占安装目录，也不写数据目录。
  ${If} ${FileExists} "$INSTDIR\*.*"
    StrCpy $3 "$TEMP\huolong-uninstall-cleanup.cmd"
    ClearErrors
    FileOpen $4 "$3" w
    IfErrors cleanup_skip
      FileWrite $4 '@echo off$\r$\n'
      FileWrite $4 'ping -n 5 127.0.0.1 >nul$\r$\n'
      FileWrite $4 'rd /s /q "$INSTDIR"$\r$\n'
      FileClose $4
      ; start /min：不留一个黑窗口在客户屏幕上停 4 秒；外层 cmd 立刻退出。
      Exec '"$SYSDIR\cmd.exe" /d /c start /min "" "$3"'
    cleanup_skip:
  ${EndIf}
  DeleteRegKey HKCU "${UNKEY}"
  DetailPrint "程序已卸载。账号、授权和日志保留在 $LOCALAPPDATA\HuolongAgent\publish-worker，如需彻底清除请手动删除该文件夹。"
SectionEnd
