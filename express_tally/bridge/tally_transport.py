"""Process-wide admission control for Tally's single-threaded HTTP gateway."""

import http.client
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class EndpointState:
	def __init__(self):
		self.lock = threading.Lock()
		self.uncertain = False
		self.ready_at = 0.0


_STATES = {}
_STATES_LOCK = threading.Lock()


def endpoint_state(url):
	parsed = urllib.parse.urlsplit(url)
	host = (parsed.hostname or "").lower()
	# Settings and callers commonly use either spelling for the local gateway.
	if host == "localhost":
		host = "127.0.0.1"
	key = (parsed.scheme.lower(), host, parsed.port or (443 if parsed.scheme == "https" else 80))
	with _STATES_LOCK:
		return _STATES.setdefault(key, EndpointState())


def send(request, timeout, error_type):
	"""Never queue concurrent calls or retry a request whose outcome is unknown."""
	state = endpoint_state(request.full_url)
	if not state.lock.acquire(blocking=False):
		raise error_type("Tally is busy with another connector request; try again after it finishes")
	try:
		if state.uncertain:
			raise error_type(
				"Tally requests are paused after a connection failure. The last request may "
				"still be running. Check Tally and the imported records, then restart the bridge "
				"only when Tally is responsive."
			)
		time.sleep(max(0, state.ready_at - time.monotonic()))
		started = time.monotonic()
		try:
			with urllib.request.urlopen(request, timeout=timeout) as response:
				return response.read().decode("utf-8", errors="replace")
		except urllib.error.HTTPError as exc:
			detail = exc.read().decode("utf-8", errors="replace")
			raise error_type(f"Tally returned HTTP {exc.code}: {detail[:1000]}") from exc
		except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
			# Closing the socket does not cancel the operation inside Tally. Keep the
			# circuit open across newly constructed clients and scheduler/health calls.
			state.uncertain = True
			raise error_type(
				f"Tally request failed; outcome unknown: {exc}. Further requests are paused. "
				"Check Tally and imported records before restarting the bridge."
			) from exc
		finally:
			elapsed = time.monotonic() - started
			state.ready_at = time.monotonic() + max(0.25, min(elapsed * 0.25, 2.0))
	finally:
		state.lock.release()
