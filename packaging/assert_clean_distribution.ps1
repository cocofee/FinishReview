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
    "coverage",
    "psutil",
    "pyreadline3",
    "ruff",
    "tomli"
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

# Model weights are configured by the user, never shipped.
$ForbiddenModelExtensions = @(".onnx", ".pt", ".pth", ".engine", ".tflite", ".pdparams")
$FoundModels = @(
    Get-ChildItem -LiteralPath $ResolvedAppDir -Recurse -Force -File |
        Where-Object { $ForbiddenModelExtensions -contains $_.Extension.ToLowerInvariant() } |
        ForEach-Object { $_.FullName }
)

if ($FoundModels.Count -gt 0) {
    throw "Distribution contains model weights: $($FoundModels -join ', ')"
}

Write-Host "Distribution boundary check passed: $ResolvedAppDir"
