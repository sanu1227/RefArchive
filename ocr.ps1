param([Parameter(Mandatory=$true)][string]$ImagePath, [string]$Language = 'ko-KR')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$taskAsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 } | Select-Object -First 1
function Wait-Task($Operation, $ResultType) {
    $taskAwait = $taskAsTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $taskAwait.GetAwaiter().GetResult()
}
$taskFile = Wait-Task ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$taskStream = Wait-Task ($taskFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
    $taskDecoder = Wait-Task ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($taskStream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $taskBitmap = Wait-Task ($taskDecoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $taskConverted = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($taskBitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
    try {
        $taskLanguage = New-Object Windows.Globalization.Language($Language)
        $taskEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($taskLanguage)
        if ($null -eq $taskEngine) { throw 'OCR language pack unavailable' }
        $taskResult = Wait-Task ($taskEngine.RecognizeAsync($taskConverted)) ([Windows.Media.Ocr.OcrResult])
        @{ text = ($taskResult.Lines | ForEach-Object { $_.Text }) -join "`n" } | ConvertTo-Json -Compress
    } finally {
        $taskConverted.Dispose()
        $taskBitmap.Dispose()
    }
} finally { $taskStream.Dispose() }
