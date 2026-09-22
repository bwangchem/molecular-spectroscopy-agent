"""Shared runtime utilities for MSA skills.

Provides:
- load_task_list / dump_task_list: JSON IO for the pipeline task_list contract.
- mark_success / mark_failed / mark_skipped: standardized status writes.
- upstream_ok: check whether an upstream stage succeeded.
- read_shared_version / log_startup: version + debug logging.
- submit_slurm / poll_slurm / download_results: SLURM remote transport (calc skills).

Skills locate this package by self-location: each skill's entry script puts its
own ``scripts/`` directory (which holds a vendored copy of ``msa_shared``) on
``sys.path`` via ``os.path.dirname(os.path.abspath(__file__))``. There is no
environment variable to set and no dependency on any repository layout.
"""

import json
import os
import sys
from typing import List, Dict, Any

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def read_shared_version() -> str:
    vp = os.path.join(_PKG_DIR, "VERSION")
    try:
        with open(vp, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError:
        return "unknown"


def log_startup(skill_name: str) -> None:
    """Every skill should call this at CLI entry so version mismatches surface."""
    print(f"[{skill_name}] msa_shared v{read_shared_version()}", file=sys.stderr)


# ---------- dependency preflight ----------
#
# Third-party libraries (rdkit / numpy / matplotlib / paramiko / pubchempy) are
# compiled wheels / C-extensions: they CANNOT be vendored as source the way the
# pure-Python msa_shared modules are. At runtime a skill is loaded by an
# arbitrary host Agent, so a missing library must fail *loudly and actionably*
# rather than silently degrading task after task. Each skill declares its
# ``MSA_REQUIRES`` at module scope and calls ``ensure_deps(...)`` at the top of
# ``main()``; this is the single, uniform place environment problems surface.

ENV_MISSING_EXIT = 4  # environment/deps not provisioned (distinct from 1/2/3)


def _module_available(import_name: str) -> bool:
    """True if ``import_name`` can be located without importing it."""
    import importlib.util
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError):
        return False


def _pip_install(spec: str, skill_name: str) -> bool:
    """Best-effort ``pip install`` into the running interpreter. Opt-in only."""
    import subprocess
    print(f"[{skill_name}] MSA_AUTO_INSTALL=1 -> pip install {spec}", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", spec],
            check=True, stdout=sys.stderr, stderr=sys.stderr,
        )
        import importlib
        importlib.invalidate_caches()
        return True
    except Exception as e:  # noqa: BLE001 — any failure just means "still missing"
        print(f"[{skill_name}] auto-install of {spec!r} failed: {e}", file=sys.stderr)
        return False


def ensure_deps(requirements, skill_name: str = "skill"):
    """Preflight third-party dependencies for a skill. Runtime-agnostic.

    ``requirements`` is an iterable of ``(import_name, pip_spec)`` or
    ``(import_name, pip_spec, kind)`` tuples, where ``kind`` is ``"hard"``
    (default) or ``"soft"``. ``import_name`` is what you'd ``import`` (e.g.
    ``"rdkit"``); ``pip_spec`` is what you'd ``pip install`` (e.g.
    ``"rdkit>=2023.9"``).

    Behaviour:
      * Missing HARD dep -> fatal. Each is reported to stderr as a
        machine-greppable ``MSA_ENV_MISSING: <import> -- pip install "<spec>"``
        line, followed by one ready-to-run install command, then the process
        exits ``4``. Exit 4 lets a chaining Agent distinguish "environment not
        provisioned" (fix once, globally) from a per-task chemistry failure
        (fail-soft, exit 0) or a contract/config error (exit 1/2/3).
      * Missing SOFT dep -> non-fatal. Reported once as
        ``MSA_ENV_SOFT_MISSING: <import> -- optional; pip install "<spec>"`` and
        returned in a set so the skill can degrade gracefully.

    Opt-in convenience: with ``MSA_AUTO_INSTALL=1`` set, a missing dep (hard or
    soft) is ``pip install``-ed into the running interpreter and re-checked
    before being declared missing. Off by default so a skill never mutates its
    host environment without explicit consent.

    Returns the set of soft-dependency import names still unavailable.
    """
    auto = os.environ.get("MSA_AUTO_INSTALL") == "1"
    missing_hard = []       # list of (import_name, spec)
    missing_soft = set()    # set of import_name

    for req in requirements:
        if len(req) >= 3:
            import_name, spec, kind = req[0], req[1], req[2]
        else:
            import_name, spec, kind = req[0], req[1], "hard"

        if _module_available(import_name):
            continue
        if auto and _pip_install(spec, skill_name) and _module_available(import_name):
            continue

        if kind == "soft":
            print(
                f'MSA_ENV_SOFT_MISSING: {import_name} -- optional; pip install "{spec}"',
                file=sys.stderr,
            )
            missing_soft.add(import_name)
        else:
            missing_hard.append((import_name, spec))

    if missing_hard:
        print(
            f"[{skill_name}] ERROR: required dependencies are missing from this "
            f"Python environment ({sys.executable}).",
            file=sys.stderr,
        )
        for import_name, spec in missing_hard:
            print(f'MSA_ENV_MISSING: {import_name} -- pip install "{spec}"', file=sys.stderr)
        joined = " ".join(f'"{spec}"' for _, spec in missing_hard)
        print(
            f"[{skill_name}] install them and re-run, or set MSA_AUTO_INSTALL=1 to "
            f"let the skill install them automatically:\n"
            f"    {sys.executable} -m pip install {joined}",
            file=sys.stderr,
        )
        sys.exit(ENV_MISSING_EXIT)

    return missing_soft


