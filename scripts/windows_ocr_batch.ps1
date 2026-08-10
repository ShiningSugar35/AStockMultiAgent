param(
    [string]$InputPath,
    [string]$ManifestPath
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1
    } |
    Select-Object -First 1

function Await-WinRt {
    param(
        [Parameter(Mandatory = $true)]
        $AsyncOperation,
        [Parameter(Mandatory = $true)]
        [Type]$ResultType
    )
    $method = $script:asTaskGeneric.MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($AsyncOperation))
    $task.Wait()
    return $task.Result
}

function Invoke-OcrFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        $Engine
    )
    try {
        $resolved = (Resolve-Path -LiteralPath $Path).Path
        $file = Await-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolved)) ([Windows.Storage.StorageFile])
        $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        try {
            $decoder = Await-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            try {
                $result = Await-WinRt ($Engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                return [ordered]@{
                    id = $Id
                    status = 'SUCCEEDED'
                    engine = 'windows-media-ocr'
                    language = $Engine.RecognizerLanguage.LanguageTag
                    text = $result.Text
                    line_count = @($result.Lines).Count
                    error = $null
                }
            }
            finally {
                if ($null -ne $bitmap) { $bitmap.Dispose() }
            }
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
    catch {
        return [ordered]@{
            id = $Id
            status = 'FAILED'
            engine = 'windows-media-ocr'
            language = $Engine.RecognizerLanguage.LanguageTag
            text = ''
            line_count = 0
            error = $_.Exception.GetType().Name
        }
    }
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    throw 'WINDOWS_OCR_ENGINE_UNAVAILABLE'
}

$items = @()
if (-not [string]::IsNullOrWhiteSpace($ManifestPath)) {
    $raw = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $items = @($raw)
}
elseif (-not [string]::IsNullOrWhiteSpace($InputPath)) {
    $items = @([pscustomobject]@{ id = 'single'; path = $InputPath })
}
else {
    throw 'INPUT_PATH_OR_MANIFEST_REQUIRED'
}

$results = foreach ($item in $items) {
    Invoke-OcrFile -Id ([string]$item.id) -Path ([string]$item.path) -Engine $engine
}
ConvertTo-Json -InputObject @($results) -Compress -Depth 4
