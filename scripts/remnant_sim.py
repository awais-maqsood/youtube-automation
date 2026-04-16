#!/usr/bin/env python3
"""
REMNANT — 20-run simulation.

Verifies cycle logic without any LLM or GitHub API calls.
Run:  python3 scripts/remnant_sim.py
"""

import sys, copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Import remnant logic but patch out the network calls
import remnant

def _noop_load(pat, repo):
    pass  # not used — state is managed inline below

def _noop_save(state, pat, repo, existed):
    pass  # not used — we inspect state directly

# ── Simulation ────────────────────────────────────────────────────────────────

RUNS = 20
SEP  = "-" * 62

def mock_topic():
    return {
        "boot_lines": [
            "> BOOT_LINE_0",
            "> BOOT_LINE_1",
            "> BOOT_LINE_2",
            "> BOOT_LINE_3",
            "> BOOT_LINE_4",
            "> BOOT_LINE_5",
        ],
        "epilogue": "mock epilogue line one.\nmock epilogue line two.",
    }


def fmt_state(s):
    return (
        f"  total={s['total_runs']:>3}  cycle={s['current_cycle']}  "
        f"stage={s['current_stage']}  remnant_runs={s['remnant_runs']:>2}  "
        f"next_remnant_at={s['next_remnant_at']:>3}\n"
        f"  in_dormant={str(s['in_dormant']):<5}  dormant_runs={s['dormant_runs']}  "
        f"cycle_remnant_count={s['cycle_remnant_count']}  "
        f"next_inc_4={s['next_increment_is_4']}"
    )


state = dict(remnant.DEFAULT_STATE)

print(SEP)
print("  REMNANT - 20-run simulation")
print(SEP)
print(f"  Initial state:")
print(fmt_state(state))
print(SEP)

for run_num in range(1, RUNS + 1):
    topic = mock_topic()

    # Replicate exactly what pipeline.py does
    state["total_runs"] += 1
    run_type     = remnant.determine_run_type(state)
    epilogue_ext = remnant.get_epilogue_extra(run_type)

    # Apply
    if run_type == "REMNANT":
        remnant.apply_remnant(state, topic)
    elif run_type == "DORMANT":
        remnant.apply_dormant(state, topic)

    # Display
    tag = {"REMNANT": "[REMNANT]", "DORMANT": "[DORMANT]", "NORMAL": "[NORMAL ]"}[run_type]
    print(f"\nRun {run_num:>2}  {tag}")
    print(f"  boot_lines[2] = {topic['boot_lines'][2]!r}")
    if epilogue_ext:
        print(f"  epilogue_extra = YES (injected into LLM prompt)")
    print(fmt_state(state))

print()
print(SEP)
print("  Final state after 20 runs:")
print(fmt_state(state))
print(SEP)
