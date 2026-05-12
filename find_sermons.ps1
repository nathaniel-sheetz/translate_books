$content = Get-Content 'C:\Users\Nathaniel\Cursor\translate_books\debug_sermons_text.txt' -Raw
$results = [regex]::Matches($content, '(?i)sermon[^\n\r]{0,40}')
Write-Host "Total matches:" $results.Count
$results | ForEach-Object { $_.Value } | Select-Object -First 50
