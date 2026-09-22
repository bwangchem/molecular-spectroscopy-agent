"""Central ORCA terminal-output classification for bounded fallback."""

from dataclasses import dataclass
import copy
import re


ORCA_NORMAL_TERMINATION = "****ORCA TERMINATED NORMALLY****"
OPTIMIZATION_CONVERGED = "THE OPTIMIZATION HAS CONVERGED"
RPA_NOT_CONVERGED = (
    "RPA/TD-DFT did not converge! You may try raising the number of iterations."
)
CIS_ERROR_TERMINATION = "ORCA finished by error termination in CIS"
SCF_NOT_CONVERGED = re.compile(r"SCF NOT CONVERGED AFTER\s+\d+\s+CYCLES")
NUMFREQ_INCOMPLETE = (
    re.compile(r"Calculation\s+\d+\s+of\s+\d+\s+is missing"),
    re.compile(r"Numerical calculation ISN'T COMPLETE"),
    re.compile(r"failed to read Hessian file!"),
)


@dataclass(frozen=True)
class Diagnostic:
    """Classification of one completed ORCA submission."""

    verdict: str
    reason: str
    normal_termination: bool
    optimization_banners: int


def classify_orca_output(
    out_text,
    *,
    expected_optimization_banners=0,
    allow_numfreq=False,
):
    """Classify output in the contract order A → C → B → unknown → success."""
    text = out_text or ""
    normal = ORCA_NORMAL_TERMINATION in text
    banners = text.count(OPTIMIZATION_CONVERGED)

    if SCF_NOT_CONVERGED.search(text):
        return Diagnostic("retry", "scf_not_converged", normal, banners)
    if RPA_NOT_CONVERGED in text and CIS_ERROR_TERMINATION in text:
        return Diagnostic("retry", "rpa_not_converged", normal, banners)
    if allow_numfreq and any(pattern.search(text) for pattern in NUMFREQ_INCOMPLETE):
        return Diagnostic("retry", "numfreq_incomplete", normal, banners)

    if CIS_ERROR_TERMINATION in text:
        return Diagnostic("failed", "cis_error", normal, banners)
    if banners < expected_optimization_banners:
        return Diagnostic("failed", "optimization_not_converged", normal, banners)
    if not normal:
        return Diagnostic("failed", "orca_terminated_abnormally", normal, banners)
    return Diagnostic("success", "none", normal, banners)


def expected_optimization_banners(params):
    """Count optimization steps whose convergence banners are mandatory."""
    optimization_types = {"opt", "opt_freq", "fluo_es_opt"}
    return sum(
        1 for step in (params or {}).values()
        if isinstance(step, dict) and step.get("calc_type") in optimization_types
    )


def recipe_candidates(task, applicable_steps=None):
    """Return baseline plus applicable upstream-owned fallback recipes in order."""
    candidates = [{
        "params_source": "params",
        "changed_steps": [],
        "params": copy.deepcopy(task.get("params") or {}),
    }]
    allowed = set(applicable_steps) if applicable_steps is not None else None
    for index, fallback in enumerate(task.get("params_fallbacks") or []):
        changed_steps = list(fallback.get("changed_steps") or [])
        if allowed is not None:
            changed_steps = [step for step in changed_steps if step in allowed]
            if not changed_steps:
                continue
        params = fallback.get("params")
        if not isinstance(params, dict):
            continue
        candidates.append({
            "params_source": f"params_fallbacks[{index}]",
            "changed_steps": changed_steps,
            "params": copy.deepcopy(params),
        })
    return candidates


def _params_for_source(task, source):
    if source == "params":
        return task.get("params") or {}
    match = re.fullmatch(r"params_fallbacks\[(\d+)\]", source or "")
    if not match:
        return {}
    fallbacks = task.get("params_fallbacks") or []
    index = int(match.group(1))
    if index >= len(fallbacks):
        return {}
    return fallbacks[index].get("params") or {}


def _production_step(params):
    steps = [
        (name, step) for name, step in (params or {}).items()
        if isinstance(step, dict)
    ]
    if not steps:
        return {}
    return sorted(steps)[-1][1]


def summarize_attempts(task, attempts, production_submission="single"):
    """Derive all calc fallback summary fields from the append-only attempts."""
    relevant = [
        attempt for attempt in attempts
        if attempt.get("submission") == production_submission
    ]
    successful = [attempt for attempt in relevant if attempt.get("status") == "success"]
    final_attempt = (successful or relevant or attempts or [{}])[-1]
    original_step = _production_step(task.get("params") or {})
    final_params = _params_for_source(task, final_attempt.get("params_source", "params"))
    final_step = _production_step(final_params)

    fallback_attempts = [
        attempt for attempt in attempts
        if attempt.get("params_source") != "params"
    ]
    changed_steps = []
    for attempt in fallback_attempts:
        for step_name in attempt.get("changed_steps") or []:
            if step_name not in changed_steps:
                changed_steps.append(step_name)

    fallback_reason = "none"
    for attempt in attempts:
        if attempt.get("status") == "failed" and attempt.get("failure_reason") in {
            "scf_not_converged", "rpa_not_converged", "numfreq_incomplete",
        }:
            fallback_reason = attempt["failure_reason"]
            break

    original_basis = original_step.get("basis_set")
    final_basis = final_step.get("basis_set")
    scf_thresh = final_step.get("scf_thresh")
    if scf_thresh is None and final_step.get("scf_tight"):
        scf_thresh = "1e-8"
    return {
        "calc_attempts": list(attempts),
        "calc_fallback_applied": bool(fallback_attempts),
        "calc_basis_original": original_basis,
        "calc_basis_final": final_basis,
        "calc_basis_fallback": "one_step" if original_basis != final_basis else "none",
        "calc_fallback_steps": changed_steps,
        "calc_fallback_reason": fallback_reason,
        "calc_scf_thresh": scf_thresh,
    }
