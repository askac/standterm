Unicode true
RequestExecutionLevel user
SilentInstall silent
Name "StandTerm legacy detection test"
OutFile "${TEST_OUTPUT}"
!include "LogicLib.nsh"
!include "..\legacy-install.nsh"

Section
  ReadEnvStr $R2 "STANDTERM_LEGACY_TEST_ROOT"
  !insertmacro StandTermHasLegacyApp "$R2" $R0
  SetErrorLevel $R0
  Quit
SectionEnd
