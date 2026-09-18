# Project introduction

This is a Python API for TeamDynamix. It is not complete--there are
other things you can do in the TeamDynamix API that you can't do with
this code.

# Installing

```
uv add tdapi                      # or: pip install tdapi
uv add 'tdapi[cache]'             # only if you want response caching
```

# How to use

## Creating a connection

To create your connection, instantiate a TDConnection with your BEID,
your WebServicesKey, and **your organization's TeamDynamix URL**:

```
import tdapi

TD_CONNECTION = tdapi.TDConnection(
    BEID=BEID,
    WebServicesKey=WebServicesKey,
    url_root='https://example.teamdynamix.com/TDWebApi/api/')
```

`url_root` is required. Older versions defaulted to the shared host
`api.teamdynamix.com`, which TeamDynamix discontinued on 2021-11-13; it
now answers every request with HTTP 503 and a message telling you to
use your organization's own URL. There is no default that could work,
so the absence of one raises `TDConfigurationException` rather than
failing later as an opaque 503.

You can also pass the bare organization URL and let the `sandbox` flag
choose the application path:

```
# https://example.teamdynamix.com/TDWebApi/api/
tdapi.TDConnection(BEID=..., WebServicesKey=...,
                   url_root='https://example.teamdynamix.com/')

# https://example.teamdynamix.com/SBTDWebApi/api/
tdapi.TDConnection(BEID=..., WebServicesKey=...,
                   url_root='https://example.teamdynamix.com/',
                   sandbox=True)
```

Passing `sandbox=True` alongside a `url_root` that already names the
production application raises, rather than quietly sending
sandbox-intended traffic to production. `preview=True` rewrites a
`*.teamdynamix.com` host to your `*.teamdynamixpreview.com` one.

## Pacing, timeouts and retries

```
TD_CONNECTION = tdapi.TDConnection(
    BEID=BEID, WebServicesKey=WebServicesKey, url_root=URL_ROOT,
    request_delay=0.5,   # minimum seconds between requests
    timeout=60,          # per-request timeout, seconds
    max_attempts=5)      # retry budget for 429s, 5xxs, dropped connections
```

`request_delay` is a *minimum interval* enforced before each request,
not a sleep after one, so time you spend between requests counts
against it.

429, 500, 502, 503, 504 and connection/timeout errors are retried up to
`max_attempts` times. `Retry-After` is honoured when the tenant sends
it (in either the delta-seconds or the HTTP-date spelling); otherwise
the client backs off exponentially with full jitter. A 429 slows down
every thread sharing the connection, not just the one that was
throttled. Whatever `X-RateLimit-*` headers the tenant returns are
recorded on `conn.rate_limit_status` so you can pace yourself against
real numbers.

## Downloading attachments and other binaries

`json_request` would try to parse a binary body, and `raw_request`
would hold it all in memory. Use `content_request`, which streams into
a file handle you open:

```
with open('report.pdf', 'wb') as fh:
    result = TD_CONNECTION.content_request(
        'attachments/{}/content'.format(attachment_id), fh)

result['bytes']           # how many were written
result['content_type']
result['content_length']  # what the server declared, or None
result['filename']        # from Content-Disposition, or None
```

Nothing is written unless the response is one the client accepts, so a
404 or an expired token leaves your file untouched rather than filling
it with an error page.

## Tickets

```
ticket = TDTicket.objects.get(ticket_id)
ticket.feed()     # the ticket's conversation and audit trail
ticket.tasks()    # the ticket's tasks
```

A ticket record carries neither its feed nor its tasks; both are
separate requests.

## Low-level stuff

Reviewing TeamDynamix's API documentation, you may see a call you want
to make that is not already in the API. Great! Especially if you are
fetching data, you probably want the `json_request_roller` method.
This method calls an arbitrary API call and always returns a list of
Python objects:

```
models = TD_CONNECTION.json_request_roller(
             method='get',
			 url_stem='assets/models')
for model in models:
  ...
```

`json_request_roller` calls `json_request` but ensures a list is
always returned. `json_request` calls `request` and then converts the
returned data into Python objects. `request` calls `raw_request`. See
the `raw_request` documentation to see the possible arguments.

## Higher-level stuff

*Some* TeamDynamix API stuff has a richer Python wrapper. The style is
along the lines of Django's calls. The trick is that to make the API cleaner the connection needs to be set as a global variable. This is currently done this way:

```
import tdapi
conn_obj = tdapi.TDConnection(BEID='your-BEID', WebServicesKey='your-key)
tdapi.set_connection(conn_obj)
```

After you've set the connection, you can say...

```
projects = TDProject.objects.current()
for project in projects:
    print project.td_url(), project.td_struct['Name']
```

for example.

# Writing your own higher-level API stuff

You can use `TDAsset` as a model for creating a new Python class.
You need at least the below code:

```
class TDWhateverManager(api.obj.TDObjectManager): pass
class TDWhatever(api.obj.TDObject): pass
api.obj.relate_cls_to_manager(TDWhatever, TDWhateverManager)
```

this `relate_cls_to_manager` function creates `TDWhatever.objects` and
it also tells `TDWhateverManager` about `TDWhatever` so that it can
instantiate objects correctly.

The `api.obj` code is a thin wrapper over the "raw" JSON code.

## Making your stuff somewhat useful

If you wanted to then make these classes useful, you would do the following:

### On the manager

Create a way to create objects, for example:

```
class TDWhateverManager(api.obj.TDObjectManager):
  def search(self, search_params):
    return [self.object_class(td_struct)
	        for td_struct
		    in settings.TD_CONNECTION.json_request_roller(
			    method='post',
				url_stem='whatever/search',
				data=search_params)]
```

(The aforementioned `relate_cls_to_manager` populates `self.object_class`.)

### On the class

Create methods as needed, for example:

```
class TDWhatever(api.obj.TDObject):
   def name(self):
     return self.td_struct['Name']
```

# Caching

Response caching is **off** by default and scoped to the connection
that asks for it:

```
tdapi.TDConnection(..., cache_expire_after=900)   # seconds
```

This requires the optional `cache` extra. Before 0.9.0 the library
called `requests_cache.install_cache()` at import time, so merely
importing tdapi made every HTTP client in your process cache for 15
minutes -- and any attachment you downloaded was written into the cache
database as well as to disk. If you were relying on that, pass
`cache_expire_after` explicitly.

Don't cache bulk reads. An export that walks every ticket never
revisits a URL, so the cache only costs you disk.

# Heads up/issues

This code is adapted from some internal code so there may be some
cruft (e.g. weird Python requirements) that are not actually needed
for what you see here.

# Development

```
make install    # uv sync with dev + test groups and the cache extra
make check      # lint + test
make security   # bandit + uv audit
```

# No warranty

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
