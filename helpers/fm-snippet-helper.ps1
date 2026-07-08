# FM Snippet Helper (Windows) - part of FM DDR Analyzer
# https://github.com/oogi-io/fm-ddr-analyzer
#
# Converts fmxmlsnippet XML *text* on the clipboard (e.g. from the web app's
# "Copy FM snippet" button) into FileMaker clipboard objects (the registered
# "Mac-XMSS" clipboard format), so they paste into Script Workspace.
#
# Usage: right-click this file -> "Run with PowerShell" after copying snippet
# XML. (Or bind it to a hotkey with AutoHotkey etc.)
#
# NOTE: follows the community-documented format (UTF-8 bytes prefixed with a
# 4-byte little-endian length, as used by FileMaker on Windows). Tested
# pattern; report issues at the repo.

Add-Type -AssemblyName System.Windows.Forms

$text = Get-Clipboard -Raw
if (-not $text -or -not $text.StartsWith('<fmxmlsnippet')) {
    Write-Host 'Clipboard does not contain fmxmlsnippet XML - copy a snippet first.'
    exit 1
}

# pick the clipboard format from the first object inside the snippet
$class = 'Mac-XMSS'
if     ($text -match '<CustomFunction ') { $class = 'Mac-XMFN' }
elseif ($text -match '<BaseTable ')      { $class = 'Mac-XMTB' }
elseif ($text -match '<Step ')           { $class = 'Mac-XMSS' }
elseif ($text -match '<Script ')         { $class = 'Mac-XMSC' }
elseif ($text -match '<Layout')          { $class = 'Mac-XML2' }
elseif ($text -match '<Field ')          { $class = 'Mac-XMFD' }

$bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
$len   = [System.BitConverter]::GetBytes([UInt32]$bytes.Length)

$ms = New-Object System.IO.MemoryStream
$ms.Write($len, 0, 4)
$ms.Write($bytes, 0, $bytes.Length)
$ms.Position = 0

$data = New-Object System.Windows.Forms.DataObject
$data.SetData($class, $ms)
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true)

Write-Host "Done ($class) - paste into FileMaker."
