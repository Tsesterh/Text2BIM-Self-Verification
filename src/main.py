import argparse
from doctest import OutputChecker
import json
import time

from orchestrator import orchestrate

model = "gpt-5.2" #"openai/gpt-oss-120b"
url = None #"http://dws-13:8743/v1"

def main():
    ap = argparse.ArgumentParser(description="Run BIM agent loop (Specifier -> Modifier -> Reviewer + IDS).")
    ap.add_argument("--prompt", default="Create a simple, but realistic house, 4 rooms per level, 2 levels, made out of wood. The roof should be a gable roof.")
    #ap.add_argument("--prompt", default="I want to build a two-story hotel with eight rooms on each floor. The rooms are arranged in groups of four on each side, separated by a 4-meter-wide corridor in the middle. Each room has a door and a window. The doors of the rooms are on the corridor side of the wall, and the windows are on the outside wall of the building. The building should have a wooden pitched roof and brick walls.", help="User prompt describing desired BIM/IFC model.")
    #ap.add_argument("--prompt", default="Build a three-story building with a different footprint on each floor: rectangular ground floor (15m x 10m), L-shaped first floor, and T-shaped top floor. Ensure structural continuity between floors.")
    ap.add_argument("--out", default="run_out", help="Output directory.")
    ap.add_argument("--max-iters", type=int, default=5)
    ap.add_argument("--model-specifier", default=model)
    ap.add_argument("--model-modifier", default=model)
    ap.add_argument("--model-reviewer", default=model)
    ap.add_argument("--modifier-backend", default="mcp", choices=["llm", "mcp"])
    ap.add_argument("--reviewer-backend", default="mcp", choices=["llm", "mcp"])
    ap.add_argument("--mcp-config", default="mcp.config.json", help="Path to MCP benchmark config JSON (for modifier-backend=mcp).")
    args = ap.parse_args()

    # extend out_dir with current timestamp
    out_dir = args.out
    args.out = f"{out_dir}/run_{int(time.time())}"


    result = orchestrate(
        user_prompt=args.prompt,
        out_dir=args.out,
        max_iterations=args.max_iters,
        model_specifier=args.model_specifier,
        model_modifier=args.model_modifier,
        model_reviewer=args.model_reviewer,
        modifier_backend=args.modifier_backend,
        reviewer_backend=args.reviewer_backend,
        mcp_config_path=args.mcp_config,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
