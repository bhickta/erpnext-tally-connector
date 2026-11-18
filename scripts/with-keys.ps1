# Set the ERPNext API keys on deverp, then run a bridge command.
#
# The dev site is periodically restored from production data, which wipes the
# User.api_key / __Auth rows. Every run therefore re-establishes them first, so
# a 401 can never be mistaken for a code bug.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\with-keys.ps1 -Command "python -m express_tally.bridge sync --limit 20"
#   powershell -ExecutionPolicy Bypass -File scripts\with-keys.ps1 -KeysOnly

param(
	[string]$Command = "",
	[switch]$KeysOnly,
	[string]$PemPath = "$env:LOCALAPPDATA\Temp\opencode\prodSRV.pem",
	[string]$Server = "bhickta@13.205.90.92",
	[string]$Bench = "~/development/dev-frappe-bench",
	[string]$Site = "deverp.srvelectricals.in",
	[string]$ApiKey = "424426d0b1340d3",
	[string]$ApiSecret = "srvTally2026x9f"
)

if (-not (Test-Path $PemPath)) {
	throw "SSH key not found: $PemPath"
}

$keyScript = @"
import frappe
from frappe.utils.password import set_encrypted_password
frappe.db.set_value("User", "Administrator", "api_key", "$ApiKey", update_modified=False)
set_encrypted_password("User", "Administrator", "$ApiSecret", "api_secret")
frappe.db.commit()
print("SETOK", frappe.db.get_value("User", "Administrator", "api_key"))
"@

$remote = "$Bench && bench --site $Site console"

# Write the helper on the server, run it, and report the key it stored.
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($keyScript))
& ssh -i $PemPath -o BatchMode=yes $Server "echo $encoded | base64 -d > /tmp/set_keys.py; cd $Bench && bench --site $Site console < /tmp/set_keys.py 2>&1 | grep SETOK"
if ($LASTEXITCODE -ne 0) { throw "Could not set API keys on $Site" }

if ($KeysOnly) { return }

if ($Command) {
	Invoke-Expression $Command
}