# ---------- task_list JSON IO ----------

def load_task_list(path: str) -> List[Dict[str, Any]]:
    """Load a task_list from JSON. Accepts a bare list or a wrapped object.

    Side effect: activates the run-isolation namespace. Every downstream skill
    reaches its tasks through this single function, so this is the one place we
    resolve the pipeline's ``run_id`` (top-level on a wrapped payload, else the
    first task that carries one) and hand it to ``naming.set_run_id``. From that
    point on every ``naming.*`` path this process builds is nested under the run's
    unique id, so concurrent runs of the same molecule/spectrum cannot clobber
    each other's inputs/outputs — locally or under the shared HPC ``remote_root``.
    A task_list without a ``run_id`` leaves the legacy flat layout untouched.
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict) and "task_list" in payload:
        tasks = list(payload["task_list"])
        run_id = payload.get("run_id")
    elif isinstance(payload, list):
        tasks = payload
        run_id = None
    else:
        raise ValueError(f"unexpected task_list payload shape at {path}")

    if not run_id:
        for t in tasks:
            if isinstance(t, dict) and t.get("run_id"):
                run_id = t["run_id"]
                break
    if run_id:
        from msa_shared import naming
        naming.set_run_id(run_id)
    return tasks


def dump_task_list(task_list: List[Dict[str, Any]], path: str, indent: int = 2) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task_list, f, ensure_ascii=False, indent=indent)


# ---------- status helpers ----------
#
# ``{stage}_status`` has exactly three values — success / failed / skipped — and
# ``upstream_ok`` is the ONLY gate every downstream stage uses. Two orthogonal
# facts are carried in their own fields rather than overloading the status:
#
#   ``{stage}_signal``   "none" when the stage ran correctly but produced no
#                        artifact (see ``mark_no_signal``); absent otherwise.
#   ``{stage}_warnings`` list of diagnostics attached to a produced artifact
#                        (see ``add_warning``); absent when there are none.

def mark_success(task: Dict[str, Any], stage: str, **fields) -> Dict[str, Any]:
    """Record a stage as fully successful, with a real artifact.

    Clears BOTH the error and the no-signal flags: re-running a stage over a
    task_list that previously recorded ``{stage}_signal="none"`` (e.g. after the
    calculation was redone and now has a bright transition) must not leave the
    stale "nothing to draw" annotation next to a freshly written figure.
    """
    task[f"{stage}_status"] = "success"
    task.pop(f"{stage}_error", None)
    task.pop(f"{stage}_signal", None)
    task.pop(f"{stage}_message", None)
    task.update(fields)
    return task


def mark_no_signal(task: Dict[str, Any], stage: str, message: str, **fields) -> Dict[str, Any]:
    """A stage that ran correctly but had nothing to draw / broaden.

    "No valid signal" (every vibrational mode imaginary, a dark S1, all core
    transitions below the oscillator-strength threshold) is a legitimate physical
    outcome, NOT an execution error — the job converged and the numbers are
    trustworthy. It is recorded as ``{stage}_status="success"`` plus a separate
    ``{stage}_signal="none"`` flag and the fixed user-facing sentence in
    ``{stage}_message``.

    Encoding it in ``{stage}_status`` instead would break the contract: every
    downstream gate is ``upstream_ok`` (status == "success"), so a fourth status
    value would silently skip the export stage — which is explicitly required to
    still write the raw peaks table in this case.

    Callers pass the artifact fields as empty strings (e.g. ``plot_png=""``) so a
    consumer can tell "no artifact" apart from "field absent".
    """
    task[f"{stage}_status"] = "success"
    task[f"{stage}_signal"] = "none"
    task[f"{stage}_message"] = str(message)
    task.pop(f"{stage}_error", None)
    task.update(fields)
    return task


def add_warning(task: Dict[str, Any], stage: str, message: str) -> Dict[str, Any]:
    """Append a diagnostic to ``{stage}_warnings`` (deduplicated, order preserved).

    A warning does not change the stage status: the artifact IS produced, but it
    carries a caveat the chemist must see (imaginary frequencies being the main
    case). The same list is written into the CSV metadata header and surfaced in
    the final report, so the two can never disagree.
    """
    key = f"{stage}_warnings"
    existing = task.get(key)
    if not isinstance(existing, list):
        existing = [] if existing in (None, "") else [str(existing)]
    text = str(message)
    if text not in existing:
        existing.append(text)
    task[key] = existing
    return task


def mark_failed(task: Dict[str, Any], stage: str, message: str, **fields) -> Dict[str, Any]:
    task[f"{stage}_status"] = "failed"
    task[f"{stage}_error"] = str(message)
    task.update(fields)
    return task


def mark_skipped(task: Dict[str, Any], stage: str, upstream_stage: str) -> Dict[str, Any]:
    task[f"{stage}_status"] = "skipped"
    task[f"{stage}_error"] = f"upstream {upstream_stage} failed"
    return task


def upstream_ok(task: Dict[str, Any], upstream_stage: str) -> bool:
    return task.get(f"{upstream_stage}_status") == "success"


def spectrum_kwargs(task: Dict[str, Any]) -> Dict[str, Any]:
    """Task fields -> ``orca_output.build_spectrum`` keyword arguments.

    Single source of truth for "which task field feeds which builder argument",
    so the six plot skills and the export skill can never disagree about, say,
    where the XANES edge element lives. Every builder swallows the arguments it
    does not need, so the same dict can be splatted for any spectrum.

    The XANES element is taken from ``naming.spec_variant`` — the very value that
    goes into the filename — so the plotted energy window and the file it is
    written to can never refer to different edges.
    """
    from msa_shared import naming

    extra = task.get("extra_params") or {}
    spec = (task.get("target_spectra") or "").strip().lower()
    basis_set = task.get("calc_basis_final")
    if not basis_set:
        params = task.get("params") or {}
        for step_name in sorted(params, reverse=True):
            candidate = params.get(step_name) or {}
            if candidate.get("basis_set"):
                basis_set = candidate["basis_set"]
                break
    return {
        "target_nucleus": (extra.get("target_nucleus") or "").strip() or None,
        "system_type": task.get("system_type"),
        "basis_set": basis_set,
        "target_element": naming.spec_variant(task) if spec == "xanes" else None,
    }


# ---------- SLURM remote transport (Stage 1: scp-transfer + slurm-remote via paramiko) ----------
#
# Transport is the scp-transfer + slurm-remote combination decided in
# PIPELINE.md §7: submit = SFTP upload + `sbatch`, poll = `squeue`, download =
# SFTP get. paramiko is imported lazily so Type-A (preprocessing) skills that
# import common_utils never need it installed.


class ClusterConfigError(RuntimeError):
    """Raised when the cluster profile is incomplete (no host / user / auth /
    remote_root / orca_bin / mpi_prefix). The message is written to be
    *actionable by an Agent*: it names the missing fields and the three ways to
    supply them, so the Agent can gather them from the user (or its own memory)
    and retry, rather than failing opaquely."""


# Fields that make up a cluster profile. Secrets and cluster identity live ONLY
# here at runtime — never in any shipped module. ``partition`` / ``qos`` fall
# back to the cluster-agnostic defaults in constants/slurm.py; every other field
# must be supplied by the operator/Agent at run time.
_CLUSTER_ENV = {
    "host": "HPC_HOST",
    "user": "HPC_USER",
    "password": "HPC_PASSWORD",
    "key_file": "HPC_KEY_FILE",
    "remote_root": "HPC_REMOTE_ROOT",
    "orca_bin": "HPC_ORCA_BIN",
    "mpi_prefix": "HPC_MPI_PREFIX",
    "partition": "HPC_PARTITION",
    "qos": "HPC_QOS",
}
# Required for any ORCA submission; auth is checked separately (password OR key).
_CLUSTER_REQUIRED = ("host", "user", "remote_root", "orca_bin", "mpi_prefix")


def resolve_cluster(**overrides) -> dict:
    """Build a full cluster profile at run time, secret-free AND file-free.

    SECURITY CONTRACT (decided by the operator): cluster credentials must be
    supplied *explicitly, per invocation*, through the inline ``HPC_*``
    environment channel — the sanctioned way to hand secrets to a single
    ``run`` command without ever touching disk. There is deliberately NO
    config-file channel: no ``--cluster-config``, no ``$MSA_CLUSTER_CONFIG``,
    and no fallback to any conventional path such as ``~/.config/msa/cluster.json``.
    Credentials are therefore never persisted and never auto-discovered — a file
    a previous task may have left on disk can never be picked up by a later one.

    Precedence (highest first): explicit ``overrides`` kwargs (programmatic /
    tests) > ``HPC_*`` environment variables > cluster-agnostic ``partition`` /
    ``qos`` defaults.

    No value is ever read from a shipped module or a disk file — the inline
    ``HPC_*`` env is the single place a cluster's host / user / credential /
    paths enter the process. Raises ``ClusterConfigError`` (actionable) if a
    required field or auth is missing, which is the signal for the host Agent to
    ASK THE USER for the cluster coordinates for this task rather than reusing
    anything cached on disk.
    """
    from msa_shared.constants import slurm as _slurm

    prof: dict = {}

    # Layer 1: HPC_* environment variables. This is the ONLY channel — the Agent
    # sets these inline for a single command after asking the user; they are not
    # persisted and do not leak to later commands. Nothing is read from disk.
    for field, env in _CLUSTER_ENV.items():
        val = os.environ.get(env)
        if val:
            prof[field] = val

    # Layer 2 (highest): explicit overrides (programmatic callers / tests).
    prof.update({k: v for k, v in overrides.items() if v is not None})

    # Cluster-agnostic convenience fallbacks (not secrets, not identity).
    prof.setdefault("partition", _slurm.DEFAULT_PARTITION)
    prof.setdefault("qos", _slurm.DEFAULT_QOS)
    prof.setdefault("key_file", None)
    prof.setdefault("password", None)

    missing = [f for f in _CLUSTER_REQUIRED if not prof.get(f)]
    no_auth = not prof.get("password") and not prof.get("key_file")
    if missing or no_auth:
        need = list(missing) + (["password or key_file"] if no_auth else [])
        raise ClusterConfigError(
            "cluster profile incomplete — missing: " + ", ".join(need) + ". "
            "Credentials are NEVER read from disk and NEVER auto-discovered; "
            "ask the user for this task's cluster coordinates and set the inline "
            "environment variables "
            f"{sorted(_CLUSTER_ENV.values())} for this single command (secrets "
            "stay off disk). Do NOT reuse a cached credential file left by a "
            "previous task. See this skill's SKILL.md."
        )
    return prof


def _ssh_client(host=None, user=None, password=None, key_file=None):
    """Return a connected paramiko SSHClient.

    Connection parameters resolve as: explicit arg > environment variable
    (HPC_HOST / HPC_USER / HPC_PASSWORD / HPC_KEY_FILE). There is no built-in
    default — credentials are never shipped in code (see ``resolve_cluster``).
    paramiko is imported lazily here.
    """
    import paramiko  # lazy: only Type-B (ORCA) skills reach this path

    host = host or os.environ.get("HPC_HOST")
    user = user or os.environ.get("HPC_USER")
    password = password or os.environ.get("HPC_PASSWORD")
    key_file = key_file or os.environ.get("HPC_KEY_FILE")

    if not host:
        raise ClusterConfigError("HPC host not specified (ask the user, then set HPC_HOST inline for this command).")
    if not user:
        raise ClusterConfigError("HPC user not specified (ask the user, then set HPC_USER inline for this command).")
    if not password and not key_file:
        raise ClusterConfigError("No HPC auth: ask the user, then set HPC_PASSWORD or HPC_KEY_FILE inline for this command.")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {"hostname": host, "username": user, "timeout": 10}
    if key_file:
        connect_kwargs["key_filename"] = key_file
    else:
        connect_kwargs["password"] = password
    ssh.connect(**connect_kwargs)
    return ssh


def submit_slurm(work_dir, script="submit.sh", upload_files=None, **conn):
    """Upload optional input files then ``sbatch`` a script in a remote work dir.

    Creates ``work_dir`` remotely, SFTP-uploads each path in ``upload_files``
    (basenames only), then runs ``cd work_dir && sbatch script`` and parses the
    SLURM job id out of ``Submitted batch job <id>``.

    Returns the SLURM job id (str). Raises RuntimeError if sbatch did not report
    a job id. ``**conn`` are forwarded to ``_ssh_client`` (host/user/password/key_file).
    """
    ssh = _ssh_client(**conn)
    try:
        ssh.exec_command(f"mkdir -p {work_dir}")
        if upload_files:
            sftp = ssh.open_sftp()
            try:
                for local_path in upload_files:
                    remote_path = f"{work_dir}/{os.path.basename(local_path)}"
                    sftp.put(local_path, remote_path)
            finally:
                sftp.close()
        _stdin, stdout, stderr = ssh.exec_command(f"cd {work_dir} && sbatch {script}")
        out = stdout.read().decode("utf-8", errors="replace").strip()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        if "Submitted batch job" in out:
            return out.split()[-1]
        raise RuntimeError(f"sbatch did not return a job id: stdout={out!r} stderr={err!r}")
    finally:
        ssh.close()


def poll_slurm(job_id, interval=30, timeout=86400, **conn):
    """Poll ``squeue -j <job_id>`` until the job leaves the queue or times out.

    A job id absent from squeue output means it has left the queue (COMPLETED or
    FAILED — squeue cannot distinguish, the caller inspects the .out file).

    Each iteration opens a fresh SSH connection, runs ``squeue``, and closes
    immediately — this avoids long-lived connections being reset by firewalls or
    server-side idle timeouts (which was the root cause of ``Connection reset by
    peer`` errors during multi-hour TD-DFT jobs).

    Transient SSH failures (network blip, DNS hiccup) are retried up to 3 times
    with exponential backoff before propagating.

    Returns "COMPLETED" when the job leaves the queue, or "TIMEOUT" if it is
    still queued after ``timeout`` seconds. ``**conn`` -> ``_ssh_client``.
    """
    import time

    _MAX_SSH_RETRIES = 3

    start = time.time()
    while True:
        # --- one poll attempt with retry ---
        last_err = None
        for attempt in range(_MAX_SSH_RETRIES):
            try:
                ssh = _ssh_client(**conn)
                try:
                    _stdin, stdout, _stderr = ssh.exec_command(f"squeue -j {job_id}")
                    output = stdout.read().decode("utf-8", errors="replace")
                finally:
                    ssh.close()
                # Successful poll — check result
                if str(job_id) not in output:
                    return "COMPLETED"
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 — transient SSH/network error
                last_err = e
                backoff = (2 ** attempt) * 5  # 5s, 10s, 20s
                print(
                    f"[poll_slurm] SSH error (attempt {attempt + 1}/{_MAX_SSH_RETRIES}): "
                    f"{e!r} — retrying in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)

        if last_err is not None:
            raise RuntimeError(
                f"poll_slurm: SSH unreachable after {_MAX_SSH_RETRIES} retries "
                f"(last error: {last_err!r})"
            ) from last_err

        if time.time() - start > timeout:
            return "TIMEOUT"
        time.sleep(interval)


def download_results(remote_dir, files, local_dir, **conn):
    """SFTP-download named files from ``remote_dir`` into ``local_dir``.

    Missing remote files are collected rather than raising. Returns a
    ``(downloaded, missing)`` tuple of filename lists. ``**conn`` -> ``_ssh_client``.
    """
    ssh = _ssh_client(**conn)
    try:
        os.makedirs(local_dir, exist_ok=True)
        sftp = ssh.open_sftp()
        downloaded, missing = [], []
        try:
            for name in files:
                remote_path = f"{remote_dir}/{name}"
                local_path = os.path.join(local_dir, name)
                try:
                    sftp.get(remote_path, local_path)
                    downloaded.append(name)
                except FileNotFoundError:
                    missing.append(name)
        finally:
            sftp.close()
        return downloaded, missing
    finally:
        ssh.close()
