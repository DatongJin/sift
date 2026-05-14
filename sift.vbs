' sift.vbs — double-click to launch Sift (silent, no console window).
' Requires Python (pythonw.exe on PATH or the py launcher) on this machine.
' For a fully standalone .exe (no Python needed), run build_exe.py instead.

Option Explicit

Dim shell, fso, scriptDir
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Run from the script's own folder so the quarantine path is predictable.
shell.CurrentDirectory = scriptDir

' 0 = hidden window, False = don't wait. pythonw.exe = windowless Python.
On Error Resume Next
shell.Run "pythonw.exe """ & scriptDir & "\dupfinder_app.py""", 0, False
If Err.Number <> 0 Then
    ' Fallback: try the py launcher (default on most Python.org installs).
    Err.Clear
    shell.Run "pyw.exe """ & scriptDir & "\dupfinder_app.py""", 0, False
    If Err.Number <> 0 Then
        MsgBox "Could not start Python. Make sure Python is installed and " & _
               "either pythonw.exe is on PATH or the py launcher is available." & vbCrLf & vbCrLf & _
               "Or build a standalone .exe by running: python build_exe.py", _
               vbExclamation, "Sift"
    End If
End If
On Error Goto 0
