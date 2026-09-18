"""
TeamDynamix API.
"""
import json
import logging
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

ALLOWED_METHODS = ('post', 'get', 'delete', 'put', 'patch')

PRODUCTION_APP_PATH = 'TDWebApi/api/'
SANDBOX_APP_PATH = 'SBTDWebApi/api/'

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
    host = parts.hostname or ''

    if host.endswith('.teamdynamixpreview.com'):
        return url_root
    if not host.endswith('.teamdynamix.com'):
        raise TDConfigurationException(
            "preview=True, but {!r} is not a *.teamdynamix.com host, so the "
            "preview hostname cannot be derived from it. Pass the preview "
            "URL directly as url_root instead.".format(url_root))

    netloc = parts.netloc.replace('.teamdynamix.com',
                                  '.teamdynamixpreview.com')
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

    normalized = url_root if url_root.endswith('/') else url_root + '/'
    lowered = normalized.lower()

    for app_path in _APP_PATHS:
        if not lowered.endswith(app_path.lower()):
            continue
        if sandbox is True and app_path == PRODUCTION_APP_PATH:
            raise TDConfigurationException(
                "sandbox=True, but url_root {!r} names the production "
                "application ({}). Pass the sandbox URL, or a bare "
                "organization URL, instead.".format(url_root,
                                                    PRODUCTION_APP_PATH))
        return normalized

    return normalized + (SANDBOX_APP_PATH if sandbox else PRODUCTION_APP_PATH)


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
        import requests_cache
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
class TDConnection(object):
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
    def __init__(self,
                 BEID,
                 WebServicesKey,
                 sandbox=False,
                 preview=False,
                 url_root=None,
                 request_delay=1,
                 cache_expire_after=None,
                 timeout=DEFAULT_TIMEOUT):
        """
        TODO this only uses the new superuser login option with BEID and
        WebServicesKey.

        The `request_delay` attribute is the amount of time to sleep
        after each request.

        `cache_expire_after` opts this connection into response
        caching; see `make_session()`.

        `timeout` is passed to every request, in seconds. Without one a
        stalled connection hangs the caller forever, which for a
        long-running batch job means a run that never finishes and
        never fails.
        """
        self.bearer_token = False            # This will be set in login()
        self.BEID = BEID
        self.WebServicesKey = WebServicesKey
        self.session = make_session(cache_expire_after)
        self.request_delay = request_delay
        self.timeout = timeout

        self._make_url_root(url_root=url_root,
                            preview=preview,
                            sandbox=sandbox)
        self.login()

    def _make_url_root(self, url_root, preview, sandbox):
        """
        Resolve `self.url_root` from the caller's organization URL.

        `url_root` is required. See `resolve_url_root()`.
        """
        self.url_root = resolve_url_root(url_root=url_root,
                                         preview=preview,
                                         sandbox=sandbox)

    def _make_url(self, url_stem):
        """
        This uses urljoin to create the absolute URL.
        """
        return urllib.parse.urljoin(self.url_root, url_stem)

    def add_authorization_header(self, headers):
        headers['Authorization'] = 'Bearer {}'.format(self.bearer_token)

    def handle_resp(self, resp):
        if resp.status_code == 401:
            raise TDAuthorizationException("{} returned 401 status\n{}".format(
                resp.url, resp.text))
        elif resp.status_code not in [200, 201]:
            raise TDException("{} returned non-200 status ({})\n{}".format(
                resp.url, resp.status_code, resp.text))

    def files_request(self, method, url_stem,
                      files):
        headers = {}
        self.add_authorization_header(headers)
        resp = self.session.post(self._make_url(url_stem),
                                 files=files,
                                 headers=headers,
                                 timeout=self.timeout)
        self.handle_resp(resp)
        return resp

                      
    def raw_request(self, method, url_stem,
                    data=None,
                    bearer_required=True):
        """
        This method sends a request to TeamDynamix.

        `data` will be converted to JSON.

        The `bearer_required` option is only set to false for logging
        in.
        """
        if method not in ALLOWED_METHODS:
            raise TDException("method {} not supported".format(method))

        headers = {}
        headers['Content-Type'] = 'application/json'

        if bearer_required:
            self.add_authorization_header(headers)

        if data is not None:
            payload = json.dumps(data)
        else:
            payload = ''

        url = self._make_url(url_stem)
        logger.debug('%s to %s, data %s', method.upper(), url, payload)

        resp = self.session.request(method=method,
                                    url=url,
                                    data=payload,
                                    headers=headers,
                                    timeout=self.timeout,
        )

        logger.debug('Response code: %s\nResponse: %s',
                      resp.status_code,
                      resp.text)

        # if resp was not from cache:
        if getattr(resp, "from_cache", False) is False:
            time.sleep(self.request_delay)

        self.handle_resp(resp)

        return resp

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
        resp = self.request(method='post',
                            url_stem='auth/loginadmin',
                            data={'BEID': self.BEID,
                                  'WebServicesKey': self.WebServicesKey,
                              },
                            bearer_required=False
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
        td_struct=self.json_request(method='post',
                                    url_stem='cmdb',
                                    data={'TypeID': type_id,
                                        'Name': name,
                                      })
        return tdapi.cmdb.TDConfigurationItem(td_struct=td_struct)


class TDUserConnection(TDConnection):
    """
    Log in as a user rather than as admin.
    """
    def __init__(self,
                 username,
                 password,
                 sandbox=False,
                 preview=False,
                 url_root=None,
                 request_delay=1,
                 cache_expire_after=None,
                 timeout=DEFAULT_TIMEOUT):
        self.bearer_token = False
        self.username = username
        self.password = password
        self.session = make_session(cache_expire_after)
        self.request_delay = request_delay
        self.timeout = timeout

        self._make_url_root(url_root=url_root,
                            preview=preview,
                            sandbox=sandbox)
        self.login()

    def login(self):
        resp = self.request(method='post',
                            url_stem='auth/login',
                            data={'UserName': self.username,
                                  'Password': self.password,
                              },
                            bearer_required=False
                        )
        self.bearer_token = resp.text

    
def set_connection(conn):
    # TODO: this probably shouldn't be a global variable.
    tdapi.TD_CONNECTION = conn

def get_connection():
    return tdapi.TD_CONNECTION
