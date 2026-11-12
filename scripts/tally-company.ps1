# Drive TallyPrime's own windows from PowerShell.
#
# Tally Prime exposes an HTTP gateway, but it refuses to create a company over
# HTTP when no company is open ("Unknown Request"). Creating a company and
# switching company therefore has to go through the UI. This script does that
# with window messages so it works without a human at the keyboard.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\tally-company.ps1 -Action new  -Name ZZTEST5
#   powershell -ExecutionPolicy Bypass -File scripts\tally-company.ps1 -Action load -Name ZZTEST5
#   powershell -ExecutionPolicy Bypass -File scripts\tally-company.ps1 -Action health
#
# Verified against TallyPrime 1.1.7.1. Coordinates target the default
# 1920x1080 layout of the "Select Company" panel.

param(
	[Parameter(Mandatory = $true)]
	[ValidateSet('new', 'load', 'health', 'shot')]
	[string]$Action,
	[string]$Name = "",
	[string]$Out = "",
	[int]$CreateCompanyX = 1150,
	[int]$CreateCompanyY = 261,
	[int]$SelectCompanyY = 289
)

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class TallyUI {
	public delegate bool EnumProc(IntPtr h, IntPtr l);
	[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
	[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
	[DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
	[DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
	[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
	[DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
	[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
	[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
	[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p);
	[DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
	[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
	[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
	[DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);

	public static IntPtr Find(uint target) {
		IntPtr found = IntPtr.Zero;
		EnumWindows((h, l) => {
			uint pid; GetWindowThreadProcessId(h, out pid);
			if (pid == target) {
				var c = new StringBuilder(128); GetClassName(h, c, 128);
				if (c.ToString() == "Ganeshji") { found = h; return false; }
			}
			return true;
		}, IntPtr.Zero);
		return found;
	}

	public static void Focus(IntPtr h) {
		uint pid; uint tid = GetWindowThreadProcessId(h, out pid);
		uint mine = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
		AttachThreadInput(mine, tid, true);
		ShowWindow(h, 9); BringWindowToTop(h); SetForegroundWindow(h);
		AttachThreadInput(mine, tid, false);
	}

	// Text fields in Tally take WM_CHAR only; sending WM_KEYDOWN as well
	// doubles every character.
	public static void Type(IntPtr h, string text) {
		foreach (char ch in text) {
			PostMessage(h, 0x0102, (IntPtr)ch, (IntPtr)1);
			System.Threading.Thread.Sleep(60);
		}
	}

	public static void Click(int x, int y) {
		SetCursorPos(x, y);
		System.Threading.Thread.Sleep(250);
		mouse_event(0x0002, 0, 0, 0, IntPtr.Zero);
		System.Threading.Thread.Sleep(100);
		mouse_event(0x0004, 0, 0, 0, IntPtr.Zero);
	}
}
"@

function Get-TallyWindow {
	$proc = Get-Process tally -ErrorAction SilentlyContinue | Select-Object -First 1
	if ($null -eq $proc) { return [IntPtr]::Zero }
	return [TallyUI]::Find([uint32]$proc.Id)
}

function Send-Special([int]$vk) {
	$h = Get-TallyWindow
	[void][TallyUI]::PostMessage($h, 0x0100, [IntPtr]$vk, [IntPtr]1)
	Start-Sleep -Milliseconds 90
	[void][TallyUI]::PostMessage($h, 0x0101, [IntPtr]$vk, [IntPtr]0xC0000001)
	Start-Sleep -Milliseconds 350
}

function Send-Ctrl([string]$letter) {
	$vk = [byte][char]::ToUpper($letter[0])
	[TallyUI]::keybd_event(0x11, 0, 0, [IntPtr]::Zero)
	Start-Sleep -Milliseconds 90
	[TallyUI]::keybd_event($vk, 0, 0, [IntPtr]::Zero)
	Start-Sleep -Milliseconds 90
	[TallyUI]::keybd_event($vk, 0, 2, [IntPtr]::Zero)
	Start-Sleep -Milliseconds 90
	[TallyUI]::keybd_event(0x11, 0, 2, [IntPtr]::Zero)
	Start-Sleep -Milliseconds 500
}

function Save-Shot([string]$path) {
	$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
	$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
	$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
	$graphics.CopyFromScreen($bounds.X, $bounds.Y, 0, 0, $bitmap.Size)
	$bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
	$graphics.Dispose(); $bitmap.Dispose()
	Write-Output "shot: $path"
}

$window = Get-TallyWindow

switch ($Action) {
	'health' {
		if ($window -eq [IntPtr]::Zero) { Write-Output "tally not running" }
		else { Write-Output "tally running window=$window" }
	}
	'shot' { Save-Shot $Out }
	'new' {
		if ($window -eq [IntPtr]::Zero) { Write-Output "tally not running"; exit 1 }
		[TallyUI]::Focus($window); Start-Sleep -Milliseconds 500
		Send-Special 114                              # F3 opens the company panel
		Start-Sleep -Milliseconds 800
		[TallyUI]::Click($CreateCompanyX, $CreateCompanyY)
		Start-Sleep -Milliseconds 1200
		Send-Special 13                               # open Company Creation
		Start-Sleep -Milliseconds 1500
		[TallyUI]::Type($window, $Name)
		Start-Sleep -Milliseconds 800
		[TallyUI]::Focus($window)
		Send-Ctrl 'a'                                 # Accept
		Start-Sleep -Milliseconds 2500
		if ($Out -ne "") { Save-Shot $Out }
	}
	'load' {
		if ($window -eq [IntPtr]::Zero) { Write-Output "tally not running"; exit 1 }
		[TallyUI]::Focus($window); Start-Sleep -Milliseconds 500
		Send-Special 114
		Start-Sleep -Milliseconds 800
		[TallyUI]::Click($CreateCompanyX, $SelectCompanyY)
		Start-Sleep -Milliseconds 1200
		[TallyUI]::Type($window, $Name)
		Start-Sleep -Milliseconds 700
		Send-Special 13
		Start-Sleep -Milliseconds 2000
		if ($Out -ne "") { Save-Shot $Out }
	}
}
