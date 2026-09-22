#!/usr/bin/env python3
"""orca-xanes — ORCA XANES calculation runner (Part-2, stage_id=calc, spec=xanes).

Type-B (ORCA) MSA skill. For every task whose ``target_spectra == "xanes"`` it
runs the expert three-step XANES recipe on the cluster. Unlike the other five
spectra, XANES cannot be a single chained input file: the core-excitation
TD-DFT step needs a ``%tddft orbwin[0] = a, b, -1, -1`` donor window whose two
integers are the orbital numbers of the target element's 1s core orbitals, and
those are only known after an intermediate single point has printed its ORBITAL
ENERGIES table. So this skill runs two cluster submissions with a local parse in
between:

  A. ``opt`` (step1) + ``SP KeepDens`` (step2), chained in one ``.inp`` via
     ``$new_job`` (the single point inherits the optimized geometry). Its ``.out``
     yields both the step2 ORBITAL ENERGIES table and the optimized geometry.
  -- locally parse the core-orbital window ``(a, b)`` for the task's
     ``target_element`` and recover the optimized geometry (``_xanes_orbitals``).
  B. core-excitation TD-DFT (step3) as a fresh single point on the optimized
     geometry, with ``orbwin[0] = a, b, -1, -1`` filled in. Its ``.out`` is the
     canonical ``<mol>-xanes_<element>.out`` the downstream plot-xanes skill
     renders; the edge element is part of the name because a XANES job is
     per-edge (``naming.job_variant``).

Chemistry choices (functional / basis / solvent / ZORA / nroots / MaxIter) come
from ``task["params"]`` (step1/step2/step3), the ORCA/SLURM grammar from
``msa_shared.orca_input``, the transport from ``common_utils``, and the K-edge
energy windows / parsing from the skill-local ``_xanes_orbitals``. This skill
hard-codes none of them.

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
SPEC = "xanes"
# ---- end bootstrap ----

from msa_shared import orca_input
from msa_shared.orca_diagnostics import (
    classify_orca_output, expected_optimization_banners,
    recipe_candidates, summarize_attempts,
)
import _xanes_orbitals


def _resolve_target_element(task):
    """Read and validate the XANES K-edge target element from the task.

    task-recognition stores it in ``task["extra_params"]["target_element"]`` and
    drops XANES tasks that lack it, so a missing/unsupported value here is a hard
    error (never silently defaulted to a wrong edge).
    """
    element = (task.get("extra_params") or {}).get("target_element")
    element = (element or "").strip().upper()
    if element not in _xanes_orbitals.SUPPORTED_XANES_ELEMENTS:
        raise ValueError(
            f"XANES task needs a supported target_element "
            f"({', '.join(_xanes_orbitals.SUPPORTED_XANES_ELEMENTS)}), got "
            f"{element!r}"
        )
    return element


def _write_job(inp_path, sh_path, params, xyz_filename, charge, multiplicity,
               out_path, cluster):
    """Write one ORCA ``.inp`` + SLURM ``submit.sh`` pair to local disk.

    ``params`` is a step dict (``{"step1": ...}`` or ``{"step1": ..., "step2":
    ...}``) consumed verbatim by ``orca_input.build_inp``; ``xyz_filename`` is the
    basename of the geometry uploaded alongside. Grammar/paths only — no chemistry.
    """
    job_name = os.path.splitext(os.path.basename(inp_path))[0]
    inp_text = orca_input.build_inp(
        params, os.path.basename(xyz_filename),
        charge=charge, multiplicity=multiplicity,
    )
    sh_text = orca_input.build_submit_sh(
        job_name, os.path.basename(inp_path), os.path.basename(out_path),
        orca_bin=cluster["orca_bin"], mpi_prefix=cluster["mpi_prefix"],
    )
    with open(inp_path, "w", encoding="utf-8") as f:
        f.write(inp_text)
    with open(sh_path, "w", encoding="utf-8") as f:
        f.write(sh_text)


def _run_submission(remote_dir, local_dir, inp_path, sh_path, out_path, xyz_path,
                    conn):
    """Submit, poll and download one job without interpreting ORCA output."""
    out_name = os.path.basename(out_path)
    job_id = submit_slurm(
        remote_dir, script=os.path.basename(sh_path),
        upload_files=[inp_path, sh_path, xyz_path], **conn,
    )
    status = poll_slurm(job_id, **conn)
    if status != "COMPLETED":
        return False, f"SLURM poll returned {status} (job {job_id})", job_id

    downloaded, missing = download_results(remote_dir, [out_name], local_dir, **conn)
    if out_name not in downloaded or not os.path.isfile(out_path):
        return False, f"ORCA .out not produced (missing={missing})", job_id

    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        return True, f.read(), job_id


def process_task(task: dict, cluster: dict) -> dict:
    """Single-task orchestrator. Never raises; enforces the fail-soft contract.

    Spec routing is a *pass-through*, not a skip: a task whose ``target_spectra``
    is not ``xanes`` is returned untouched (no ``calc_status`` written) so the
    other five orca-{spec} skills can run over the same batch without clobbering
    this field. ``calc_status="skipped"`` is reserved for the real case — a XANES
    task whose upstream ``params`` stage did not succeed.
    """
    if task.get("target_spectra") != SPEC:
        return task  # not our spectrum; leave the task completely untouched
    if not upstream_ok(task, UPSTREAM_STAGE):
        return mark_skipped(task, STAGE_ID, UPSTREAM_STAGE)
    try:
        mol = task["molecule_name"]
        params = task.get("params")
        if not isinstance(params, dict) or not all(
            k in params for k in ("step1", "step2", "step3")
        ):
            raise ValueError(
                "xanes params must carry step1 (opt), step2 (SP KeepDens) and "
                "step3 (core-excitation TD-DFT)"
            )
        xyz_file = naming.resolve_initial_xyz(task)

        target_element = _resolve_target_element(task)
        charge = task.get("charge", 0)
        multiplicity = task.get("multiplicity", 1)

        # A XANES job is genuinely per-edge, so its .inp/.sh/.out carry the
        # element (PIPELINE.md §3.2.1, iron rule 34) — otherwise a C-edge and an
        # N-edge run of the same molecule overwrite each other. job_variant() is
        # the naming-side source of truth; _resolve_target_element above has
        # already validated that the same element is supported.
        variant = naming.job_variant(task) or target_element

        conn = {k: cluster.get(k) for k in ("host", "user", "password", "key_file")}
        remote_dir = naming.remote_work_dir(mol, cluster["remote_root"])
        local_dir = naming.workspace_dir(mol)

        t0 = time.time()
        attempts = []

        # ---- Submission A: opt (step1) + SP KeepDens (step2), chained ----
        optsp_variant = f"{variant}_optsp"
        ab_inp = naming.orca_inp(mol, SPEC, variant=optsp_variant)
        ab_sh = naming.orca_submit_sh(mol, SPEC, variant=optsp_variant)
        ab_out = naming.orca_out(mol, SPEC, variant=optsp_variant)
        ab_text = None
        job_ab = None
        optsp_candidates = recipe_candidates(task, applicable_steps={"step2"})
        for candidate in optsp_candidates:
            ab_params = {
                "step1": candidate["params"]["step1"],
                "step2": candidate["params"]["step2"],
            }
            _write_job(
                ab_inp, ab_sh, ab_params, xyz_file, charge, multiplicity,
                ab_out, cluster,
            )
            ok, output, job_ab = _run_submission(
                remote_dir, local_dir, ab_inp, ab_sh, ab_out, xyz_file, conn,
            )
            if not ok:
                attempts.append({
                    "attempt": len(attempts),
                    "params_source": candidate["params_source"],
                    "submission": "optsp",
                    "changed_steps": candidate["changed_steps"],
                    "status": "failed",
                    "failure_reason": "orca_terminated_abnormally",
                    "out_file": ab_out,
                    "slurm_job_id": job_ab,
                })
                return mark_failed(
                    task, STAGE_ID, f"XANES opt/SP step: {output}",
                    **summarize_attempts(task, attempts, production_submission="core"),
                )
            diagnostic = classify_orca_output(
                output,
                expected_optimization_banners=expected_optimization_banners(ab_params),
            )
            attempts.append({
                "attempt": len(attempts),
                "params_source": candidate["params_source"],
                "submission": "optsp",
                "changed_steps": candidate["changed_steps"],
                "status": "success" if diagnostic.verdict == "success" else "failed",
                "failure_reason": diagnostic.reason,
                "out_file": ab_out,
                "slurm_job_id": job_ab,
            })
            if diagnostic.verdict == "success":
                ab_text = output
                break
            if diagnostic.verdict != "retry":
                return mark_failed(
                    task, STAGE_ID, f"XANES opt/SP step: {diagnostic.reason}",
                    **summarize_attempts(task, attempts, production_submission="core"),
                )
        if ab_text is None:
            return mark_failed(
                task, STAGE_ID, "XANES opt/SP fallback recipes exhausted",
                **summarize_attempts(task, attempts, production_submission="core"),
            )

        # ---- Local parse: core-orbital window + optimized geometry ----
        orb_start, orb_end = _xanes_orbitals.parse_core_orbital_range(
            ab_text, target_element
        )
        atoms = _xanes_orbitals.extract_last_geometry(ab_text)
        opt_xyz = naming.xyz_opt(mol)
        with open(opt_xyz, "w", encoding="utf-8") as f:
            f.write(_xanes_orbitals.geometry_to_xyz(
                atoms, comment=f"{mol} xanes-optimized geometry"
            ))

        # ---- Submission B: core-excitation TD-DFT (step3) with orbwin ----
        c_inp = naming.orca_inp(mol, SPEC, variant=variant)
        c_sh = naming.orca_submit_sh(mol, SPEC, variant=variant)
        c_out = naming.orca_out(mol, SPEC, variant=variant)
        core_candidates = recipe_candidates(task, applicable_steps={"step3"})
        for candidate in core_candidates:
            step3 = dict(candidate["params"]["step3"])
            step3["orb_win"] = [orb_start, orb_end, -1, -1]
            core_params = {"step1": step3}
            _write_job(
                c_inp, c_sh, core_params, opt_xyz, charge, multiplicity,
                c_out, cluster,
            )
            ok, output, job_c = _run_submission(
                remote_dir, local_dir, c_inp, c_sh, c_out, opt_xyz, conn,
            )
            if not ok:
                attempts.append({
                    "attempt": len(attempts),
                    "params_source": candidate["params_source"],
                    "submission": "core",
                    "changed_steps": candidate["changed_steps"],
                    "status": "failed",
                    "failure_reason": "orca_terminated_abnormally",
                    "out_file": c_out,
                    "slurm_job_id": job_c,
                })
                return mark_failed(
                    task, STAGE_ID, f"XANES TD-DFT step: {output}",
                    **summarize_attempts(task, attempts, production_submission="core"),
                )
            diagnostic = classify_orca_output(output)
            attempts.append({
                "attempt": len(attempts),
                "params_source": candidate["params_source"],
                "submission": "core",
                "changed_steps": candidate["changed_steps"],
                "status": "success" if diagnostic.verdict == "success" else "failed",
                "failure_reason": diagnostic.reason,
                "out_file": c_out,
                "slurm_job_id": job_c,
            })
            summary = summarize_attempts(
                task, attempts, production_submission="core"
            )
            if diagnostic.verdict == "success":
                return mark_success(
                    task, STAGE_ID,
                    out_file=c_out,
                    calc_wallclock=int(time.time() - t0),
                    slurm_job_id=job_c,
                    xanes_orbwin=[orb_start, orb_end, -1, -1],
                    xanes_target_element=target_element,
                    xanes_optsp_out=ab_out,
                    xanes_optsp_job_id=job_ab,
                    **summary,
                )
            if diagnostic.verdict != "retry":
                return mark_failed(
                    task, STAGE_ID, f"XANES TD-DFT step: {diagnostic.reason}",
                    **summary,
                )
        return mark_failed(
            task, STAGE_ID, "XANES TD-DFT fallback recipes exhausted",
            **summarize_attempts(task, attempts, production_submission="core"),
        )
    except Exception as e:  # noqa: BLE001 — fail soft, never abort the batch
        return mark_failed(task, STAGE_ID, f"{type(e).__name__}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="orca-xanes — ORCA XANES calculation runner")
    parser.add_argument("--input", required=True, help="input task_list JSON path (default: none)")
    parser.add_argument("--output", required=True, help="output task_list JSON path (default: none)")
    args = parser.parse_args()

    log_startup("orca-xanes")
    ensure_deps(MSA_REQUIRES, "orca-xanes")
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
        f"SUCCESS: orca-xanes processed {len(tasks)} tasks "
        f"(ok={n_ok}, failed={n_fail}, skipped={n_skip}) -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
