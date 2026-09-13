import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # nf_tui, serve
sys.path.insert(0, str(ROOT / "tests"))  # generate_run


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """When a MinIO test fails, say what state the store was in at that moment.

    One run of these tests failed 7 of 20 and could not be reproduced — not
    normally, not under full CPU load. Its output wasn't kept. Every MinIO
    failure now carries the store's health and the container's state, so the
    next one explains itself instead of costing an afternoon.
    """
    outcome = yield
    rep = outcome.get_result()
    info = getattr(item, "_minio", None)
    if info is None or not rep.failed or call.when != "call":
        return
    import subprocess
    import urllib.request
    try:
        with urllib.request.urlopen(f"{info.endpoint}/minio/health/live",
                                    timeout=3) as r:
            health = f"HTTP {r.status}"
    except Exception as e:                              # noqa: BLE001
        health = f"unreachable ({e})"
    state = subprocess.run(
        ["docker", "inspect", "-f",
         "{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} "
         "started={{.State.StartedAt}}", info.container],
        capture_output=True, text=True).stdout.strip() or "container is gone"
    rep.sections.append(("MinIO at failure",
                         f"endpoint  {info.endpoint}\nhealth    {health}\n"
                         f"container {info.container}: {state}"))
