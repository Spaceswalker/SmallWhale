Set sh = CreateObject("WScript.Shell")
home = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = home & "\..\AstrBot"
sh.Run "cmd /c python main.py", 0, False
