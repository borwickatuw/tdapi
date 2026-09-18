# To do list

- url_helper() to remove TDConnection logic

- move __init__ code into another module e.g. core.py

- move more logic into obj.py with standards e.g. object_class() not named classes

- replace the `assert len(td_struct) == 1` invariants in the object
  wrappers (asset, cmdb, person, ticket) with raises. They are
  suppressed in ruff's per-file-ignores today, and they vanish entirely
  under `python -O`, which would turn "the API returned something
  unexpected" into silently taking the first element.

- cover the legacy per-object wrappers (asset, cmdb, group, kb, person,
  project) with tests, and raise the coverage floor as that lands.

## Done

- ~~add timeout for requests~~ -- 0.9.0

- ~~unit tests~~ -- 0.9.0, covering the transport layer

- ~~test requests_cache -- did I break something? does not seem to use
  the cache~~ -- 0.9.0: it was installed globally at import time and is
  now opt-in per connection, so there is nothing left to be confused by
