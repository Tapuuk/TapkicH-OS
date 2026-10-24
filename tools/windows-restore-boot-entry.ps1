# Put the "TapkicH-OS" UEFI boot entry back from Windows (PowerShell 5.1, run as administrator).
# Some firmware (seen on a Lenovo ThinkPad) drops OS-made boot entries, e.g. when Secure Boot is switched; then only
# Windows starts. This finds TapkicH-OS's own EFI partition (type ESP, holds
# \EFI\limine\limine_x64.efi; never Windows' ESP) and adds one firmware entry for it with bcdedit.
# It never changes {bootmgr}, Windows' ESP or any existing entry.
#   powershell -ExecutionPolicy Bypass -File tools\windows-restore-boot-entry.ps1            report only (-Check)
#   powershell -ExecutionPolicy Bypass -File tools\windows-restore-boot-entry.ps1 -Restore   add the entry (first in the list)
#   ... -Restore -Last                                                                        add it last instead
#   ... -Remove [-Force]                                                                      delete "TapkicH-OS" entries (undo a test)
# bcdedit prints its words in the Windows language ("description" is "Beschreibung" in German), so the entry is found
# by its VALUE: `bcdedit /enum firmware /v` is split into blocks at blank lines, the block that has a line ending in
# the value "TapkicH-OS" is ours, and its {guid} is the first {guid} in the block (the identifier line).
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Restore,
    [switch]$Remove,
    [switch]$Last,
    [switch]$Force
)

$EntryName = 'TapkicH-OS'
$EspGuid   = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
$EspLabel  = 'TAPKICH'
$LimineRel = '\EFI\limine\limine_x64.efi'
$GuidRx    = '\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}'

# Blocks of `bcdedit /enum firmware /v` text whose description value is exactly $Name: returns
# objects with Id (first {guid} of the block) and Lines (the block's lines).
function Get-FirmwareEntries {
    param([string[]]$Text, [string]$Name = 'TapkicH-OS')
    $lines = @($Text -split "\r?\n")
    $blocks = New-Object System.Collections.ArrayList
    $cur = New-Object System.Collections.ArrayList
    foreach ($l in $lines) {
        if ($l.Trim() -eq '') {
            if ($cur.Count) { [void]$blocks.Add(@($cur)); $cur = New-Object System.Collections.ArrayList }
        } else { [void]$cur.Add($l) }
    }
    if ($cur.Count) { [void]$blocks.Add(@($cur)) }
    $found = @()
    $valueRx = '(^|\s{2,})' + [regex]::Escape($Name) + '\s*$'
    foreach ($b in $blocks) {
        $has = $false
        foreach ($l in $b) { if ($l -match $valueRx -and $l -notmatch $GuidRx) { $has = $true; break } }
        if (-not $has) { continue }
        $id = $null
        foreach ($l in $b) { $m = [regex]::Match($l, $GuidRx); if ($m.Success) { $id = $m.Value; break } }
        if ($id) { $found += [pscustomobject]@{ Id = $id; Lines = $b } }
    }
    return ,$found
}

# First {guid} in any text (the new object's id in the output of `bcdedit /copy`, whatever the language).
function Get-FirstGuid {
    param([string]$Text)
    $m = [regex]::Match($Text, $GuidRx)
    if ($m.Success) { return $m.Value }
    return $null
}

function Invoke-Bcdedit {
    param([string[]]$BcdArgs)
    $out = (& bcdedit.exe @BcdArgs 2>&1 | Out-String)
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $out }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SecureBootState {
    try { if (Confirm-SecureBootUEFI) { return 'on' } else { return 'off' } }
    catch { return 'unknown' }
}

function Get-OurEntries {
    $r = Invoke-Bcdedit @('/enum', 'firmware', '/v')
    if ($r.ExitCode -ne 0) { throw "bcdedit /enum firmware failed (is Windows started in UEFI mode?): $($r.Output.Trim())" }
    return [pscustomobject]@{ Entries = (Get-FirmwareEntries -Text $r.Output -Name $EntryName) }
}

