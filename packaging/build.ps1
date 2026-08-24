param(
    [string]$FfmpegPath,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Artifacts = Join-Path $RepoRoot "artifacts"
$DistRoot = Join-Path $Artifacts "dist"
$BuildRoot = Join-Path $Artifacts "build"
$Spec = Join-Path $PSScriptRoot "FinishReview.spec"
$AppName = "FinishReviewConsole"
$AppDir = Join-Path $DistRoot $AppName
$RepoPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ResolvedPython = if ($PythonPath) {
    if (Test-Path -LiteralPath $PythonPath -PathType Leaf) {
        (Resolve-Path -LiteralPath $PythonPath).Path
    } else {
        (Get-Command $PythonPath -ErrorAction Stop).Source
    }
} elseif (Test-Path -LiteralPath $RepoPython -PathType Leaf) {
    (Resolve-Path -LiteralPath $RepoPython).Path
} else {
    Write-Warning "Repository .venv not found; using the active PATH Python."
    (Get-Command python -ErrorAction Stop).Source
}
$ResolvedFfmpeg = if ($FfmpegPath) {
    (Resolve-Path -LiteralPath $FfmpegPath).Path
} else {
    (Get-Command ffmpeg -ErrorAction Stop).Source
}

function Test-PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$ChildPath,
        [Parameter(Mandatory = $true)][string]$RootPath
    )

    $child = [IO.Path]::GetFullPath($ChildPath).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($RootPath).TrimEnd('\')
    return $child.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $child.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Stage-DistributionInput {
    param(
        [Parameter(Mandatory = $true)][string]$InputPath,
        [Parameter(Mandatory = $true)][string]$StagedName
    )

    if (-not (Test-PathUnderRoot -ChildPath $InputPath -RootPath $AppDir)) {
        return $InputPath
    }

    $stageDir = Join-Path $Artifacts "build-input\$AppName"
    New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
    $stagedPath = Join-Path $stageDir $StagedName
    Copy-Item -LiteralPath $InputPath -Destination $stagedPath -Force
    Write-Host "Staged distribution input: $InputPath -> $stagedPath"
    return $stagedPath
}

$ResolvedFfmpeg = Stage-DistributionInput -InputPath $ResolvedFfmpeg -StagedName "ffmpeg.exe"

$env:FINISH_REVIEW_FFMPEG = $ResolvedFfmpeg

Push-Location $RepoRoot
try {
    Write-Host "Build Python: $ResolvedPython"
    & $ResolvedPython -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $BuildRoot $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
    Remove-Item Env:FINISH_REVIEW_FFMPEG -ErrorAction SilentlyContinue
}

& (Join-Path $PSScriptRoot "assert_clean_distribution.ps1") -AppDir $AppDir

Write-Host "Built: $AppDir"
