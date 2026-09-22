"""SLURM compute defaults (cluster-agnostic) — NO credentials, NO cluster identity.

This module deliberately contains **no** host, username, password, key, remote
path, or ORCA install location. Those are per-operator/per-cluster values that a
skill must never ship. They are resolved *at runtime* by
``common_utils.resolve_cluster(...)`` from, in order of precedence:

    explicit programmatic kwargs  >  HPC_* environment variables
    >  (hard error — nothing is read from disk, nothing is silently defaulted)

so a third-party Agent can point any skill at its own cluster without editing a
single line of code, and no secret is ever baked into the published artifact.
There is deliberately no config-file channel (no ``--cluster-config``, no
``$MSA_CLUSTER_CONFIG``): credentials are supplied only as inline ``HPC_*`` env
vars, per run command, so nothing lands on disk. See ``resolve_cluster`` and each
orca-* skill's SKILL.md for the field list.

What lives here are only the cluster-*agnostic* compute defaults — safe to ship
because they identify nothing and carry no secret. ``partition`` / ``qos`` are
convenience fallbacks for the Stage-1 cluster shape (env-overridable); they are
not credentials.
"""

import os as _os

# ── SLURM job resource defaults (generic; no secret, no cluster identity) ──
# Verified against the teacher's Part_2 spec and the cluster's proven submit.sh:
#   * jobs must carry --qos=normal;
#   * nodes advertise no RealMemory (sinfo mem=1), so --mem-per-cpu MUST NOT be
#     emitted or the scheduler rejects the job. DEFAULT_MEMORY_MB_PER_CORE is kept
#     only for ORCA's in-input `%maxcore` line, not for an SBATCH request.
DEFAULT_NPROCS = 16
DEFAULT_WALLTIME = "08:00:00"
DEFAULT_MEMORY_MB_PER_CORE = 4000            # ORCA %maxcore (MB/core); NOT an SBATCH mem request

# Partition / qos: convenience fallbacks only (env-overridable). Not secrets.
# A different cluster overrides via the runtime cluster profile or HPC_PARTITION
# / HPC_QOS.
DEFAULT_PARTITION = _os.environ.get("HPC_PARTITION", "DELLP56")
DEFAULT_QOS = _os.environ.get("HPC_QOS", "normal")
