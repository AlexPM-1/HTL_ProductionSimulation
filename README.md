# HTL Production Simulation

Discrete-event simulation and browser dashboards for mixed production with
Kanban (pull) and push orders on the HTL3, HTL5 and HTL6 production lines.

## Entry points

Backend:

- `backend/api_server_mixed.py`: FastAPI application object `app`. This is the
	normal HTTP entry point and exposes the mixed simulation API.
- `backend/mixed_runner.py`: Python entry point for running the simulation
	directly with `run_mixed(...)` or `python mixed_runner.py ...`.

Frontend:

- `frontend/mixed_production_planner_dashboard.html`: main planner UI. It
	calls `POST /api/simulate_mixed` and renders schedules, KPIs, card flow,
	supermarket state and push-delivery results.
- `frontend/movement_simulation.html`: secondary movement playback UI. It
	reads the last run cached by the API and animates a selected piece, Kanban
	card or plant layout.
- `frontend/movement_layout.js`: shared browser-side SVG layout and playback
	logic used by the movement page.

There is no frontend build system. The HTML, CSS and JavaScript are served as
static files and run in a modern browser.

## Technology

### Runtime and packages

- Python 3.10+ (type annotations use modern Python syntax).
- FastAPI: HTTP API and request/response models.
- Uvicorn: ASGI development server.
- Pydantic: API request validation.
- SimPy: discrete-event simulation engine and resource scheduling.
- pandas: workbook/table loading and data transformation.
- openpyxl: `.xlsx` workbook access used by the configuration loader.
- Python standard library: dataclasses, datetime, pathlib, JSON-compatible
	serialization and environment-variable configuration.

### Browser

- Plain HTML, CSS and modern JavaScript.
- Native `fetch`, SVG and browser controls; no npm packages or bundler.
- Python's built-in `http.server` is used only to serve the static frontend
	during local development.

The repository currently has no `requirements.txt`, `pyproject.toml`, lockfile
or automated test suite. Install the packages explicitly in a virtual
environment, and pin them in project metadata before deploying or sharing the
tool.

## Required input files

The backend loads three Excel workbooks. By default it looks for these paths
relative to the backend process's current working directory:

- `ProductionPlanning_v6.xlsx`: process, buffers, demand, Kanban, shifts and
	related planning sheets.
- `HTL_setup_times.xlsx`: setup/changeover times by line and TTNr.
- `product_master.xlsx`: product-to-TTNr mapping.

These files are not included in this repository. Set the environment variables
to their actual locations before starting the API:

- `SIM_EXCEL_PATH`
- `SIM_SETUP_XLSX_PATH`
- `SIM_PRODUCT_MASTER_PATH`

The API reads `SIM_CORS_ORIGINS`, but the current middleware still uses a
hard-coded localhost allow-list. See the technical debt section below.

## Local startup on Windows

Create a virtual environment once:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install fastapi "uvicorn[standard]" pydantic simpy pandas openpyxl
```

Open two PowerShell terminals from the repository root. In terminal 1, point
the API at the workbook files and start Uvicorn from `backend` so the local
module imports resolve:

```powershell
Set-Location .\backend
$env:SIM_EXCEL_PATH = "C:\path\to\ProductionPlanning_v6.xlsx"
$env:SIM_SETUP_XLSX_PATH = "C:\path\to\HTL_setup_times.xlsx"
$env:SIM_PRODUCT_MASTER_PATH = "C:\path\to\product_master.xlsx"
python -m uvicorn api_server_mixed:app --reload --port 8000
```

In terminal 2, serve the frontend from its directory:

```powershell
Set-Location .\frontend
python -m http.server 5500
```

Open the main dashboard:

<http://localhost:5500/mixed_production_planner_dashboard.html>

Useful checks:

- API health: <http://localhost:8000/api/health>
- Interactive API documentation: <http://localhost:8000/docs>
- Movement page: <http://localhost:5500/movement_simulation.html>

The dashboard's default API URL is `http://localhost:8000`. If the API uses a
different host or port, change **API Base URL** in the dashboard.

## What the application does

