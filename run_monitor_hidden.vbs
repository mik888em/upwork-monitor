Option Explicit

Dim shell
Dim fso
Dim scriptDir
Dim pwshPath
Dim runnerPath
Dim command
Dim exitCode

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

pwshPath = "C:\Program Files\PowerShell\7\pwsh.exe"
runnerPath = fso.BuildPath(scriptDir, "run_monitor.ps1")

If Not fso.FileExists(pwshPath) Then
    WScript.Quit 20
End If

If Not fso.FileExists(runnerPath) Then
    WScript.Quit 21
End If

command = _
    """" & pwshPath & """" & _
    " -NoLogo" & _
    " -NoProfile" & _
    " -NonInteractive" & _
    " -ExecutionPolicy Bypass" & _
    " -File " & _
    """" & runnerPath & """"

' Window style 0 = completely hidden.
' True = WScript waits and returns the real PowerShell exit code.
exitCode = shell.Run(command, 0, True)

WScript.Quit exitCode
