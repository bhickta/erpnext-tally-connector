import json
import logging
import mimetypes
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import ControlCentre


LOGGER = logging.getLogger("erpnext_tally_bridge")
WEB_ROOT = Path(__file__).resolve().with_name("web")


class BridgeHTTPServer(ThreadingHTTPServer):
	def __init__(self, address, controller, web_root=None):
		self.controller = controller
		# Compatibility for callers that still pass a bare SyncService.
		self.service = controller
		self.web_root = Path(web_root or WEB_ROOT).resolve()
		self._sync_state_lock = threading.Lock()
		self._sync_thread = None
		self._last_sync = None
		super().__init__(address, BridgeRequestHandler)

	@property
	def is_control_centre(self):
		return isinstance(self.controller, ControlCentre)

	def trigger_sync(self, limit=None, direction="all", flow_keys=None, source="manual"):
		if self.is_control_centre:
			return self.controller.trigger(direction, flow_keys, limit, source)
		with self._sync_state_lock:
			if self._sync_thread and self._sync_thread.is_alive():
				return False
			self._sync_thread = threading.Thread(
				target=self._run_legacy_sync,
				args=(limit,),
				name="tally-click-sync",
				daemon=True,
			)
			self._sync_thread.start()
			return True

	def _run_legacy_sync(self, limit):
		try:
			summary = self.service.sync_once(limit=limit)
			self._last_sync = summary.to_dict()
			LOGGER.info("Manual sync: %s", self._last_sync)
		except Exception as exc:
			self._last_sync = {"error": str(exc)}
			LOGGER.exception("Manual sync failed")

	def sync_status(self):
		if self.is_control_centre:
			return self.controller.state()
		with self._sync_state_lock:
			running = bool(self._sync_thread and self._sync_thread.is_alive())
			return {"status": 1, "running": running, "last_sync": self._last_sync}

	def server_close(self):
		if self.is_control_centre:
			self.controller.shutdown()
		super().server_close()


