[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$t = '<toast><visual><binding template="ToastGeneric"><text>Recorder test</text><text>notification appeared here</text></binding></visual></toast>'
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($t)
$toast = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PowerShell').Show($toast)
Write-Output 'toast-sent'
