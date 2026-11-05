#Requires -Version 5.1
<#
.SYNOPSIS
    Expose this skill to local agents by linking it into their skill directories.

.DESCRIPTION
    DSH loads skills from ~/.agents/skills and ~/.dsh/skills; Claude Code uses ~/.claude/skills.
    This script creates a directory junction (no admin rights needed, unlike a symlink) from
    each existing, relevant directory to this repo, then bootstraps the virtualenv.

    The link points at the checkout, so `git pull` upgrades the installed skill.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipBootstrap,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Links = @(
    (Join-Path $HOME ".agents\skills\pixelvision"),
    (Join-Path $HOME ".dsh\skills\pixelvision")
)
if (Test-Path (Join-Path $HOME ".claude")) {
    $Links += (Join-Path $HOME ".claude\skills\pixelvision")
}

foreach ($link in $Links) {
    if ($Remove) {
        if (Test-Path $link) {
            $item = Get-Item $link -Force
            if ($item.LinkType -eq "Junction" -or $item.LinkType -eq "SymbolicLink") {
                Remove-Item $link -Force
                Write-Host "removed  $link"
            } else {
                Write-Warning "not a link, leaving alone: $link"
            }
        }
        continue
    }

    $parent = Split-Path -Parent $link
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.LinkType -eq "Junction" -or $item.LinkType -eq "SymbolicLink") {
            Remove-Item $link -Force
        } else {
            Write-Warning "already exists and is not a link, skipping: $link"
            continue
        }
    }
    New-Item -ItemType Junction -Path $link -Target $Root | Out-Null
    Write-Host "linked   $link -> $Root"
}

if (-not $Remove -and -not $SkipBootstrap) {
    & (Join-Path $PSScriptRoot "bootstrap.ps1")
}
