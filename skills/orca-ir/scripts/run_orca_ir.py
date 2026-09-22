#!/usr/bin/env python3
"""orca-ir — ORCA IR spectrum calculation runner (Part-2, stage_id=calc, spec=ir).

Type-B (ORCA) MSA skill. For every task whose ``target_spectra == "ir"`` it:
builds the ORCA ``.inp`` and a SLURM ``submit.sh`` from ``task["params"]`` (never
hard-coding functional / basis / solvent), uploads them together with the initial
XYZ to the cluster, submits the job via SLURM, polls to completion, downloads the
``.out``, and verifies ORCA terminated normally.

Chemistry choices come entirely from ``task["params"]`` (filled upstream by
param-recommendation); the ORCA/SLURM grammar comes from ``msa_shared.orca_input``;
the transport is ``common_utils.submit_slurm / poll_slurm / download_results`` (the
PIPELINE.md §7 scp-transfer + slurm-remote combination). This skill hard-codes no
functional, basis, solvent, or SLURM template of its own.

Input : upstream task_list JSON (--input)
Output: task_list JSON with calc_status / out_file / calc_wallclock / slurm_job_id
        (calc_error on failure) appended (--output)
"""
import os
import sys
import time
import argparse

# ---- msa_shared bootstrap (DO NOT MODIFY) ----
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from msa_shared.common_utils import (
    load_task_list, dump_task_list,
    mark_success, mark_failed, mark_skipped, upstream_ok,
    log_startup, read_shared_version, ensure_deps,
    submit_slurm, poll_slurm, download_results,
    resolve_cluster, ClusterConfigError,
)
from msa_shared import naming

MSA_CONTRACT_VERSION = "2.0"
MSA_REQUIRES = [('paramiko', 'paramiko>=3.0')]
STAGE_ID = "calc"
UPSTREAM_STAGE = "params"
SPEC = "ir"
# ---- end bootstrap ----

from msa_shared import orca_input
from msa_shared.orca_diagnostics import (
    classify_orca_output, expected_optimization_banners,
    recipe_candidates, summarize_attempts,
)


def build_ir_inputs(task: dict, cluster: dict, params=None) -> dict:
    """Write the ORCA ``.inp`` and SLURM ``submit.sh`` for one task to local paths.

    Reads ``task["params"]`` / ``task["molecule_name"]`` / ``task["xyz_file"]`` and
    delegates all grammar to ``orca_input``. Never mutates the task, never touches
    task_id. Returns the local/remote paths process_task needs to run the job:
      {inp_path, sh_path, out_path, xyz_file, out_name, remote_dir}
    Raises (FileNotFoundError / ValueError / NotImplementedError) on bad input;
    process_task converts any raise into calc_status="failed".
    """
    mol = task["molecule_name"]
    params = task.get("params") if params is None else params
    xyz_file = naming.resolve_initial_xyz(task)

    inp_path = naming.orca_inp(mol, SPEC)
    sh_path = naming.orca_submit_sh(mol, SPEC)
    out_path = naming.orca_out(mol, SPEC)
    job_name = f"{os.path.splitext(os.path.basename(inp_path))[0]}"

    inp_text = orca_input.build_inp(
        params, os.path.basename(xyz_file),
        charge=task.get("charge", 0), multiplicity=task.get("multiplicity", 1),
    )
    sh_text = orca_input.build_submit_sh(
        job_name, os.path.basename(inp_path), os.path.basename(out_path),
        orca_bin=cluster["orca_bin"], mpi_prefix=cluster["mpi_prefix"],
    )
    with open(inp_path, "w", encoding="utf-8") as f:
        f.write(inp_text)
    with open(sh_path, "w", encoding="utf-8") as f:
        f.write(sh_text)

    return {
        "inp_path": inp_path,
        "sh_path": sh_path,
        "out_path": out_path,
        "xyz_file": xyz_file,
        "out_name": os.path.basename(out_path),
        "remote_dir": naming.remote_work_dir(mol, cluster["remote_root"]),
    }


