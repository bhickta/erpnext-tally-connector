import argparse
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .clients import BridgeRequestError, FrappeClient, TallyClient
from .config import BridgeConfig
from .controller import ControlCentre
from .http_server import BridgeHTTPServer
from .service import SyncService


def _default_config_path():
	local_app_data = os.getenv("LOCALAPPDATA")
	if os.name == "nt" and local_app_data:
		return str(Path(local_app_data) / "Express Tally Control Centre" / "tally-bridge.json")
	return "tally-bridge.json"


def _parser():
	parser = argparse.ArgumentParser(description="Run registered ERPNext to TallyPrime flows")
	parser.add_argument("--config", default=None, help="Path to bridge JSON configuration")
	parser.add_argument("--verbose", action="store_true")
	subparsers = parser.add_subparsers(dest="command")
	sync = subparsers.add_parser("sync", help="Run one synchronization batch")
	sync.add_argument("--limit", type=int)
	subparsers.add_parser("status", help="Check Tally connectivity and loaded company")
	serve = subparsers.add_parser("serve", help="Run the Windows Control Centre")
	serve.add_argument("--no-poll", action="store_true", help="Disable automatic sync")
	serve.add_argument("--no-browser", action="store_true", help="Do not open the Control Centre UI")
	subparsers.add_parser("self-test", help=argparse.SUPPRESS)
	api = subparsers.add_parser("api", help="Call any Frappe REST or whitelisted API path")
	api.add_argument("method", choices=("GET", "POST", "PUT", "DELETE"))
	api.add_argument("path")
	api.add_argument("--data", default="{}", help="JSON request object")
	return parser


def main(argv=None):
	args = _parser().parse_args(argv)
	args.config = args.config or _default_config_path()
	command = args.command or "serve"
	handlers = []
	if command == "serve":
		log_path = Path(args.config).resolve().with_name("tally-control-centre.log")
		log_path.parent.mkdir(parents=True, exist_ok=True)
		handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
	if sys.stderr:
		handlers.append(logging.StreamHandler())
	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
		handlers=handlers or None,
	)
	try:
		if command == "self-test":
			web_root = Path(__file__).resolve().with_name("web")
			return 0 if (web_root / "index.html").is_file() else 2
		if command == "serve":
			controller = ControlCentre(args.config)
			config = controller.config()
			if getattr(args, "no_poll", False) and config.auto_sync_enabled:
				controller.set_auto_sync(False)
			server = BridgeHTTPServer((config.listen_host, config.listen_port), controller)
			url = f"http://{config.listen_host}:{config.listen_port}/"
			logging.info("Tally Control Centre listening on %s", url)
			if config.open_browser_on_start and not getattr(args, "no_browser", False):
				threading.Timer(0.6, webbrowser.open, args=(url,)).start()
			server.serve_forever()
			return 0

		config = BridgeConfig.load(args.config)
		frappe = FrappeClient(
			config.frappe_url,
			config.api_key,
			config.api_secret,
			config.request_timeout_seconds,
		)
		tally = TallyClient(config.tally_url, config.request_timeout_seconds)
		service = SyncService(config, frappe, tally)
		if command == "sync":
			result = service.sync_once(limit=args.limit).to_dict()
			print(json.dumps(result, indent=2))
			return 1 if result["error"] or result["failed"] else 0
		if command == "status":
			print(json.dumps(service.health(), indent=2))
			return 0
		if command == "api":
			print(json.dumps(frappe.request(args.method, args.path, json.loads(args.data)), indent=2))
			return 0
	except (BridgeRequestError, OSError, ValueError, json.JSONDecodeError) as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
