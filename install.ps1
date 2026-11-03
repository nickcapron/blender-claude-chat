# crender installer for Windows.
#
# Run this in PowerShell:
#     irm https://raw.githubusercontent.com/nickcapron/blender-claude-chat/main/install.ps1 | iex
#
# It finds Blender 4.2 or newer, installs the crender add-on for each Blender version it
# finds, turns the add-on on, and offers to install Claude Code (Anthropic's official
# installer), which crender uses to talk to Claude with your own Claude account.
# It never asks for or stores a Claude password or key: you sign in to Claude yourself.
# Running it again updates crender.

$ErrorActionPreference = 'Stop'
# The crender release this installs (a tag, so what's installed is what was released).
$Release = 'v0.3.1'
$Repo = 'nickcapron/blender-claude-chat'
$AddonFiles = @('__init__.py', 'ui.py', 'session.py', 'blender_tools.py', 'claude_code.py',
                'bridge.py', 'mcp_bridge.py', 'install_deps.py', 'LICENSE', 'README.md')

$ProgressPreference = 'SilentlyContinue'

function Run-Blender($exe, [string[]]$arguments) {
    # Blender prints warnings on stderr; in Windows PowerShell with 'Stop', any stderr line
    # from a program would end this script, so run it with errors as plain output.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $exe @arguments 2>&1 | Out-String) } catch { return '' } finally { $ErrorActionPreference = $saved }
}
function Ask($prompt, $default) {
    try { return Read-Host $prompt } catch { return $default }  # non-interactive shells
}
function Say($text) { Write-Host $text -ForegroundColor Cyan }
function Warn($text) { Write-Host $text -ForegroundColor Yellow }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'Machine')
}
function Add-UserPath($dir) {
    # The registry value as stored, so entries like %USERPROFILE%\bin stay variables:
    # Environment.SetEnvironmentVariable would write every entry back expanded.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $parts = @($raw -split ';' | Where-Object { $_ })
        $expanded = @($parts | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) })
        if (($parts -notcontains $dir) -and ($expanded -notcontains $dir)) {
            $key.SetValue('Path', (@($parts) + $dir) -join ';', [Microsoft.Win32.RegistryValueKind]::ExpandString)
            # Writing the registry doesn't tell Windows the environment changed; setting
            # (and clearing) a variable this way does, so new windows see the new PATH.
            [Environment]::SetEnvironmentVariable('CRENDER_INSTALL', '1', 'User')
            [Environment]::SetEnvironmentVariable('CRENDER_INSTALL', $null, 'User')
        }
    } finally {
        $key.Close()
    }
    Refresh-Path
}

Say "`ncrender installer`n"

# An administrator window can belong to a different account, and then crender would
# install for that account instead of yours.
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Warn "This PowerShell is running as administrator. crender installs for one user, so a normal"
    Warn "(non-administrator) PowerShell window is better."
    $answer = Ask "Continue anyway? [y/N]" 'n'
    if ($answer -notmatch '^[yY]') { return }
}

# --- Find Blender --------------------------------------------------------------------
# Every blender.exe in the usual places, keyed by version (major.minor), which is also the
# name of that version's settings folder.
$candidates = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
    if (-not $root) { continue }
    $foundation = Join-Path $root 'Blender Foundation'
    if (Test-Path $foundation) {
        $candidates += Get-ChildItem $foundation -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'blender.exe' }
    }
    $candidates += Join-Path $root 'Steam\steamapps\common\Blender\blender.exe'
}
$onPath = Get-Command blender -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }

$blenders = @{}
foreach ($exe in ($candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)) {
    $out = Run-Blender $exe @('--version')
    if ($out -match 'Blender (\d+)\.(\d+)') {
        $ver = "$($Matches[1]).$($Matches[2])"
        if ([version]$ver -ge [version]'4.2' -and -not $blenders.ContainsKey($ver)) {
            $blenders[$ver] = $exe
        }
    }
}
# Versions that were opened at some point but whose blender.exe wasn't found (portable
# copies, for example) still get the add-on; it's switched on in Preferences.
$configRoot = Join-Path $env:APPDATA 'Blender Foundation\Blender'
$targets = @{}
foreach ($ver in $blenders.Keys) { $targets[$ver] = $blenders[$ver] }
if (Test-Path $configRoot) {
    Get-ChildItem $configRoot -Directory | Where-Object { $_.Name -match '^\d+\.\d+$' } | ForEach-Object {
        if ([version]$_.Name -ge [version]'4.2' -and -not $targets.ContainsKey($_.Name)) { $targets[$_.Name] = $null }
    }
}
if ($targets.Count -eq 0) {
    Warn "Blender 4.2 or newer wasn't found."
    Warn "Install it from https://www.blender.org/download/, open it once, then run this again."
    return
}
Say ("Blender found: " + (($targets.Keys | Sort-Object { [version]$_ }) -join ', '))

