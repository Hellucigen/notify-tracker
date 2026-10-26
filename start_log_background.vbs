' 静默启动通知记录器（0 = 不显示窗口）。把本文件复制到"启动"文件夹即可开机自启。
Dim sh, dir
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "cmd /c """ & dir & "start_log_background.bat""", 0, False
