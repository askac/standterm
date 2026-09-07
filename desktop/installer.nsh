!include "nsDialogs.nsh"
!include "Sections.nsh"
!include "legacy-install.nsh"

!define ST_MODE_KEY "Software\StandTermDesktop\Installer"
!define ST_ENV_SECTION 1

!ifndef BUILD_UNINSTALLER
  Var StandTermMode
  Var StandTermDialog
  Var StandTermWindows
  Var StandTermBoth
  Var StandTermWSL
!endif
Var StandTermParent
Var StandTermExit

!macro preInit
  ${If} ${isForAllUsers}
    SetErrorLevel 5
    Quit
  ${EndIf}
!macroend

!macro customInstallMode
  ${If} ${isForAllUsers}
    SetErrorLevel 5
    Quit
  ${EndIf}
  StrCpy $isForceCurrentInstall "1"
!macroend

!macro customCheckAppRunning
  ; Never invoke the builder's automatic Stop-Process/taskkill path.
  ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0
  ${If} $R0 != 603
    MessageBox MB_OK|MB_ICONEXCLAMATION "Quit StandTerm Desktop (including tray windows) before continuing. Setup will not stop sessions for you." /SD IDOK
    SetErrorLevel 4
    Quit
  ${EndIf}
!macroend

!macro customInit
  ${If} ${Silent}
    SetErrorLevel 5
    Quit
  ${EndIf}
  ; This candidate supports only the standard per-user installation location.
  ; Reject hidden /D overrides rather than letting an uninstaller own a broad path.
  !insertmacro GetDParameter $R0
  ${If} $R0 != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "Custom installation paths are not supported by this candidate." /SD IDOK
    SetErrorLevel 5
    Quit
  ${EndIf}
  ; Squirrel and NSIS own different registrations. Never execute an arbitrary
  ; registry uninstall command or remove the old app/environment automatically.
  StrCpy $R2 ""
  SetRegView 32
  ReadRegStr $R2 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StandTermDesktopEvaluation" "DisplayName"
  SetRegView 64
  ReadRegStr $R3 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\StandTermDesktopEvaluation" "DisplayName"
  ${If} $R2 != ""
  ${OrIf} $R3 != ""
    Goto standtermLegacy
  ${EndIf}
  ${nsProcess::FindProcess} "StandTermDesktopEvaluation.exe" $R0
  ${If} $R0 == 0
    Goto standtermLegacy
  ${ElseIf} $R0 != 603
    MessageBox MB_OK|MB_ICONEXCLAMATION "Could not check whether the older StandTerm Desktop is running. Close it and retry; setup will not stop sessions for you." /SD IDOK
    SetErrorLevel 7
    Quit
  ${EndIf}
  !insertmacro StandTermHasLegacyApp "$LOCALAPPDATA\StandTermDesktopEvaluation" $R0
  ${If} $R0 == 1
    Goto standtermLegacy
  ${EndIf}
  Goto standtermNoLegacy
  standtermLegacy:
    MessageBox MB_OK|MB_ICONEXCLAMATION "An older Squirrel-based StandTerm Desktop is still registered, running, or has an application executable.$\r$\n$\r$\nQuit it and uninstall its desktop interface from Windows Settings first, then run this installer again. Keep its environments and settings. This installer will not uninstall it or stop your sessions.$\r$\n$\r$\nIf Windows no longer lists it, ask for help checking the remaining application files. Update.exe alone does not block installation." /SD IDOK
    SetErrorLevel 6
    Quit
  standtermNoLegacy:
  ReadRegStr $StandTermMode HKCU "${ST_MODE_KEY}" "Mode"
  ${If} $StandTermMode != "windows"
  ${AndIf} $StandTermMode != "both"
  ${AndIf} $StandTermMode != "wsl"
    StrCpy $StandTermMode "windows"
  ${EndIf}
!macroend

