"""
TeamDynamix API.
"""

import email.utils
import json
import logging
import random
import threading
import time
import urllib.parse
from importlib.metadata import PackageNotFoundError, version

import requests

import tdapi.asset
import tdapi.cmdb

try:
    __version__ = version("tdapi")
except PackageNotFoundError:  # pragma: no cover - source tree, not installed
    __version__ = "0.0.0+unknown"

logger = logging.getLogger(__name__)


TD_CONNECTION = None


DEFAULT_TIMEOUT = 60

# Bounded retry budget for 429s, 5xxs and connection errors. A bulk
# export makes hundreds of thousands of requests; a single transient
# failure must not end the run, and an unbounded retry must not hide a
# tenant that is genuinely down.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_MAX_RETRY_WAIT = 300.0

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Streaming chunk size for content_request(). 64 KiB is large enough
# that syscall overhead is noise and small enough that a big attachment
# never sits in memory.
DEFAULT_CHUNK_SIZE = 64 * 1024

# requests raises these for a connection reset, DNS failure or a read
# that exceeded `timeout` -- all worth one more try.
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

RATE_LIMIT_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)

ALLOWED_METHODS = ("post", "get", "delete", "put", "patch")

PRODUCTION_APP_PATH = "TDWebApi/api/"
SANDBOX_APP_PATH = "SBTDWebApi/api/"

_APP_PATHS = (PRODUCTION_APP_PATH, SANDBOX_APP_PATH)

_URL_ROOT_REQUIRED = (
    "url_root is required: pass your organization's TeamDynamix URL, "
    "e.g. url_root='https://example.teamdynamix.com/TDWebApi/api/' "
    "(or the bare 'https://example.teamdynamix.com/' and let sandbox= "
    "choose the application). The shared host api.teamdynamix.com was "
    "discontinued on 2021-11-13 and now answers every request with 503."
)


def _apply_preview(url_root):
    """
    Point a production organization URL at the organization's preview
    environment, which TeamDynamix hosts under teamdynamixpreview.com.

    Raises TDConfigurationException rather than guessing when the host
    is not recognizably a TeamDynamix one -- silently sending preview
    traffic to a production tenant is the failure worth preventing.
    """
    parts = urllib.parse.urlsplit(url_root)
    host = parts.hostname or ""

    if host.endswith(".teamdynamixpreview.com"):
        return url_root
    if not host.endswith(".teamdynamix.com"):
        raise TDConfigurationException(
            f"preview=True, but {url_root!r} is not a *.teamdynamix.com host, so the "
            "preview hostname cannot be derived from it. Pass the preview "
            "URL directly as url_root instead."
        )

    netloc = parts.netloc.replace(".teamdynamix.com", ".teamdynamixpreview.com")
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def resolve_url_root(url_root, preview=False, sandbox=False):
    """
    Build the absolute API root every request URL is joined onto.

    `url_root` is required and is the organization's own TeamDynamix
    URL. It may be either:

    * a complete API root ending in `TDWebApi/api` (production) or
      `SBTDWebApi/api` (sandbox), which is used as given; or
    * a bare organization URL, in which case `sandbox` selects the
      application path.

    `sandbox=True` against a url_root that already names the production
    application is a contradiction and raises, rather than quietly
    sending sandbox-intended traffic to production.

    Returns:
        The API root, with a trailing slash so urljoin() keeps the path.

    Raises:
        TDConfigurationException: if url_root is missing, or if the
            arguments contradict each other.
    """
    if not url_root:
        raise TDConfigurationException(_URL_ROOT_REQUIRED)

    if preview is True:
        url_root = _apply_preview(url_root)

    normalized = url_root if url_root.endswith("/") else url_root + "/"
    lowered = normalized.lower()

    for app_path in _APP_PATHS:
        if not lowered.endswith(app_path.lower()):
            continue
        if sandbox is True and app_path == PRODUCTION_APP_PATH:
            raise TDConfigurationException(
                f"sandbox=True, but url_root {url_root!r} names the production "
                f"application ({PRODUCTION_APP_PATH}). Pass the sandbox URL, or a bare "
                "organization URL, instead."
            )
        return normalized

    return normalized + (SANDBOX_APP_PATH if sandbox else PRODUCTION_APP_PATH)


