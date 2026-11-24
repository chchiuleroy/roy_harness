# S-1 OCR candidate: Windows built-in OCR (Windows.Media.Ocr), no download. Offline.
# Usage: powershell -File ocr_winrt.ps1 -Out <json> -Lang <tag or ''> <image paths...>
param([string]$Out, [string]$Lang = "", [Parameter(ValueFromRemainingArguments = $true)][string[]]$Images)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$langs = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
$result = [ordered]@{ available_languages = $langs; pages = @() }
if ($Lang -eq "" -or $Lang -eq "auto") {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
} else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
}
if ($null -eq $engine) { $result["error"] = "no engine for '$Lang'" }
else {
    $result["engine_language"] = $engine.RecognizerLanguage.LanguageTag
    foreach ($img in $Images) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($img)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocr = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
        $result.pages += [ordered]@{ image = $img; ms = $sw.ElapsedMilliseconds; w = $bmp.PixelWidth; h = $bmp.PixelHeight; text = $ocr.Text }
    }
}
[System.IO.File]::WriteAllText($Out, ($result | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("languages: " + ($langs -join ","))
