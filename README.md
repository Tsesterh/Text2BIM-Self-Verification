# bim_agent_project

General-purpose multi-agent loop for generating and verifying IFC models.

## Components

- **Specifier**: Converts user prompt into:
  - `spec.md` (canonical contract for verification)
  - `requirements.ids` (IDS XML, best-effort subset)
  - optional `requirements.json` and `assumptions.json`

- **Modifier**: Produces or updates an IFC model using IfcOpenShell tools:
  - generic `ifc_api(action, kwargs)` gateway to `ifcopenshell.api.run`
  - `ifc_python_exec` to execute IfcOpenShell Python snippets for bulk edits or custom logic

- **Reviewer**: Receives only `spec.md + model.ifc`, verifies using read-only tools (including python query).

- **IDS Checker**: Runs IfcTester against `requirements.ids` and the produced IFC.

- **Merger**: Merges Reviewer + IDS reports into a patch plan for the next iteration.

## Install

```bash
pip install -r requirements.txt
```

## Run

Set your API key:

```bash
export OPENAI_API_KEY="..."
```

Run an example:

```bash
python -m bim_agent.main --prompt "create a house, 4 rooms per level, 2 levels, made out of wood" --out run1
```

Artifacts are written into `run1/` including per-iteration outputs.

## Notes on `ifc_python_exec`

`ifc_python_exec` is a developer-mode tool. It blocks imports and some dunder access,
but it is **not a hardened sandbox**. For production, execute in a locked-down container/process.

## Extending

- Add more tool functions in `bim_agent/tools_ifc.py` if you want higher-level wrappers.
- Keep the Reviewer toolset read-only by exposing wrappers like `ifc_python_query`.
