#!/usr/bin/env python3
"""orca-nmr — ORCA NMR shielding calculation runner (Part-2, stage_id=calc, spec=nmr).

Type-B (ORCA) MSA skill. For every task whose ``target_spectra == "nmr"`` it:
builds a two-step ORCA ``.inp`` (ground-state ``opt`` -> ``nmr`` shielding single
point, chained in one file with ``$new_job``) and a SLURM ``submit.sh`` from
``task["params"]`` (never hard-coding functional / basis / solvent), uploads them
together with the initial XYZ to the cluster, submits the job via SLURM, polls to
completion, downloads the ``.out``, and verifies ORCA terminated normally.

The NMR recipe is two steps: a ground-state geometry optimization followed by a
shielding single point (ORCA simple keyword ``NMR``) whose isotropic shieldings
the downstream plot-nmr skill turns into a spectrum. The shielding step emits a
``%eprnmr`` block naming the nuclei to compute; ionic systems additionally get a
tightened ``%scf`` (both come from ``task["params"]``). The shielding job inherits
the optimized geometry from the opt job via the ORCA multi-job coordinate
carry-over (a filename-less ``* xyzfile`` line), so only the initial XYZ is
uploaded.

Per-molecule nucleus merge: task-recognition splits a "¹H and ¹³C" request into
one NMR task per nucleus, but shielding for every nucleus of a molecule comes from
the *same* electronic structure, so running two jobs would just repeat the opt +
SCF. This runner therefore groups the runnable NMR tasks by molecule and submits
ONE shielding job per molecule whose ``%eprnmr`` block carries one ``NUCLEI`` line
per requested element (``orca_input`` builds that from the union of the group's
nuclei). Every task in the group is then stamped with the *same* ``.out`` /
``slurm_job_id`` / wallclock; the downstream plot-nmr and export stages filter that
shared CHEMICAL SHIELDING SUMMARY by each task's ``target_nucleus``, so a molecule
still yields one figure and one peaks CSV per nucleus.

Chemistry choices come from ``task["params"]``, the ORCA/SLURM grammar from
``msa_shared.orca_input``, and the transport from ``common_utils``. This skill
hard-codes none of them.

Input : upstream task_list JSON (--input)
Output: task_list JSON with calc_status / out_file / calc_wallclock / slurm_job_id
        (calc_error on failure) appended (--output)
"""
import copy
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
SPEC = "nmr"
# ---- end bootstrap ----

from msa_shared import orca_input
from msa_shared.orca_diagnostics import (
    classify_orca_output, expected_optimization_banners,
    recipe_candidates, summarize_attempts,
)


def build_nmr_inputs(task: dict, nuclei: list, cluster: dict, params=None) -> dict:
    """Write the ORCA ``.inp`` and SLURM ``submit.sh`` for one molecule's shielding
    job to local paths.

    ``task`` is any representative task of the per-molecule group (they share
    identical params except the requested nucleus); ``nuclei`` is the union of the
    group's element letters (e.g. ``["H", "C"]``). Reads
    ``task["params"]`` / ``task["molecule_name"]`` / ``task["xyz_file"]`` and
    delegates all grammar to ``orca_input``. The params are **deep-copied** and the
    shielding step (``calc_type == "nmr"``) is given ``nmr_nuclei = nuclei`` (its
    single-element ``nmr_nucleus`` is dropped) so ``orca_input`` emits one
    ``NUCLEI = ALL <X> {SHIFT}`` line per element in one ``%eprnmr`` block — the
    original task dicts are never mutated. The nmr recipe carries two steps (opt
    then nmr); ``orca_input.build_inp`` chains them with ``$new_job`` and the
    shielding job inherits the optimized geometry, so only the initial XYZ is
    uploaded. Never touches task_id. Returns the local/remote paths the group
    runner needs:
      {inp_path, sh_path, out_path, xyz_file, out_name, remote_dir}
    Raises (FileNotFoundError / ValueError / NotImplementedError) on bad input;
    the caller converts any raise into calc_status="failed".
    """
    mol = task["molecule_name"]
    params = copy.deepcopy(task.get("params") if params is None else params)
    for step in params.values():
        if isinstance(step, dict) and step.get("calc_type") == "nmr":
            step["nmr_nuclei"] = list(nuclei)
            step.pop("nmr_nucleus", None)
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


def _group_nuclei(group: list) -> list:
    """Element letters requested across a per-molecule group of NMR tasks.

    Reads each task's shielding step (``params["step*"]`` with ``calc_type ==
    "nmr"``) ``nmr_nucleus`` (falling back to the task-level ``target_nucleus``),
    in task order. Normalization / de-duplication / canonical ordering is left to
    ``orca_input`` (``_nmr_nuclei``), so this simply collects the raw letters.
    """
    nuclei = []
    for t in group:
        letter = None
        for step in (t.get("params") or {}).values():
            if isinstance(step, dict) and step.get("calc_type") == "nmr":
                letter = step.get("nmr_nucleus")
                break
        if not letter:
            letter = t.get("target_nucleus")  # e.g. "1H" / "13C"; orca_input maps it
        nuclei.append(letter or "H")
    return nuclei


