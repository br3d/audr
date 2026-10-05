"""Container health check: `python -m audr.operations.healthcheck` (AUD-439).

This exists so `compose.yaml` can say

    test: ["CMD", "python", "-m", "audr.operations.healthcheck"]

instead of embedding a seven-line Python program in YAML. The rules it has to
honour are not obvious, so they live here rather than in a comment an operator
has to read before they can read the service definition:

* **stdlib only, no `curl`.** `curl` is not in the runtime image and installing
  it would add an unpinned apt layer for one HTTP request the interpreter
  already there can make. See docs/third-party.md section 1.3.
* **Exceptions are caught and printed, never raised.** An escaping traceback
  writes ~2KB into `.State.Health.Log`, of which Docker keeps only the last 5
  entries at 4KB each — so one failure crowds out the useful history. One line
  ("[Errno 111] Connection refused") says the same thing.
* **`/health/live` only.** The readiness probe (`/health/ready`) touches the
  database and the master key; using it here would make the container unhealthy
  — and therefore restarted — for an outage it cannot fix by restarting.
* **The port is fixed at 8000.** Only the host side of the published port is
  configurable (`AUDR_HTTP_PORT`); inside the container the app always listens
  on 8000, which is also what the deploy health-gate talks to.

Exit code 0 means live, 1 means anything else.
"""

from __future__ import annotations

import sys
import urllib.request

URL = "http://localhost:8000/health/live"
TIMEOUT_SECONDS = 4.0


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except Exception as exc:  # noqa: BLE001 - one readable line beats a traceback
        print(exc)
        return 1
    if status != 200:
        print(f"{URL} returned HTTP {status}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
