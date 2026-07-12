"""Shared pytest fixtures.

Test isolation for the router's rate-limit cooldown map.

`friday.core.router._RATE_LIMITED` is a MODULE-GLOBAL dict that records, per model, a wall-clock
time (~120 s out) until which the model is skipped after it rate-limits. It intentionally persists
across calls within a process. In the test suite that persistence is a hazard: if any test marks a
model as cooling down and doesn't clear it (e.g. a test exercising the 429 fallback path), a *later*
test inherits that cooldown. The router's local-first tests then see the local model as "cooling
down", skip it, and fail their "local is tried first" assertions.

Because a cooldown expires on wall-clock time, whether the leak bites depends on how close the two
tests run and how fast the machine is — so it surfaces as a flaky, order- and environment-dependent
CI failure (green locally, red on a slower CI runner) that's painful to trace. Reset the map around
every test so no test can ever inherit another's cooldown state. Clearing a cooldown dict has no
effect on any test's own behaviour, so this is safe for the whole suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_router_cooldowns():
    from friday.core import router

    router._RATE_LIMITED.clear()
    yield
    router._RATE_LIMITED.clear()