def run_group(group: list, cluster: dict) -> list:
    """Run ONE ORCA shielding job for a group of same-molecule NMR tasks.

    Every task in ``group`` is an NMR task of the same molecule whose upstream
    ``params`` stage succeeded. Because shielding for all of a molecule's nuclei
    comes from one electronic-structure calculation, a single job computes them
    all: the ``%eprnmr`` block carries one ``NUCLEI`` line per requested element.
    Returns one result task per input task, order-aligned, all stamped with the
    **same** ``out_file`` / ``slurm_job_id`` / ``calc_wallclock`` on success (so the
    downstream plot/export stages read the shared ``.out`` and filter it per
    nucleus). Never raises; enforces the fail-soft contract — on any failure every
    task in the group is marked failed with the same reason.
    """
    try:
        nuclei = _group_nuclei(group)
        conn = {k: cluster.get(k) for k in ("host", "user", "password", "key_file")}
        attempts = []
        t0 = time.time()
        candidates = recipe_candidates(group[0])
        for attempt_index, candidate in enumerate(candidates):
            paths = build_nmr_inputs(
                group[0], nuclei, cluster, params=candidate["params"],
            )
            job_id = submit_slurm(
                paths["remote_dir"], script=os.path.basename(paths["sh_path"]),
                upload_files=[paths["inp_path"], paths["sh_path"], paths["xyz_file"]], **conn,
            )
            status = poll_slurm(job_id, **conn)
            if status != "COMPLETED":
                attempts.append({
                    "attempt": attempt_index, "params_source": candidate["params_source"],
                    "submission": "single", "changed_steps": candidate["changed_steps"],
                    "status": "failed", "failure_reason": "orca_terminated_abnormally",
                    "out_file": paths["out_path"], "slurm_job_id": job_id,
                })
                reason = f"SLURM poll returned {status} (job {job_id})"
                return [mark_failed(t, STAGE_ID, reason,
                                    **summarize_attempts(t, attempts)) for t in group]
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
                reason = f"ORCA .out not produced (missing={missing})"
                return [mark_failed(t, STAGE_ID, reason,
                                    **summarize_attempts(t, attempts)) for t in group]
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
            if diagnostic.verdict == "success":
                wallclock = int(time.time() - t0)
                return [
                    mark_success(
                        t, STAGE_ID, out_file=paths["out_path"],
                        calc_wallclock=wallclock, slurm_job_id=job_id,
                        **summarize_attempts(t, attempts),
                    )
                    for t in group
                ]
            if diagnostic.verdict != "retry" or attempt_index + 1 >= len(candidates):
                return [mark_failed(t, STAGE_ID, diagnostic.reason,
                                    **summarize_attempts(t, attempts)) for t in group]
        return [mark_failed(t, STAGE_ID, "no ORCA recipe candidates") for t in group]
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        reason = f"{type(e).__name__}: {e}"
        return [mark_failed(t, STAGE_ID, reason) for t in group]


def process_task(task: dict, cluster: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract.

    Spec routing is a *pass-through*, not a skip: a task whose ``target_spectra``
    is not ``nmr`` is returned untouched (no ``calc_status`` written) so the other
    five orca-{spec} skills can run over the same batch without clobbering this
    field. ``calc_status="skipped"`` is reserved for the real case — an NMR task
    whose upstream ``params`` stage did not succeed.
    """
    if task.get("target_spectra") != SPEC:
        return task  # not our spectrum; leave the task completely untouched
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    # A single runnable NMR task is just a group of one (one nucleus -> the compact
    # one-liner %eprnmr block); reuse the group runner so there is one code path.
    return run_group([task], cluster)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="orca-nmr — ORCA NMR shielding calculation runner")
    parser.add_argument("--input", required=True, help="input task_list JSON path (default: none)")
    parser.add_argument("--output", required=True, help="output task_list JSON path (default: none)")
    args = parser.parse_args()

    log_startup("orca-nmr")
    ensure_deps(MSA_REQUIRES, "orca-nmr")
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

    # Per-molecule merge: runnable NMR tasks of the same molecule share ONE
    # shielding job (one %eprnmr block, one NUCLEI line per nucleus). Non-runnable
    # tasks (not our spectrum, or upstream params not ok) are resolved inline by
    # process_task without launching anything. Original task order is preserved.
    results = [None] * len(tasks)
    groups: dict = {}  # molecule_name -> list of (index, task)
    for i, t in enumerate(tasks):
        if t.get("target_spectra") == SPEC and upstream_ok(t, UPSTREAM_STAGE):
            groups.setdefault(t.get("molecule_name"), []).append((i, t))
        else:
            results[i] = process_task(t, cluster)
    for _mol, items in groups.items():
        group_results = run_group([t for _, t in items], cluster)
        for (i, _), r in zip(items, group_results):
            results[i] = r
    tasks = results
    dump_task_list(tasks, args.output)

    n_ok = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "success")
    n_fail = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "failed")
    n_skip = sum(1 for t in tasks if t.get(f"{STAGE_ID}_status") == "skipped")
    print(
        f"SUCCESS: orca-nmr processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
