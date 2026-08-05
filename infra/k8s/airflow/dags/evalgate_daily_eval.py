"""The daily eval DAG: full 96-case suite against the deployed model server.

This is the QUALITY instrument. The PR gate is a different instrument with a
different config and no thresholds of its own -- see
`packages/evalcore/src/evalcore/gate_config.py` for why they are separate.

SIZING, all from measurement rather than estimate
-------------------------------------------------
    2.90 h    measured wall time, 96 cases, node at 108.89 s/case
    2.9285 h  THIS DAG, end to end, 2026-08-05: 96 cases, 0 errors, 109.50 s/case.
              +1.0% on the sizing above, of which 24.7 s is orchestration.
    3.80 h    projection from the 12-case soak on the LONGEST prompts, i.e.
              sustained rather than burst rates
    6 h       dagrun_timeout, 1.6x the sustained projection; the run above used
              49% of it

The deadline is sized on 3.80, not on the 2.75 h a three-case smoke suggested.
That distinction is not academic: the soak that produced these numbers was itself
killed by a deadline set from burst rates, at 11 of 12 cases.

WHY THE EVAL TASK HAS NO RETRIES
--------------------------------
A retry of a 3-hour task cannot fit inside a 6-hour deadline alongside the
original attempt, so `retries=1` would mostly convert a failure into a
DeadlineExceeded and lose the partial evidence too. The staleness bound already
tolerates one missed night (36 h vs a 27.8 h floor), so the correct response to a
failed run is tomorrow's run, not an immediate retry.

AIRFLOW 3 CONSTRAINTS THIS DAG RESPECTS
---------------------------------------
  * Task code must not touch the metadata DB -- `RuntimeError: Direct database
    access via the ORM is not allowed in Airflow 3.0`. State travels by XCom.
  * Deferrable operators are barred while the triggerer is disabled: a deferred
    task with no triggerer hangs in `deferred` forever rather than erroring.
  * LocalExecutor runs tasks as subprocesses inside the scheduler's cgroup, whose
    1000m limit is a hard ceiling on all DAG execution. Inference therefore runs
    in a SEPARATE pod (KubernetesPodOperator), not in the task process -- the
    model server's 2500m budget is its own, and the scheduler must not host a
    2.9-hour CPU workload.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta

from airflow.sdk import dag, task
from kubernetes.client import models as k8s

from airflow.providers.cncf.kubernetes.operators.pod import (  # isort: skip
    KubernetesPodOperator,
)

NAMESPACE = "evalgate"
MODEL_URL = "http://evalgate-model.evalgate.svc.cluster.local:8080"
IMAGE = "evalgate-api:0.1.0"

# Declared, not detected. The gate refuses to diff runs whose backends differ,
# and this is the node: llama.cpp compiled for aarch64 CPU, no Metal, no CUDA.
BACKEND = "cpu-aarch64"

SUITE = "/eval/suite.json"
OUT_DIR = "/out/daily"
BASELINE = "/out/baseline/run_baseline.json"

# 6 h. See the module docstring: sized on the 3.80 h sustained projection.
DAGRUN_TIMEOUT_H = 6

_INPUTS = k8s.V1Volume(
    name="eval",
    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
        claim_name="evalgate-models", read_only=True
    ),
)
_RESULTS = k8s.V1Volume(
    name="results",
    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(claim_name="evalgate-results"),
)
_MOUNTS = [
    # Inputs share the volume with the hash-gated weights and are mounted
    # readOnly. Results are a separate claim entirely -- a completed 96-case run
    # was once discarded at its final write because the two shared a mount.
    k8s.V1VolumeMount(name="eval", mount_path="/eval", sub_path="eval", read_only=True),
    k8s.V1VolumeMount(name="results", mount_path="/out"),
]
_SECURITY = k8s.V1PodSecurityContext(run_as_non_root=True, run_as_user=10001, run_as_group=10001)


def _pod(task_id: str, arguments: list[str], timeout_h: int) -> KubernetesPodOperator:
    return KubernetesPodOperator(
        task_id=task_id,
        namespace=NAMESPACE,
        image=IMAGE,
        image_pull_policy="IfNotPresent",  # no registry; the image lives in containerd
        cmds=["/app/.venv/bin/evalgate-eval"],
        arguments=arguments,
        name=f"evalgate-{task_id}",
        volumes=[_INPUTS, _RESULTS],
        volume_mounts=_MOUNTS,
        security_context=_SECURITY,
        container_resources=k8s.V1ResourceRequirements(
            # Small on purpose: this container waits on HTTP and scores JSON. The
            # work is in the model server's cgroup, and this must not compete
            # with the thing it is measuring.
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "500m", "memory": "512Mi"},
        ),
        get_logs=True,
        # The pod is deleted on success and KEPT on failure, so a failed run can
        # be read afterwards instead of only inferred from Airflow's log.
        on_finish_action="delete_succeeded_pod",
        startup_timeout_seconds=300,
        # NOT deferrable. The triggerer is disabled; see the module docstring.
        deferrable=False,
        execution_timeout=timedelta(hours=timeout_h),
    )


@dag(
    dag_id="evalgate_daily_eval",
    description="Full 96-case golden suite against the deployed model server, then gate.",
    # 08:00 UTC = 03:00 America/Chicago. Quiet hour on a node whose binding
    # constraint is CPU and which also serves a public API; inference at 2500m
    # would otherwise contend with daytime traffic.
    #
    # WAS 03:00 UTC until 2026-08-05, taken from the "nightly batch" habit and
    # never checked against the operator's clock: 03:00 UTC is 22:00 Chicago, so
    # the 2.9 h run occupied the node every *evening*. Only the cron instant
    # moves. The interval is still 24 h, so the 6 h deadline and the 36 h
    # staleness bound keep their derivations untouched -- see gate_config.py.
    #
    # The timezone stays UTC and is NOT America/Chicago. A local-timezone cron
    # skips a run at the spring-forward transition and runs twice at the autumn
    # one, which on a 2.9 h job means either a missing day the staleness bound
    # will flag or two runs contending for the same 2500m. Accepted cost: when
    # Chicago returns to CST this fires at 02:00 local rather than 03:00.
    schedule="0 8 * * *",
    # MUST BE IN THE PAST, and this is not a formality. `DagRun` builds task
    # instances only for tasks whose start_date is at or before the run's logical
    # date, and a DAG-level start_date propagates to every task. This was written
    # as 2026-08-05 while the DAG was landing on 2026-08-04, and the result was
    # not an error: two manual runs produced ZERO task instances and were marked
    # `success` in 12 s and 0.03 s. A green DAG run that ran nothing is the one
    # outcome this project cannot afford to render as green -- the entire staleness
    # design exists to tell a regression apart from a dead DAG. It is caught
    # downstream (publish_latest never runs, so `latest.json` never restamps and
    # the PR gate ages out), but it is caught a day late.
    start_date=datetime(2026, 8, 4),
    # Load-bearing. Without it, a DAG paused for a week backfills seven 3-hour
    # runs on one node the moment it is unpaused.
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=DAGRUN_TIMEOUT_H),
    default_args={"retries": 0},
    tags=["evalgate", "eval", "p3"],
)
def evalgate_daily_eval():
    @task
    def preflight() -> dict:
        """Refuse to start a 3-hour run against a server that is not ready.

        `/health` is 200 only after the weights are loaded and warmed -- on this
        build the socket is not even open before then. Checking it here turns a
        three-hour walk into a ten-second failure.
        """
        with urllib.request.urlopen(f"{MODEL_URL}/health", timeout=30) as r:
            health = json.loads(r.read().decode())
        with urllib.request.urlopen(f"{MODEL_URL}/props", timeout=30) as r:
            props = json.loads(r.read().decode())
        model_path = props.get("model_path", "")
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        if n_ctx != 8192:
            raise RuntimeError(f"model server n_ctx is {n_ctx}, expected 8192")
        return {"health": health, "model_path": model_path, "build_info": props.get("build_info")}

    run_suite = _pod(
        "run_suite",
        [
            "run",
            "--suite",
            SUITE,
            "--out",
            f"{OUT_DIR}/run_{{{{ ds }}}}.json",
            # Per-case flush. A killed pod then leaves every completed case on
            # disk instead of nothing.
            "--progress-jsonl",
            f"{OUT_DIR}/run_{{{{ ds }}}}.progress.jsonl",
            "--server-url",
            MODEL_URL,
            "--backend",
            BACKEND,
            "--model-version",
            "v1-iter2000",
            "--quantization",
            "Q4_K_M",
            "--run-id",
            "daily-{{ ds }}",
            "--judge",
            # 900 s against a measured worst case of 302 s. The default 180 s
            # would record the longest prompts as per-case errors, which in a run
            # artifact are indistinguishable from real model failures.
            "--timeout-s",
            "900",
        ],
        timeout_h=5,
    )

    gate = _pod(
        "gate",
        [
            "gate",
            "--suite",
            SUITE,
            "--baseline",
            BASELINE,
            "--candidate",
            f"{OUT_DIR}/run_{{{{ ds }}}}.json",
            "--json",
            f"{OUT_DIR}/verdict_{{{{ ds }}}}.json",
            "--comment",
            f"{OUT_DIR}/comment_{{{{ ds }}}}.md",
            "--html",
            f"{OUT_DIR}/report_{{{{ ds }}}}.html",
        ],
        timeout_h=1,
    )

    publish = _pod(
        "publish_latest",
        # `gate` exits 1 on a breach, which fails that task -- correct, the daily
        # run should go red on a regression. This task runs regardless so the PR
        # gate can still see a FRESH verdict: a regression must block merges, and
        # so must a silently dead DAG, but the two have to be distinguishable.
        [
            "publish-latest",
            "--verdict",
            f"{OUT_DIR}/verdict_{{{{ ds }}}}.json",
            "--out",
            "/out/daily/latest.json",
        ],
        timeout_h=1,
    )
    publish.trigger_rule = "all_done"

    @task(trigger_rule="all_success")
    def finalize() -> str:
        """Exists so the DAG RUN's colour means what a reader assumes it means.

        A DagRun's state is decided by its LEAF tasks. With `publish_latest` the
        only leaf and its `all_done` rule, EVERY outcome came out green -- measured
        2026-08-04, before this task existed: a `run_suite` that died in 6 s on a
        suite-validation error produced `state=success` on the run, and so would a
        real regression, since `gate` exiting 1 is upstream of an all_done leaf.

        That is exactly the confusion the whole staleness design exists to prevent
        one level up, reappearing one level down. BUILD_PLAN's P3 exit is "three
        consecutive green days", and three empty runs would have satisfied it.

        ALL_SUCCESS, and depending on `gate` as well as `publish_latest`, because a
        trigger rule sees only DIRECT upstreams: hanging this off `publish` alone
        would go green whenever publish did, which is the bug it is meant to fix.
        `publish_latest` keeps `all_done` so a bad night still stamps a verdict for
        the PR gate to read -- the two signals are deliberately separate.
        """
        return "ok"

    done = finalize()
    preflight() >> run_suite >> gate >> publish >> done
    gate >> done


evalgate_daily_eval()
