"""Integration smoke DAG. Proves the surface and nothing more.

Two things have to be true before the daily eval DAG is worth writing:
Airflow can reach the EvalGate API over in-cluster DNS, and it can talk to its
own metadata database. This DAG asserts exactly those and stops.

Deliberately NOT deferrable. The triggerer is disabled in values.yaml, and a
deferred task with no triggerer does not error — it hangs in `deferred`
forever. See the convention note there.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from airflow.sdk import dag, task

API = "http://evalgate-api.evalgate.svc.cluster.local"


@dag(
    dag_id="evalgate_smoke",
    description="Integration proof: EvalGate API reachable, metadata DB writable.",
    schedule=None,  # triggered by hand; the daily schedule belongs to the eval DAG
    start_date=datetime(2026, 8, 1),
    catchup=False,
    tags=["evalgate", "smoke", "p3"],
    max_active_runs=1,
)
def evalgate_smoke():
    @task
    def call_api() -> dict:
        """Reach the API by ClusterIP DNS, not by the public address.

        Using the in-cluster name is the point: it proves the path the daily
        eval DAG will actually use, and it keeps the check independent of
        traefik, the NSG, and the public IP.
        """
        out = {}
        for path in ("/health", "/ready"):
            req = urllib.request.Request(f"{API}{path}")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode())
                assert r.status == 200, f"{path} returned {r.status}"
                out[path] = body
        # /ready queries Postgres, so a 200 here also proves the API's own
        # database path is alive — not just that the process is up.
        assert out["/ready"]["status"] == "ready", out["/ready"]
        print(f"api ok: {out}")
        return out

    @task
    def check_metadata_db(api_result: dict) -> str:
        """Round-trip state through the metadata database.

        NOT via the ORM. Airflow 3 forbids it outright — task code runs under
        the Task SDK in a supervised subprocess and a `create_session()` call
        raises:

            RuntimeError: Direct database access via the ORM is not allowed
            in Airflow 3.0

        Measured here on 2026-08-03 before this was rewritten. The supported
        path is the api-server, and it is the one the daily eval DAG will use,
        so proving it is worth more than a raw SELECT would have been.

        Two round trips, both landing in the metadata database:

          * `api_result` arrived as an XCom pushed by the upstream task, which
            means it was serialised into the DB and read back out;
          * the Variable below is written and re-read within this task.

        Between them, they prove the out-of-band `airflow-metadata` secret
        resolves, the separate `airflow` role and database exist, and the
        migration ran — none of which the API check upstream would catch.
        """
        from airflow.sdk import Variable

        assert api_result["/ready"]["status"] == "ready", api_result
        print(f"xcom round-trip ok: {api_result}")

        key, want = "evalgate_smoke_probe", "ok"
        Variable.set(key, want)
        got = Variable.get(key)
        assert got == want, f"variable round-trip returned {got!r}, expected {want!r}"
        print(f"variable round-trip ok: {key}={got}")
        return got

    check_metadata_db(call_api())


evalgate_smoke()
