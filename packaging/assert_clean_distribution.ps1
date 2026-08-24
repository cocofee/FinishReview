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
    "finish_review_config.json",
    "global_config.json"
)
$ForbiddenDependencyNames = @(
    "psutil",
    "pyreadline3"
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

$FoundDependencies = @(
    foreach ($Item in Get-ChildItem -LiteralPath $ResolvedAppDir -Recurse -Force) {
        foreach ($DependencyName in $ForbiddenDependencyNames) {
            if ($Item.Name -match "(?i)^$([regex]::Escape($DependencyName))([.\-]|$)") {
                $Item.FullName
                break
            }
        }
    }
)

if ($FoundDependencies.Count -gt 0) {
    throw "Distribution contains unexpected dependencies: $($FoundDependencies -join ', ')"
}

Write-Host "Distribution boundary check passed: $ResolvedAppDir"