!macro customPageAfterChangeDir
  Page custom StandTermModePage StandTermModeLeave

  Function StandTermModePage
    !insertmacro MUI_HEADER_TEXT "Choose Core environments" "Prepare Python environments now, before setup completes."
    nsDialogs::Create 1018
    Pop $StandTermDialog
    ${If} $StandTermDialog == error
      Abort
    ${EndIf}
    ${NSD_CreateLabel} 0 0 100% 32u "Python 3.10+ with venv support must already be installed in each selected environment. StandTerm does not install system Python, WSL, or a Linux distribution."
    Pop $0
    ${NSD_CreateRadioButton} 0 40u 100% 14u "Windows only - native Windows shell"
    Pop $StandTermWindows
    ${NSD_CreateRadioButton} 0 62u 100% 14u "Windows + WSL - two separate shortcuts"
    Pop $StandTermBoth
    ${NSD_CreateRadioButton} 0 84u 100% 14u "WSL only - Linux shell through an existing distribution"
    Pop $StandTermWSL
    ${If} $StandTermMode == "both"
      ${NSD_Check} $StandTermBoth
    ${ElseIf} $StandTermMode == "wsl"
      ${NSD_Check} $StandTermWSL
    ${Else}
      ${NSD_Check} $StandTermWindows
    ${EndIf}
    ${NSD_CreateLabel} 0 110u 100% 45u "Next, select the WSL distribution when applicable and approve dependency installation. Internet access is needed. Keep the preparation window open (minimizing is safe). All selected environments must succeed before shortcuts are created."
    Pop $0
    nsDialogs::Show
  FunctionEnd

  Function StandTermModeLeave
    StrCpy $StandTermMode "windows"
    ${NSD_GetState} $StandTermBoth $0
    ${If} $0 == ${BST_CHECKED}
      StrCpy $StandTermMode "both"
    ${EndIf}
    ${NSD_GetState} $StandTermWSL $0
    ${If} $0 == ${BST_CHECKED}
      StrCpy $StandTermMode "wsl"
    ${EndIf}
  FunctionEnd
!macroend

!macro customInstall
  System::Call 'kernel32::GetCurrentProcessId() i.r0'
  StrCpy $StandTermParent $0
  DetailPrint "Preparing selected Core environments. Keep the preparation window open."
  ClearErrors
  ExecWait '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --installer-prepare=$StandTermMode --installer-parent=$StandTermParent' $StandTermExit
  ${If} ${Errors}
    StrCpy $StandTermExit 1
  ${EndIf}
  ${If} $StandTermExit != 0
    SetErrorLevel $StandTermExit
    Abort "Environment preparation did not complete. Application files and partial environments are retained. Run setup again to retry."
  ${EndIf}
  WriteRegStr HKCU "${ST_MODE_KEY}" "Mode" "$StandTermMode"
  DetailPrint "All selected environments are ready. Use the corresponding desktop shortcut."
!macroend

!macro customUnInit
  ${If} ${isForAllUsers}
    SetErrorLevel 5
    Quit
  ${EndIf}
  !insertmacro GetDParameter $R0
  ${If} $R0 != ""
    SetErrorLevel 5
    Quit
  ${EndIf}
  ; Keep all preferences even if a caller supplies builder's delete-app-data flag.
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "--delete-app-data" $R1
  ${IfNot} ${Errors}
    SetErrorLevel 5
    Quit
  ${EndIf}
!macroend

!macro customUnInstall
  ; Internal NSIS upgrades retain environments, preferences and mode shortcuts.
  ${IfNot} ${isUpdated}
    StrCpy $R2 ""
    ${IfNot} ${Silent}
      SectionGetFlags ${ST_ENV_SECTION} $R1
      IntOp $R1 $R1 & ${SF_SELECTED}
      ${If} $R1 != 0
        StrCpy $R2 "--installer-cleanup-venvs"
      ${EndIf}
    ${EndIf}
    System::Call 'kernel32::GetCurrentProcessId() i.r0'
    StrCpy $StandTermParent $0
    ClearErrors
    ExecWait '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --installer-uninstall --installer-parent=$StandTermParent $R2' $StandTermExit
    ${If} ${Errors}
      StrCpy $StandTermExit 1
    ${EndIf}
    ${If} $StandTermExit != 0
      SetErrorLevel $StandTermExit
      Abort "StandTerm maintenance did not complete. No application files were removed. Retry after closing other setup windows."
    ${EndIf}
    DeleteRegValue HKCU "${ST_MODE_KEY}" "Mode"
    DeleteRegKey /ifempty HKCU "${ST_MODE_KEY}"
  ${EndIf}
!macroend

!macro customUnInstallSection
  Section /o "un.Move idle venvs to recovery (keeps disk usage)" StandTermEnvironmentSection
    ; Selection is consumed before application files are removed, not here.
  SectionEnd
  !if ${StandTermEnvironmentSection} != ${ST_ENV_SECTION}
    !error "Unexpected uninstall section order"
  !endif
!macroend
