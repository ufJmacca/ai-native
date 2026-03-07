from ai_native.stages.architecture import run as run_architecture
from ai_native.stages.git_pr import commit_run, create_prs
from ai_native.stages.intake import run as run_intake
from ai_native.stages.loop import run as run_loop
from ai_native.stages.planning import run as run_plan
from ai_native.stages.prd import run as run_prd
from ai_native.stages.recon import run as run_recon
from ai_native.stages.slicing import run as run_slice
from ai_native.stages.verify import run as run_verify

ORDERED_STAGES = [
    "intake",
    "recon",
    "plan",
    "architecture",
    "prd",
    "slice",
    "loop",
    "verify",
    "commit",
    "pr",
]