1. `api_server_mixed.py` loads the Excel configuration and creates a shared
	 SimPy environment for Kanban and push production.
2. `mixed_runner.run_mixed()` schedules both classes on the same line,
	 stations, buffers and Kanban chutes. Kanban work has priority over push
	 work, but production is non-preemptive once a unit is running.
3. Push demand is assigned dynamically to compatible lines using the product
	 line matrix, frozen-zone rules and rush handling. Shift calendars can keep
	 a line off-shift.
4. The API returns Gantt segments, combined and class-specific KPIs, card-flow
	 series, supermarket snapshots, delivery records and issue logs.
5. The main dashboard visualizes that response. After a successful run it
	 enables the Movement Simulation link, which queries the API's in-memory
	 last-run cache for detailed piece/card traces and plant movement frames.

Typical workflow:

1. Start the API and static frontend server.
2. Confirm `/api/health` reports the expected lines.
3. Open the main dashboard, review the run parameters and push policy, then
	 run the mixed simulation.
4. Inspect All Production, Kanban (Pull) and Push tabs, including day-range
	 views and issue logs.
5. Open Movement Simulation to select a piece/card or load a line's plant
	 movement. It cannot be used for detailed playback until a simulation has
	 run because the API only keeps the last run in memory.

## Engineering assessment

### Highest-impact debt

- **No reproducible dependency contract.** There is no package metadata,
	version pinning, test command or CI configuration. Add `pyproject.toml`
	(or at minimum `requirements.txt`), pin compatible versions, and add unit
	tests for configuration parsing, policy validation, scheduling and API
	payloads.
- **Input files are implicit runtime state.** Relative defaults depend on the
	process working directory, while the workbooks are outside the repository.
	Validate all configured paths at startup, expose a clear configuration
	error, and use one typed settings object instead of module-level constants.
- **The API is stateful and single-run.** `_LAST_MIXED_KENV` is a global
	in-memory slot: concurrent users overwrite each other's run, a restart
	loses all results, and worker processes would not share it. Introduce a run
	ID and persisted/artifact-backed result store, or explicitly enforce a
	single-user session model.
- **Simulation runs block the web worker.** `POST /api/simulate_mixed`
	performs a potentially long SimPy run inside the request. Move execution to
	a job worker or background task with status polling and cancellation.
- **Runtime monkeypatching couples subsystems.** `mixed_runner.py` patches
	Kanban functions and per-instance chute methods to merge push and pull
	behavior. This is difficult to test and easy to break during refactors.
	Prefer explicit interfaces or dependency injection for gate admission,
	chute selection and event classification.

### Medium-impact debt

- **Version drift and duplicated logic.** Module names and comments still
	refer to older `v4`/`v5` files, and frontend code duplicates styling and
	rendering conventions. Consolidate shared contracts and remove stale
	references so documentation reflects executable code.
- **Frontend is a set of large inline scripts.** The two HTML files contain
	substantial state, rendering and API code with global functions. Split API
	clients, state management and views into modules, then add browser tests for
	loading, filtering, playback and error states.
- **Configuration is only partly environment-driven.** `SIM_CORS_ORIGINS`
	is parsed but ignored by the middleware. Use the parsed value, validate
	origins, and keep development origins separate from production settings.
- **Class membership is inferred from product sets.** API class KPIs infer
	pull/push from product membership rather than recording production class on
	each part at creation time. Store the class on the domain event/entity to
	avoid ambiguity when workbook sheets overlap or change.
- **Observability is mostly print-based.** Replace simulation `print()` calls
	with structured logging and include run IDs, configuration fingerprints,
	duration and failure causes in API responses/logs.

### Suggested order of improvement

1. Add dependency metadata, a smoke test for `/api/health` and one deterministic
	 simulation fixture using small test workbooks.
2. Centralize typed settings and validate workbook paths/sheet schemas before a
	 run starts.
3. Replace the global last-run cache with explicit run storage and IDs.
4. Move long simulations out of the request thread.
5. Refactor monkeypatched integration points into explicit simulation services,
	 then modularize the frontend around the resulting API contract.