def process_task(task: dict, cluster: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract.

    Spec routing is a *pass-through*, not a skip: a task whose ``target_spectra``
    is not ``ir`` is returned untouched (no ``calc_status`` written) so the other
    five orca-{spec} skills can run over the same batch without clobbering this
    field. ``calc_status="skipped"`` is reserved for the real case — an IR task
    whose upstream ``params`` stage did not succeed.
    """
    if task.get("target_spectra") != SPEC:
        return task  # not our spectrum; leave the task completely untouched
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        conn = {k: cluster.get(k) for k in ("host", "user", "password", "key_file")}
        attempts = []
        t0 = time.time()
        candidates = recipe_candidates(task)
        for attempt_index, candidate in enumerate(candidates):
            paths = build_ir_inputs(task, cluster, params=candidate["params"])
            job_id = submit_slurm(
                paths["remote_dir"],
                script=os.path.basename(paths["sh_path"]),
                upload_files=[paths["inp_path"], paths["sh_path"], paths["xyz_file"]],
                **conn,
            )
            status = poll_slurm(job_id, **conn)
            if status != "COMPLETED":
                attempts.append({
                    "attempt": attempt_index, "params_source": candidate["params_source"],
                    "submission": "single", "changed_steps": candidate["changed_steps"],
                    "status": "failed", "failure_reason": "orca_terminated_abnormally",
                    "out_file": paths["out_path"], "slurm_job_id": job_id,
                })
                return mark_failed(
                    task, STAGE_ID, f"SLURM poll returned {status} (job {job_id})",
                    **summarize_attempts(task, attempts),
                )

            local_dir = os.path.dirname(paths["out_path"])
            downloaded, missing = download_results(
                paths["remote_dir"], [paths["out_name"]], local_dir, **conn,
            )
            if paths["out_name"] not in downloaded or not os.path.isfile(paths["out_path"]):
                attempts.append({
                    "attempt": attempt_index, "params_source": candidate["params_source"],
                    "submission": "single", "changed_steps": candidate["changed_steps"],
                    "status": "failed", "failure_reason": "orca_terminated_abnormally",
                    "out_file": paths["out_path"], "slurm_job_id": job_id,
                })
                return mark_failed(
                    task, STAGE_ID, f"ORCA .out not produced (missing={missing})",
                    **summarize_attempts(task, attempts),
                )

            with open(paths["out_path"], "r", encoding="utf-8", errors="replace") as f:
                out_text = f.read()
            diagnostic = classify_orca_output(
                out_text,
                expected_optimization_banners=expected_optimization_banners(candidate["params"]),
            )
            attempts.append({
                "attempt": attempt_index, "params_source": candidate["params_source"],
                "submission": "single", "changed_steps": candidate["changed_steps"],
                "status": "success" if diagnostic.verdict == "success" else "failed",
                "failure_reason": diagnostic.reason, "out_file": paths["out_path"],
                "slurm_job_id": job_id,
            })
            summary = summarize_attempts(task, attempts)
            if diagnostic.verdict == "success":
                return mark_success(
                    task, STAGE_ID, out_file=paths["out_path"],
                    calc_wallclock=int(time.time() - t0), slurm_job_id=job_id,
                    **summary,
                )
            if diagnostic.verdict != "retry" or attempt_index + 1 >= len(candidates):
                return mark_failed(task, STAGE_ID, diagnostic.reason, **summary)
        return mark_failed(task, STAGE_ID, "no ORCA recipe candidates")
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="orca-ir — ORCA IR calculation runner")
    parser.add_argument("--input", required=True, help="input task_list JSON path (default: none)")
    parser.add_argument("--output", required=True, help="output task_list JSON path (default: none)")
    args = parser.parse_args()

    log_startup("orca-ir")
    ensure_deps(MSA_REQUIRES, "orca-ir")
    shared_v = read_shared_version()
    if shared_v != MSA_CONTRACT_VERSION:
        print(
            f"ERROR: msa_contract_version mismatch (skill={MSA_CONTRACT_VERSION}, shared={shared_v})",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        tasks = load_task_list(args.input)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot load task_list from {args.input}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        cluster = resolve_cluster()
    except ClusterConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)

    tasks = [process_task(t, cluster) for t in tasks]
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    print(
        f"SUCCESS: orca-ir processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