class BridgeRequestHandler(BaseHTTPRequestHandler):
	server_version = "ERPNextTallyControlCentre/2.0"

	def _json(self, status, payload):
		content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(content)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("X-Frame-Options", "DENY")
		self.end_headers()
		self.wfile.write(content)

	def _request_json(self):
		try:
			length = int(self.headers.get("Content-Length", "0"))
		except ValueError as exc:
			raise ValueError("Invalid Content-Length") from exc
		if length > 1_000_000:
			raise ValueError("Request body is too large")
		if not length:
			return {}
		try:
			value = json.loads(self.rfile.read(length).decode("utf-8"))
		except (UnicodeDecodeError, json.JSONDecodeError) as exc:
			raise ValueError("Request body must be valid JSON") from exc
		if not isinstance(value, dict):
			raise ValueError("Request body must be a JSON object")
		return value

	def _same_origin(self):
		origin = self.headers.get("Origin")
		if not origin:
			return True
		origin_parts = urllib.parse.urlsplit(origin)
		return origin_parts.netloc == self.headers.get("Host") and origin_parts.scheme in {"http", "https"}

	def _require_same_origin(self):
		if not self._same_origin():
			self._json(403, {"ok": False, "error": "Cross-origin control requests are not allowed"})
			return False
		return True

	def _handle_error(self, exc):
		LOGGER.warning("Control API request failed: %s", exc)
		self._json(400, {"ok": False, "error": str(exc)})

	def do_GET(self):
		path = urllib.parse.urlparse(self.path)
		if path.path in {"/health", "/api/v1/health"}:
			try:
				payload = self.server.controller.health()
				self._json(200 if payload.get("ok") else 503, payload)
			except Exception as exc:
				self._json(503, {"ok": False, "error": str(exc)})
			return
		if path.path == "/sync":
			if self.headers.get("Sec-Fetch-Site") == "cross-site" or not self._same_origin():
				self._json(403, {"status": 0, "message": "Cross-origin control requests are not allowed"})
				return
			query = urllib.parse.parse_qs(path.query)
			try:
				limit = int(query.get("limit", [0])[0]) or None
			except ValueError:
				self._json(400, {"status": 0, "message": "limit must be a number"})
				return
			started = self.server.trigger_sync(limit=limit)
			message = "Sync started in background" if started else "A sync is already running"
			self._json(202, {"status": 1, "started": started, "message": message})
			return
		if path.path in {"/sync-status", "/api/v1/state"}:
			self._json(200, self.server.sync_status())
			return
		if path.path == "/api/v1/config":
			if not self.server.is_control_centre:
				self._json(404, {"error": "Not found"})
				return
			self._json(200, self.server.controller.config().public_dict())
			return
		if path.path == "/api/v1/flows":
			try:
				self._json(200, {"flows": self.server.controller.flows()})
			except Exception as exc:
				self._json(503, {"flows": [], "error": str(exc)})
			return
		self._serve_static(path.path)

	def do_POST(self):
		if not self._require_same_origin():
			return
		path = urllib.parse.urlparse(self.path).path
		try:
			body = self._request_json()
			if path == "/api/v1/sync":
				started = self.server.trigger_sync(
					limit=body.get("limit"),
					direction=body.get("direction", "all"),
					flow_keys=body.get("flow_keys"),
				)
				self._json(
					202 if started else 409,
					{"ok": started, "started": started, "message": "Sync started" if started else "A sync is already running"},
				)
				return
			if path == "/api/v1/auto-sync":
				if not self.server.is_control_centre:
					raise ValueError("Automatic sync requires Control Centre mode")
				self._json(200, {"ok": True, **self.server.controller.set_auto_sync(body.get("enabled"))})
				return
			if path == "/api/v1/test-connections":
				self._json(200, self.server.controller.health())
				return
			if path == "/api/v1/checkpoints/reset":
				self._json(200, {"ok": True, **self.server.controller.reset_inbound_checkpoints()})
				return
			self._json(404, {"error": "Not found"})
		except Exception as exc:
			self._handle_error(exc)

	def do_PUT(self):
		if not self._require_same_origin():
			return
		path = urllib.parse.urlparse(self.path).path
		try:
			if path == "/api/v1/config" and self.server.is_control_centre:
				config = self.server.controller.update_config(self._request_json())
				self._json(200, {"ok": True, "config": config})
				return
			self._json(404, {"error": "Not found"})
		except Exception as exc:
			self._handle_error(exc)

	def _serve_static(self, request_path):
		if request_path in {"", "/"}:
			request_path = "/index.html"
		relative = Path(urllib.parse.unquote(request_path).lstrip("/"))
		if ".." in relative.parts:
			self._json(404, {"error": "Not found"})
			return
		asset = (self.server.web_root / relative).resolve()
		if self.server.web_root not in asset.parents or not asset.is_file():
			self._json(404, {"error": "Not found"})
			return
		content = asset.read_bytes()
		content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
		self.send_response(200)
		self.send_header("Content-Type", f"{content_type}; charset=utf-8")
		self.send_header("Content-Length", str(len(content)))
		self.send_header("Cache-Control", "no-cache" if asset.name == "index.html" else "public, max-age=3600")
		self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("X-Frame-Options", "DENY")
		self.end_headers()
		self.wfile.write(content)

	def log_message(self, fmt, *args):
		LOGGER.info("%s - %s", self.address_string(), fmt % args)


def start_polling(service, interval_seconds):
	"""Backward-compatible polling helper for callers outside Control Centre mode."""
	def poll():
		while True:
			summary = service.sync_once()
			LOGGER.info("Scheduled sync: %s", summary.to_dict())
			time.sleep(max(int(interval_seconds), 10))

	thread = threading.Thread(target=poll, name="tally-sync-poller", daemon=True)
	thread.start()
	return thread