def filename_from_content_disposition(value):
    """
    Pull the filename out of a Content-Disposition header.

    Handles both `filename="x.pdf"` and RFC 5987's `filename*=UTF-8''x.pdf`,
    preferring the latter when both are present because it is the one
    that can carry non-ASCII names.

    Returns:
        The decoded filename, or None when the header is absent or
        carries no filename. None means "the server did not tell us",
        and the caller should fall back to the attachment metadata.
    """
    if not value:
        return None

    encoded = None
    plain = None
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if part.lower().startswith("filename*="):
            encoded = part.split("=", 1)[1].strip()
        elif part.lower().startswith("filename="):
            plain = part.split("=", 1)[1].strip().strip('"')

    if encoded:
        # charset'language'percent-encoded-value
        pieces = encoded.split("'", 2)
        if len(pieces) == 3:
            charset, _language, raw = pieces
            return urllib.parse.unquote(raw, encoding=charset or "utf-8", errors="replace")
        return urllib.parse.unquote(encoded)

    return plain or None


def parse_retry_after(value, now=None):
    """
    Parse a Retry-After (or X-RateLimit-Reset) header into seconds.

    The header is either delta-seconds or an HTTP date; TeamDynamix has
    been seen to use both spellings across endpoints, and the value is
    not worth a failed run either way.

    Returns:
        A non-negative float number of seconds, or None if the value is
        absent or unparseable. None means "no server-supplied delay",
        never "no delay needed" -- the caller falls back to its own
        backoff.
    """
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None

    if now is None:
        now = time.time()
    if parsed.tzinfo is None:
        # An HTTP date without a zone is GMT by definition (RFC 9110).
        return max(0.0, parsed.timestamp() - now)
    return max(0.0, parsed.timestamp() - now)


class RateLimiter:
    """
    Minimum-interval pacer shared by every thread on one connection.

    Unlike an unconditional `time.sleep()` after each request, this only
    sleeps when the next request would otherwise arrive too soon -- so
    work done between requests (parsing, writing a file to disk) counts
    against the interval instead of being added to it.

    `pause()` lets a 429 on one thread slow every thread down, which is
    the behaviour a shared tenant rate limit actually calls for.
    """

    def __init__(self, min_interval):
        self.min_interval = max(0.0, float(min_interval or 0.0))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        """Block until this thread's turn, then claim it."""
        with self._lock:
            sleep_for = self._next_allowed - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    def record(self):
        """Note that a real (non-cached) request just went out."""
        if self.min_interval <= 0:
            return
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + self.min_interval)

    def pause(self, seconds):
        """Hold every thread off for at least `seconds` from now."""
        if seconds is None or seconds <= 0:
            return
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


def make_session(cache_expire_after=None):
    """
    Build the `requests` session a connection will use.

    By default this is a plain `requests.Session` with no caching at
    all. Pass `cache_expire_after` (seconds) to get a
    `requests_cache.CachedSession` instead; that requires the optional
    `cache` extra (`pip install tdapi[cache]`).

    Caching is opt-in, and scoped to this session rather than installed
    globally, for two reasons:

    * `requests_cache.install_cache()` monkey-patches `requests` for the
      whole process, so importing this library used to silently change
      the behaviour of every other HTTP client in the program.
    * A caching session is actively wrong for bulk reads: an export that
      walks every ticket never revisits a URL, so the cache only costs
      disk, and a cached *attachment download* writes the binary into
      the cache database as well as to its destination.
    """
    if cache_expire_after is None:
        return requests.Session()

    try:
        import requests_cache  # noqa: PLC0415 - optional extra, imported on demand
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise TDException(
            "cache_expire_after was set but requests-cache is not installed; "
            "install the optional extra with: pip install tdapi[cache]"
        ) from exc

    return requests_cache.CachedSession(expire_after=cache_expire_after)


class TDException(Exception):
    """
    Any manner of TD error e.g. failed login.

    Returned for non-200 HTTP response codes.
    """

    pass


class TDAuthorizationException(Exception):
    """
    Returned for 401 unauthorized HTTP response code.
    """

    pass


class TDConfigurationException(TDException):
    """
    Raised for a connection that cannot be built as configured, before
    any request is attempted.
    """

    pass


