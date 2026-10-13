"""Package the stdlib-only bridge with an official Windows embedded Python ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build(runtime, output):
	repo = Path(__file__).resolve().parents[1]
	output.parent.mkdir(parents=True, exist_ok=True)
	manifest = {}
	with ZipFile(output, "w", ZIP_DEFLATED) as archive:
		with ZipFile(runtime) as embedded:
			if "python.exe" not in embedded.namelist() or "pythonw.exe" not in embedded.namelist():
				raise ValueError("Expected the official Windows embeddable Python distribution")
			for name in embedded.namelist():
				if Path(name).is_absolute() or ".." in Path(name).parts:
					raise ValueError("Unsafe path in Python runtime ZIP")
				archive.writestr(name, embedded.read(name))
		files = [
			repo / "license.txt",
			repo / "express_tally" / "__init__.py",
			repo / "express_tally" / "tests" / "__init__.py",
		]
		files.extend((repo / "express_tally" / "bridge").rglob("*.py"))
		files.extend(
			path for path in (repo / "express_tally" / "bridge" / "web").rglob("*") if path.is_file()
		)
		files.extend(
			repo / "express_tally" / "tests" / name for name in ("test_bridge.py", "test_payments.py")
		)
		files.append(repo / "tally_plugin" / "install-portable.ps1")
		for path in files:
			if "__pycache__" in path.parts:
				continue
			name = (
				"install-portable.ps1"
				if path.name == "install-portable.ps1"
				else path.relative_to(repo).as_posix()
			)
			archive.write(path, name)
			manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
		archive.writestr(
			"start-control-centre.cmd",
			'@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0pythonw.exe" -m express_tally.bridge %*\r\n',
		)
		archive.writestr("source-manifest.json", json.dumps(manifest, indent=2))
	checksum = hashlib.sha256(output.read_bytes()).hexdigest()
	output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n")
	print(output)
	print(f"SHA256 {checksum}")


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--python-runtime", type=Path, required=True)
	parser.add_argument("--output", type=Path, required=True)
	args = parser.parse_args()
	build(args.python_runtime, args.output)
