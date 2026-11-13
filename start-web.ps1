$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildUi = -not (Test-Path 'ui\dist\index.html')
if (-not $buildUi) {
    $builtAt = (Get-Item 'ui\dist\index.html').LastWriteTimeUtc
    $sourceFiles = @((Get-ChildItem 'ui\src' -File -Recurse)) + @((Get-Item 'ui\index.html','ui\vite.config.js','ui\package.json'))
    $buildUi = @($sourceFiles | Where-Object { $_.LastWriteTimeUtc -gt $builtAt }).Count -gt 0
}
if ($buildUi) {
    Push-Location ui
    try {
        if (-not (Test-Path 'node_modules\vite\package.json')) {
            & npm.cmd install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
        }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'UI build failed.' }
    } finally { Pop-Location }
}
& .\.venv\Scripts\python.exe -m veritas.web