# Attach a partition to a new empty folder, return what $Script says about it, always detach and delete the folder.
function Use-TempFolderMount {
    param($Part, [scriptblock]$Script)
    $dir = Join-Path ([IO.Path]::GetTempPath()) ('tapkich-esp-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir | Out-Null
    $attached = $false
    try {
        Add-PartitionAccessPath -DiskNumber $Part.DiskNumber -PartitionNumber $Part.PartitionNumber -AccessPath $dir
        $attached = $true
        return (& $Script $dir)
    } finally {
        if ($attached) {
            Remove-PartitionAccessPath -DiskNumber $Part.DiskNumber -PartitionNumber $Part.PartitionNumber -AccessPath $dir -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue
    }
}

# Returns the one partition that is our ESP, or $null (after saying why).
function Find-OurEsp {
    $esps = @(Get-Partition | Where-Object { $_.GptType -eq $EspGuid })
    $candidates = @()
    foreach ($p in $esps) {
        # The label (TAPKICH) is not required: Windows does not always report labels of EFI partitions. What decides
        # is Limine inside and no Windows Boot Manager.
        try {
            $seen = Use-TempFolderMount $p {
                param($dir)
                [pscustomobject]@{
                    Limine  = (Test-Path -LiteralPath (Join-Path $dir $LimineRel) -PathType Leaf)
                    Windows = (Test-Path -LiteralPath (Join-Path $dir '\EFI\Microsoft\Boot\bootmgfw.efi') -PathType Leaf)
                }
            }
        } catch {
            Write-Host "Could not look inside disk $($p.DiskNumber) partition $($p.PartitionNumber): $($_.Exception.Message)"
            continue
        }
        if ($seen.Limine -and -not $seen.Windows) { $candidates += $p }
    }
    if ($candidates.Count -eq 0) {
        Write-Host "Could not find TapkicH-OS's EFI partition."
        Write-Host "It should be an EFI System partition (normally labelled $EspLabel) that holds $LimineRel."
        Write-Host "Nothing was changed. Was TapkicH-OS installed on this computer? (From Linux: lsblk -o NAME,PARTLABEL)"
        return $null
    }
    if ($candidates.Count -gt 1) {
        Write-Host "Found $($candidates.Count) EFI partitions that look like TapkicH-OS's:"
        $candidates | ForEach-Object { Write-Host "  disk $($_.DiskNumber) partition $($_.PartitionNumber)" }
        Write-Host "The tool will not guess which one to use. Nothing was changed."
        return $null
    }
    return $candidates[0]
}

function Show-OurEntry {
    param($Entries)
    if (-not $Entries.Count) { Write-Host "No firmware entry named $EntryName."; return }
    foreach ($e in $Entries) { Write-Host ''; $e.Lines | ForEach-Object { Write-Host "  $_" } }
}

function Show-SecureBootAdvice {
    param([string]$State)
    switch ($State) {
        'on' {
            Write-Host "Secure Boot is ON."
            Write-Host "  The TapkicH-OS boot menu (Limine) is not signed and is refused while Secure Boot is on,"
            Write-Host "  unless 'sudo tapkich secureboot setup' was done in TapkicH-OS. To turn it off: restart, open the"
            Write-Host "  firmware setup (ThinkPad: F1 at power-on), go to the Security (or Boot) tab, set Secure Boot to Off, save."
        }
        'off'     { Write-Host "Secure Boot is off." }
        default   { Write-Host "Secure Boot: could not be read (Windows may not be started in UEFI mode)." }
    }
}

function Main {
    if (-not (Test-Admin)) {
        Write-Host "This tool must run as administrator (it reads disks and the firmware boot list)."
        Write-Host "Start one: press the Windows key, type 'powershell', right-click 'Windows PowerShell', choose"
        Write-Host "'Run as administrator', then run the same command again."
        return 1
    }
    $ErrorActionPreference = 'Stop'
    try {
        return (Run-Tool)
    } catch {
        Write-Host "Stopped: $($_.Exception.Message)"
        return 1
    }
}

function Run-Tool {
    Show-SecureBootAdvice (Get-SecureBootState)

    if ($Remove) {
        $info = Get-OurEntries
        if (-not $info.Entries.Count) { Write-Host "No firmware entry named $EntryName. Nothing to remove."; return 0 }
        Show-OurEntry $info.Entries
        if (-not $Force) {
            $a = Read-Host "Delete $($info.Entries.Count) firmware entry/entries named $EntryName ? Type yes to continue"
            if ($a -ne 'yes') { Write-Host "Nothing was changed."; return 0 }
        }
        foreach ($e in $info.Entries) {
            $r = Invoke-Bcdedit @('/delete', $e.Id)
            if ($r.ExitCode -ne 0) { Write-Host "Could not delete $($e.Id): $($r.Output.Trim())"; return 1 }
            Write-Host "Deleted $($e.Id)."
        }
        return 0
    }

    $esp = Find-OurEsp
    if (-not $esp) { return 1 }
    Write-Host "TapkicH-OS's EFI partition: disk $($esp.DiskNumber) partition $($esp.PartitionNumber) ($([math]::Round($esp.Size/1MB)) MiB)."
    $info = Get-OurEntries

    if (-not $Restore) {
        if ($info.Entries.Count) {
            Write-Host "The firmware boot list has a $EntryName entry:"
            Show-OurEntry $info.Entries
            Write-Host ''
            Write-Host "Nothing to do. If it does not start, check Secure Boot (above)."
        } else {
            Write-Host "The firmware boot list has NO $EntryName entry."
            Write-Host "Next: turn Secure Boot off (see above), then run this tool again with -Restore."
        }
        return 0
    }

    if ($info.Entries.Count) {
        Write-Host "The firmware boot list already has a $EntryName entry; nothing to add."
        Show-OurEntry $info.Entries
        return 0
    }

    $guid = $null
    $letterAdded = $false
    try {
        $r = Invoke-Bcdedit @('/copy', '{bootmgr}', '/d', $EntryName)
        $guid = Get-FirstGuid $r.Output
        if ($r.ExitCode -ne 0 -or -not $guid) { $guid = $null; throw "bcdedit /copy failed: $($r.Output.Trim())" }

        # The BCD stores the partition, not the letter, so a temporary letter is enough.
        $letter = (Get-Partition -DiskNumber $esp.DiskNumber -PartitionNumber $esp.PartitionNumber).DriveLetter
        if (-not $letter -or $letter -eq [char]0) {
            Add-PartitionAccessPath -DiskNumber $esp.DiskNumber -PartitionNumber $esp.PartitionNumber -AssignDriveLetter
            $letterAdded = $true
            $letter = (Get-Partition -DiskNumber $esp.DiskNumber -PartitionNumber $esp.PartitionNumber).DriveLetter
        }
        if (-not $letter -or $letter -eq [char]0) { throw "No drive letter could be given to the EFI partition." }

        $where = if ($Last) { '/addlast' } else { '/addfirst' }
        $steps = @(
            ,@('/set', $guid, 'device', "partition=${letter}:")
            ,@('/set', $guid, 'path', $LimineRel)
            ,@('/set', '{fwbootmgr}', 'displayorder', $guid, $where)
        )
        foreach ($s in $steps) {
            $r = Invoke-Bcdedit $s
            if ($r.ExitCode -ne 0) { throw "bcdedit $($s -join ' ') failed: $($r.Output.Trim())" }
        }
    } catch {
        Write-Host "Failed: $($_.Exception.Message)"
        if ($guid) {
            $d = Invoke-Bcdedit @('/delete', $guid)
            if ($d.ExitCode -eq 0) { Write-Host "The half-made entry $guid was deleted again. Nothing else was changed." }
            else { Write-Host "Could not delete the half-made entry $guid ($($d.Output.Trim())). Delete it with: bcdedit /delete $guid" }
        }
        return 1
    } finally {
        if ($letterAdded) {
            $dl = (Get-Partition -DiskNumber $esp.DiskNumber -PartitionNumber $esp.PartitionNumber).DriveLetter
            if ($dl) {
                Remove-PartitionAccessPath -DiskNumber $esp.DiskNumber -PartitionNumber $esp.PartitionNumber -AccessPath "${dl}:\" -ErrorAction SilentlyContinue
            }
        }
    }

    Write-Host "Added the $EntryName firmware entry $guid ($(if ($Last) { 'last' } else { 'first' }) in the boot order)."
    Show-OurEntry (Get-OurEntries).Entries
    Write-Host ''
    Write-Host "Restart to try it. If Windows starts anyway, pick $EntryName from the firmware boot menu (ThinkPad: F12)."
    return 0
}

if ($MyInvocation.InvocationName -ne '.') { exit (Main) }
