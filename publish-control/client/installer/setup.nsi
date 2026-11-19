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
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "$$d=''$INSTDIR''; Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$d, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2"'
  Pop $0
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNKEY}"
  DetailPrint "程序已卸载。账号、授权和日志保留在 $LOCALAPPDATA\HuolongAgent\publish-worker，如需彻底清除请手动删除该文件夹。"
SectionEnd