# TODO probably rename this to TDAdminConnection
class TDConnection:
    """
    This uses the TeamDynamix API:

        https://community.teamdynamix.com/Developers/REST/Default.aspx

    This class manages authentication and API requests. Instantiation
    requires a login.

    Typical use:

        conn = tdapi.TDConnection(BEID='key-here',
                                WebServicesKey='key-here')
        conn.post_accounts_search(method='post',
                                  url_stem='accounts/search',
                                  data={'search': 'Test'})

    """

    def __init__(
        self,
        BEID,
        WebServicesKey,
        sandbox=False,
        preview=False,
        url_root=None,
        request_delay=1,
        cache_expire_after=None,
        timeout=DEFAULT_TIMEOUT,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    ):
        """
        TODO this only uses the new superuser login option with BEID and
        WebServicesKey.

        The `request_delay` attribute is the minimum number of seconds
        between requests. It is enforced *before* each request rather
        than slept away after one, so time spent between requests
        counts against it.

        `cache_expire_after` opts this connection into response
        caching; see `make_session()`.

        `timeout` is passed to every request, in seconds. Without one a
        stalled connection hangs the caller forever, which for a
        long-running batch job means a run that never finishes and
        never fails.

        `max_attempts` bounds the retry budget for 429s, 5xxs and
        connection errors; see `raw_request()`.
        """
        self.bearer_token = False  # This will be set in login()
        self.BEID = BEID
        self.WebServicesKey = WebServicesKey
        self.session = make_session(cache_expire_after)
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.rate_limiter = RateLimiter(request_delay)
        # Most recent X-RateLimit-* values the tenant reported, so a
        # caller can pace itself against real numbers rather than
        # guessed ones. Empty until the tenant sends any.
        self.rate_limit_status = {}

        self._make_url_root(url_root=url_root, preview=preview, sandbox=sandbox)
        self.login()

    def _make_url_root(self, url_root, preview, sandbox):
        """
        Resolve `self.url_root` from the caller's organization URL.

        `url_root` is required. See `resolve_url_root()`.
        """
        self.url_root = resolve_url_root(url_root=url_root, preview=preview, sandbox=sandbox)

    def _make_url(self, url_stem):
        """
        This uses urljoin to create the absolute URL.
        """
        return urllib.parse.urljoin(self.url_root, url_stem)

    def add_authorization_header(self, headers):
        headers["Authorization"] = f"Bearer {self.bearer_token}"

    def handle_resp(self, resp):
        if resp.status_code == 401:
            raise TDAuthorizationException(f"{resp.url} returned 401 status\n{resp.text}")
        elif resp.status_code not in [200, 201]:
            raise TDException(
                f"{resp.url} returned non-200 status ({resp.status_code})\n{resp.text}"
            )

    def files_request(self, method, url_stem, files):  # noqa: ARG002 - published signature
        headers = {}
        self.add_authorization_header(headers)
        resp = self.session.post(
            self._make_url(url_stem), files=files, headers=headers, timeout=self.timeout
        )
        self.handle_resp(resp)
        return resp

    def note_rate_limit_headers(self, resp):
        """
        Record whatever X-RateLimit-* headers the tenant returned.

        The TeamDynamix docs went offline with api.teamdynamix.com, so
        which of these a given tenant sends is not knowable in advance.
        Recording what actually arrives lets a caller pace itself
        against the tenant's own numbers.
        """
        seen = {
            header: resp.headers[header] for header in RATE_LIMIT_HEADERS if header in resp.headers
        }
        if seen:
            self.rate_limit_status = seen
            logger.debug("Rate limit headers: %s", seen)
        return seen

    def retry_wait(self, attempt, resp):
        """
        Seconds to wait before retrying: the server's number if it gave
        one, otherwise exponential backoff with full jitter.
        """
        server_wait = None
        if resp is not None:
            server_wait = parse_retry_after(resp.headers.get("Retry-After"))

        if server_wait is None:
            ceiling = min(DEFAULT_MAX_RETRY_WAIT, DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)))
            # Full jitter: spreads a thundering herd of worker threads
            # that all hit the same 429 instead of re-synchronizing them.
            return random.uniform(0, ceiling)  # noqa: S311 - pacing, not crypto

        if server_wait > DEFAULT_MAX_RETRY_WAIT:
            logger.warning(
                "Server asked for a %.0fs retry delay; capping at %.0fs",
                server_wait,
                DEFAULT_MAX_RETRY_WAIT,
            )
            return DEFAULT_MAX_RETRY_WAIT
        return server_wait

    def _build_request(self, method, url_stem, data, bearer_required, content_type):
        """
        Assemble one request's URL, headers and body.

        Returns:
            A (url, headers, payload) tuple.

        Raises:
            TDException: if the HTTP method is not one this client sends.
        """
        if method not in ALLOWED_METHODS:
            raise TDException(f"method {method} not supported")

        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type

        if bearer_required:
            self.add_authorization_header(headers)

        payload = json.dumps(data) if data is not None else ""
        return self._make_url(url_stem), headers, payload

    def _backoff_after_exception(self, attempt, method, url, exc):
        """
        Sleep before retrying a connection-level failure.

        Raises:
            TDException: if the retry budget is spent, naming the
                attempt count so the log says why the run gave up.
        """
        self.rate_limiter.record()
        if attempt == self.max_attempts:
            raise TDException(
                f"{method.upper()} {url} failed after {self.max_attempts} attempts: {exc}"
            ) from exc

        wait = self.retry_wait(attempt, None)
        logger.warning(
            "%s %s: %s; retrying in %.1fs (attempt %s/%s)",
            method.upper(),
            url,
            exc,
            wait,
            attempt,
            self.max_attempts,
        )
        time.sleep(wait)

    def _should_retry_response(self, attempt, method, url, resp, stream):
        """
        Decide whether `resp` earns another attempt, and pause if so.

        Returns:
            True if the caller should loop again -- in which case this
            has already slept and, for a streaming response, released
            the connection. False when the response is final, whether
            it succeeded or the budget is spent.
        """
        if resp.status_code not in RETRYABLE_STATUS_CODES:
            return False

        if attempt == self.max_attempts:
            logger.error(
                "%s %s still returning %s after %s attempts",
                method.upper(),
                url,
                resp.status_code,
                self.max_attempts,
            )
            return False

        wait = self.retry_wait(attempt, resp)
        logger.warning(
            "%s %s returned %s; retrying in %.1fs (attempt %s/%s)",
            method.upper(),
            url,
            resp.status_code,
            wait,
            attempt,
            self.max_attempts,
        )
        if resp.status_code == 429:
            # Slow every thread on this connection, not just this one.
            self.rate_limiter.pause(wait)
        # A retried streaming response still holds its connection;
        # give it back before asking for another.
        if stream:
            resp.close()
        time.sleep(wait)
        return True

    def send(
        self,
        method,
        url_stem,
        data=None,
        bearer_required=True,
        stream=False,
        content_type="application/json",
    ):
        """
        Send one logical request, pacing and retrying as configured.

        This is the shared core of `raw_request()` (which reads a JSON
        body) and `raw_content_request()` (which streams a binary one),
        so there is exactly one retry loop in this client.

        Retries 429, 5xx and connection errors up to `max_attempts`,
        honouring Retry-After when the tenant sends it and backing off
        exponentially with jitter when it does not. A 429 pauses the
        shared rate limiter, so every thread on this connection slows
        down rather than only the one that was throttled.

        Returns:
            The `requests.Response`, *not* yet passed through
            `handle_resp()` -- the caller decides when to raise,
            because a streaming caller must do so before touching the
            body.

        Raises:
            TDException: if the method is unsupported, or if a
                connection error outlived the retry budget.
        """
        url, headers, payload = self._build_request(
            method, url_stem, data, bearer_required, content_type
        )
        resp = None

        for attempt in range(1, self.max_attempts + 1):
            self.rate_limiter.wait()
            logger.debug(
                "%s to %s, data %s (attempt %s/%s)",
                method.upper(),
                url,
                payload,
                attempt,
                self.max_attempts,
            )

            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    data=payload,
                    headers=headers,
                    timeout=self.timeout,
                    stream=stream,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                self._backoff_after_exception(attempt, method, url, exc)
                continue

            # A cached response consumed no quota, so it must not push
            # the next real request further out.
            if getattr(resp, "from_cache", False) is False:
                self.rate_limiter.record()

            self.note_rate_limit_headers(resp)

            if not self._should_retry_response(attempt, method, url, resp, stream):
                break

        return resp

    def raw_request(self, method, url_stem, data=None, bearer_required=True):
        """
        This method sends a request to TeamDynamix and returns the
        response, whose body is read into memory.

        `data` will be converted to JSON.

        The `bearer_required` option is only set to false for logging
        in.

        Returns:
            The `requests.Response`, which has already passed
            `handle_resp()`.

        Raises:
            TDAuthorizationException: on a 401.
            TDException: on any other non-200/201 response, including
                one still failing after the last retry.
        """
        resp = self.send(
            method=method, url_stem=url_stem, data=data, bearer_required=bearer_required
        )

        logger.debug("Response code: %s\nResponse: %s", resp.status_code, resp.text)

        self.handle_resp(resp)

        return resp

    def raw_content_request(self, url_stem, fileobj, method="get", chunk_size=DEFAULT_CHUNK_SIZE):
        """
        Stream a non-JSON response body into `fileobj`.

        This is the path for `GET attachments/{id}/content`, whose body
        is an arbitrary binary file: `json_request` would try to parse
        it, and `raw_request` would hold the whole thing in memory and
        log it. Here the body is never materialized -- it is copied
        chunk by chunk into the caller's file handle.

        `fileobj` must be opened in binary mode. Nothing is written to
        it unless the response is one `handle_resp()` accepts, so a 404
        or an expired token leaves the caller's file untouched.

        Returns:
            A dict of what the transfer saw: `bytes` written,
            `content_type`, `content_length` (the declared value, which
            may be None), and `filename` parsed out of any
            Content-Disposition header. The caller checksums the bytes
            it wrote; this client does not.

        Raises:
            TDAuthorizationException: on a 401.
            TDException: on any other non-200/201 response.
        """
        resp = self.send(
            method=method, url_stem=url_stem, bearer_required=True, stream=True, content_type=None
        )

        # Raise before a single byte reaches the caller's file: an error
        # body is HTML or JSON, and writing it would produce a
        # plausible-looking attachment full of an error message.
        try:
            self.handle_resp(resp)

            written = 0
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                fileobj.write(chunk)
                written += len(chunk)
        finally:
            resp.close()

        declared = resp.headers.get("Content-Length")
        return {
            "bytes": written,
            "content_type": resp.headers.get("Content-Type"),
            "content_length": int(declared) if declared is not None else None,
            "filename": filename_from_content_disposition(resp.headers.get("Content-Disposition")),
        }

    def request(self, *args, **kwargs):
        """
        Calls raw_request. If TDAuthorizationException is raised, tries to
        login and do it again.
        """
        try:
            return self.raw_request(*args, **kwargs)
        except TDAuthorizationException:
            self.login()
            return self.raw_request(*args, **kwargs)

    def content_request(self, *args, **kwargs):
        """
        Calls raw_content_request. If TDAuthorizationException is
        raised, tries to login and do it again.

        The retry is safe because `raw_content_request` raises before it
        writes anything, so the caller's file handle is untouched by the
        failed attempt.
        """
        try:
            return self.raw_content_request(*args, **kwargs)
        except TDAuthorizationException:
            self.login()
            return self.raw_content_request(*args, **kwargs)

    def json_request(self, *args, **kwargs):
        """
        Simple wrapper around request() that converts JSON response into
        a Python object.
        """
        resp = self.request(*args, **kwargs)
        return json.loads(resp.text)

    def login(self):
        """
        This posts the login data.
        """
        resp = self.request(
            method="post",
            url_stem="auth/loginadmin",
            data={
                "BEID": self.BEID,
                "WebServicesKey": self.WebServicesKey,
            },
            bearer_required=False,
        )
        self.bearer_token = resp.text

    def json_request_roller(self, *args, **kwargs):
        """
        Will always return a list. If TD returns one element, you'll get a
        list.
        """
        objs = self.json_request(*args, **kwargs)
        if isinstance(objs, dict):
            # TD returned one element
            return [objs]
        else:
            return objs

    def new_ci(self, type_id, name):
        # FIXME this needs to be redone probably as
        # TDConfigurationItem({new_struct}).save()
        td_struct = self.json_request(
            method="post",
            url_stem="cmdb",
            data={
                "TypeID": type_id,
                "Name": name,
            },
        )
        return tdapi.cmdb.TDConfigurationItem(td_struct=td_struct)


class TDUserConnection(TDConnection):
    """
    Log in as a user rather than as admin.
    """

    def __init__(
        self,
        username,
        password,
        sandbox=False,
        preview=False,
        url_root=None,
        request_delay=1,
        cache_expire_after=None,
        timeout=DEFAULT_TIMEOUT,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    ):
        self.bearer_token = False
        self.username = username
        self.password = password
        self.session = make_session(cache_expire_after)
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.rate_limiter = RateLimiter(request_delay)
        # Most recent X-RateLimit-* values the tenant reported, so a
        # caller can pace itself against real numbers rather than
        # guessed ones. Empty until the tenant sends any.
        self.rate_limit_status = {}

        self._make_url_root(url_root=url_root, preview=preview, sandbox=sandbox)
        self.login()

    def login(self):
        resp = self.request(
            method="post",
            url_stem="auth/login",
            data={
                "UserName": self.username,
                "Password": self.password,
            },
            bearer_required=False,
        )
        self.bearer_token = resp.text


def set_connection(conn):
    # TODO: this probably shouldn't be a global variable.
    tdapi.TD_CONNECTION = conn


def get_connection():
    return tdapi.TD_CONNECTION
