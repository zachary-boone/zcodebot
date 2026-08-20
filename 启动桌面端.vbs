' CodeBot desktop hidden launcher.
' Launches the batch launcher next to this script with a hidden console
' window, so no cmd window flashes when starting from the desktop shortcut.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Find the .bat launcher in this folder (avoids hardcoding a non-ASCII name here).
batPath = ""
For Each f In fso.GetFolder(scriptDir).Files
    If LCase(fso.GetExtensionName(f.Name)) = "bat" Then
        batPath = f.Path
        Exit For
    End If
Next

If batPath = "" Then
    MsgBox "CodeBot launcher batch file not found in the project folder.", _
           vbCritical, "CodeBot"
    WScript.Quit 1
End If

' intWindowStyle=0 -> hidden window; bWaitOnReturn=False -> exit immediately,
' the batch keeps running in the background and Electron shows its own window.
shell.Run """" & batPath & """ /silent", 0, False
