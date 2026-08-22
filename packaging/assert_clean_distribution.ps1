param(
    [Parameter(Mandatory = $true)]
    [string]$AppDir
)

$ErrorActionPreference = "Stop"
$ResolvedAppDir = (Resolve-Path -LiteralPath $AppDir).Path
$ForbiddenRelativePaths = @(
    "RaceData",
    "logs",
    "config.json",
    "global_config.json"
)

$Found = @(
    foreach ($RelativePath in $ForbiddenRelativePaths) {
        $Candidate = Join-Path $ResolvedAppDir $RelativePath
        if (Test-Path -LiteralPath $Candidate) {
            $RelativePath
        }
    }
)

if ($Found.Count -gt 0) {
    throw "Distribution contains runtime state: $($Found -join ', ')"
}

Write-Host "Distribution runtime-state check passed: $ResolvedAppDir"
