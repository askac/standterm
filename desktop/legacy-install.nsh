; File presence is separate from registry/process checks. Update.exe and .dead
; may remain after Squirrel uninstall; neither proves the application exists.
!macro StandTermHasLegacyApp ROOT RESULT
  Push $R6
  Push $R7
  StrCpy ${RESULT} 0
  ClearErrors
  FindFirst $R6 $R7 "${ROOT}\app-*"
  ${IfNot} ${Errors}
    ${Do}
      ${If} $R7 == ""
        ${Break}
      ${EndIf}
      ${If} ${FileExists} "${ROOT}\$R7\StandTermDesktopEvaluation.exe"
        StrCpy ${RESULT} 1
        ${Break}
      ${EndIf}
      ClearErrors
      FindNext $R6 $R7
      ${If} ${Errors}
        ${Break}
      ${EndIf}
    ${Loop}
    FindClose $R6
  ${EndIf}
  Pop $R7
  Pop $R6
!macroend
