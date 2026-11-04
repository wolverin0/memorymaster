Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")

command = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " & _
    "-ExecutionPolicy Bypass -File ""{CODE}\scripts\windows-hermes-sync.ps1"""
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
