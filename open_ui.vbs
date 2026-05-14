' open_ui.vbs — open the Sift UI in a chromeless app window.
'
' Use this when the Sift server is already running but you closed the window
' (or started it with --no-browser).
'
' Tries Edge first, then Chrome, then falls back to your default browser.

Option Explicit

Dim shell, url
Set shell = CreateObject("WScript.Shell")
url = "http://127.0.0.1:8765/"

On Error Resume Next
shell.Run "msedge.exe --app=" & url & " --new-window", 0, False
If Err.Number = 0 Then WScript.Quit
Err.Clear

shell.Run "chrome.exe --app=" & url & " --new-window", 0, False
If Err.Number = 0 Then WScript.Quit
Err.Clear

' Fallback: default browser via Windows protocol handler.
shell.Run url, 0, False
If Err.Number <> 0 Then
    MsgBox "Could not open the UI. Visit " & url & " manually in any browser.", _
           vbExclamation, "Sift"
End If
On Error Goto 0
