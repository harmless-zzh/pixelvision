<#
    PowerShell launcher for pixelvision.

    Prefer this over pixelvision.cmd when an argument may contain '%' (for example a
    URL with percent-encoded characters or a password): cmd.exe expands %VAR% inside
    arguments before the batch file can forward them, while PowerShell passes them
    literally.
#>
# No param() block on purpose: with ValueFromRemainingArguments, PowerShell still
# binds "-o" as an abbreviated common parameter (it matches -OutVariable), so
# `pixelvision.ps1 ... -o shots` fails before this script runs. $args has no such
# binding and preserves every token.
$Rest = @($args)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    Write-Error "pixelvision: no virtualenv at '$root\.venv'. Run: powershell -ExecutionPolicy Bypass -File '$root\scripts\bootstrap.ps1'"
    exit 2
}

& $py (Join-Path $root "scripts\capture.py") @Rest
exit $LASTEXITCODE
