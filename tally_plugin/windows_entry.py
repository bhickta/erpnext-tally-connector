"""PyInstaller entry point for the standalone Windows bridge."""

from express_tally.bridge.__main__ import main


if __name__ == "__main__":
	raise SystemExit(main())
