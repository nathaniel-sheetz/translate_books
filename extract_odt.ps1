Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead('C:\Users\Nathaniel\Cursor\translate_books\sermons_copy.odt')
$entry = $zip.GetEntry('content.xml')
$reader = New-Object System.IO.StreamReader($entry.Open())
$content = $reader.ReadToEnd()
$reader.Close()
$zip.Dispose()
$content | Out-File -FilePath 'C:\Users\Nathaniel\Cursor\translate_books\debug_sermons.xml' -Encoding UTF8