# Turning the add-on on saves Blender's preferences, which an open Blender could then
# overwrite when it closes.
while (Get-Process blender -ErrorAction SilentlyContinue) {
    Warn "Blender is open. Save your work and close Blender, then press Enter."
    $answer = Ask "(or type s to skip turning the add-on on automatically)" 's'
    if ($answer -match '^[sS]') { $blenders = @{}; break }
}

# --- Download crender ----------------------------------------------------------------
Say "Downloading crender $Release..."
$work = Join-Path $env:TEMP ("crender-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    $zip = Join-Path $work 'crender.zip'
    if ($env:CRENDER_SOURCE_ZIP) {
        Copy-Item $env:CRENDER_SOURCE_ZIP $zip  # local testing
    } else {
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/tags/$Release.zip" -OutFile $zip -UseBasicParsing
    }
    Expand-Archive $zip -DestinationPath (Join-Path $work 'src') -Force
    $src = Get-ChildItem (Join-Path $work 'src') -Recurse -Filter '__init__.py' |
        Where-Object { Select-String -Path $_.FullName -Pattern 'bl_info' -Quiet } |
        Select-Object -First 1
    if (-not $src) { Warn "The download didn't contain crender. Run this again later."; return }
    $srcDir = $src.DirectoryName

    # --- Install for each Blender version ----------------------------------------------
    $enabled = @(); $manual = @()
    foreach ($ver in ($targets.Keys | Sort-Object { [version]$_ })) {
        $addons = Join-Path $configRoot "$ver\scripts\addons"
        $dest = Join-Path $addons 'claude_chat'
        $item = Get-Item $dest -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Warn "Blender ${ver}: crender there is a link to a development copy; leaving it alone."
            continue
        }
        if ($item) { Remove-Item -Recurse -Force $dest }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        foreach ($f in $AddonFiles) {
            $from = Join-Path $srcDir $f
            if (Test-Path $from) { Copy-Item $from $dest }
        }
        Say "Blender ${ver}: crender installed."

        $exe = $blenders[$ver]
        if ($exe) {
            $py = "import addon_utils, bpy; m = addon_utils.enable('claude_chat', default_set=True); " +
                  "bpy.ops.wm.save_userpref(); print('CRENDER_ENABLED' if m else 'CRENDER_FAILED')"
            $out = Run-Blender $exe @('--background', '--python-expr', $py)
            if ($out -match 'CRENDER_ENABLED') { $enabled += $ver } else { $manual += $ver }
        } else {
            $manual += $ver
        }
    }
} finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

# --- Claude Code -----------------------------------------------------------------------
# crender talks to Claude through Claude Code, signed in with your own Claude account.
# Its installer puts it in ~\.local\bin but doesn't always add that to PATH.
$claudeBin = Join-Path $env:USERPROFILE '.local\bin'
$haveClaude = (Get-Command claude -ErrorAction SilentlyContinue) -or
              (Test-Path (Join-Path $claudeBin 'claude.exe'))
if (-not $haveClaude) {
    Write-Host ""
    Write-Host "crender talks to Claude through Claude Code, signed in with your own Claude account."
    $answer = Ask "Install Claude Code now, with Anthropic's official installer? [Y/n]" 'n'
    if ($answer -eq '' -or $answer -match '^[yY]') {
        # In its own PowerShell: that installer calls `exit` when something fails, which
        # would close this window, and it turns on strict mode for the whole session.
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
        if ($LASTEXITCODE -ne 0) {
            Warn "Claude Code didn't install. You can install it later from https://claude.com/claude-code."
        }
        Refresh-Path
    }
}
if ((Test-Path (Join-Path $claudeBin 'claude.exe')) -and -not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Add-UserPath $claudeBin
}

Write-Host ""
Say "Done. Last steps:"
Write-Host "  1. If you haven't signed in to Claude Code yet: open a new terminal, run  claude  and sign in."
if ($manual.Count -gt 0) {
    Write-Host ("  2. In Blender " + ($manual -join ', ') + ": Edit > Preferences > Add-ons, search crender, and tick it.")
    Write-Host "  3. Open Blender. The Claude chat opens by itself; after that, click the Claude button"
    Write-Host "     at the top of the 3D Viewport."
} else {
    Write-Host "  2. Open Blender. The Claude chat opens by itself; after that, click the Claude button"
    Write-Host "     at the top of the 3D Viewport."
}
Write-Host "To update crender later, run this installer again."
