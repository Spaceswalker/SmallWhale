Set sh = CreateObject("WScript.Shell")
p = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & p & "\qq-update-guard.ps1""", 0, False
