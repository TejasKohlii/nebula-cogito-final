# Cogito

Cogito is a local RTL optimization application with formal equivalence checking,
logic synthesis, static timing analysis, timing and RTL analysis, live progress,
and a browser-based interface. It can optimize the included **Liberica** and
**Excelsa** designs or prepare a user-supplied Verilog/SystemVerilog project.

Cogito supports two Claude connections:

- **Claude Code CLI** through the pinned Headless bridge.
- **Anthropic API** using a key entered for the current run in the WebUI.

The same evaluator, formal checks, timing flow, evidence, and optimization
guidance are used in both modes.

## Quick start

Cogito is supported on Linux. Run every command below from the repository root,
the directory containing the `cogito` launcher and this README.

```bash
python3 cogito setup
python3 cogito doctor
python3 cogito start
```

Open <http://127.0.0.1:5173> after the server starts.

To stop the server, including one running in the background:

```bash
python3 cogito stop
```

`stop` finds the Cogito server for the current checkout on any port, requests
cancellation of its active optimization process, and waits for it to exit. It
is harmless when Cogito is already stopped and does not require Node, Python
dependencies, or EDA tools to be installed.

## Required tools

Cogito does not bundle EDA executables. Install the following tools before
running `python3 cogito doctor`.

| Tool | Requirement | Why Cogito needs it |
| --- | --- | --- |
| Linux | Required | Process control, local execution, and the `cogito stop` implementation |
| Python | 3.12 or newer | Orchestrator, evaluators, analysis, reporting, and ShinkaEvolve |
| Python `venv` and `pip` | Required | Creates `.venv` and installs the frozen Python environment |
| Node.js | 22 or newer | WebUI server and Headless Claude CLI bridge |
| npm | Required during setup | Installs the pinned Node runtime fallback and Headless bridge |
| Yosys with ABC | Required | RTL elaboration, synthesis, mapping, and formal equivalence |
| Yosys Slang plugin | Required for advanced SystemVerilog | Parses constructs such as unpacked-array ports used by projects including Ibex |
| OpenSTA (`sta`) | Required | Static timing analysis |
| Verilator | Required | RTL lint/compilation support |
| Graphviz (`dot`) | Required | Graph rendering |
| GNU Make | Required | Included design workflows and validation |
| C++ compiler (`c++`) | Required | Verilator-generated builds |
| Claude Code CLI | Required only for CLI mode | Provides the model connection through Headless |
| Anthropic API key | Required only for API mode | Authenticates direct Anthropic Messages API requests |
| Git | Recommended | Cloning and updating the public repository; not needed after a ZIP download |

The Slang plugin must load inside the same Yosys executable used by Cogito:

```bash
yosys -Q -T -p 'plugin -i slang; help read_slang'
```

If this command fails, basic Verilog projects may still work, but advanced
SystemVerilog uploads will fail during preparation. Install a Yosys distribution
that includes `yosys-slang`, then configure Cogito to use that executable.

## Frozen validation stack

The following versions define the submission's fully tested environment. The
minimums above describe the supported interface; these exact versions record the
stack used for release validation.

| Component | Validated version |
| --- | --- |
| Operating system | Ubuntu 26.04.1 LTS, x86-64 |
| Linux kernel | 7.0.0-31-generic |
| glibc | 2.43 |
| Python | 3.14.4 |
| pip | 25.1.1 |
| setuptools | 78.1.1 |
| wheel | 0.46.3 |
| Node.js | 22.23.2 |
| npm | 10.8.2 |
| Yosys | 0.68+195, git `435977e97` |
| Yosys Slang frontend | Available as `read_slang` in the validated Yosys build |
| OpenSTA | 3.1.0 |
| Verilator | 5.053 development build, revision `v5.052-22-g7cf8c5cca` |
| Graphviz | 14.1.2 |
| GNU Make | 4.4.1 |
| C++ compiler | GCC 15.2.0 |
| Headless | 0.6.1 |
| Claude Code | 2.1.272; CLI mode only |
| ShinkaEvolve fork | 0.0.7, vendored in this repository |

Python direct, transitive, installer, and package-build dependencies are pinned
with exact versions in [`requirements.txt`](requirements.txt) and
[`ShinkaEvolve/pyproject.toml`](ShinkaEvolve/pyproject.toml). The editable line
uses the relative path `./ShinkaEvolve`, avoiding a checkout-specific absolute
path.

Node dependencies are frozen by npm lockfiles:

- [`.tools/package-lock.json`](.tools/package-lock.json) pins Headless 0.6.1 and
  its complete dependency tree.
- [`.tools/node-runtime/package-lock.json`](.tools/node-runtime/package-lock.json)
  pins the private Node.js fallback to 22.23.2.
- The Cogito WebUI itself has no npm runtime dependencies and no build step.

Do not replace the lockfiles with generated `node_modules` directories. Setup
reconstructs the dependency trees with `npm ci`.

## Installing the system prerequisites

Package names vary across Linux distributions. On a Debian/Ubuntu system, the
general development prerequisites can usually be installed with:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip nodejs npm graphviz make g++ git
```

Check that `python3 --version` is at least 3.12 and `node --version` is at least
22. If the system Node is older but npm is available, `python3 cogito setup`
installs the pinned private Node.js 22.23.2 runtime under
`.tools/node-runtime/node_modules/` without replacing the system Node.

Install Yosys with ABC, the Yosys Slang plugin, OpenSTA, and Verilator using the
packages or binary distribution appropriate for the judge's system. Their
commands must either be on `PATH` or configured as described below.

## Setting up the codebase

### 1. Download Cogito

Clone the public repository:

```bash
git clone <public-cogito-repository-url> cogito
cd cogito
```

For a downloaded ZIP, extract it and enter the extracted `cogito` directory.
The checkout may live anywhere, including a path containing spaces. Cogito
derives project paths from the launcher location and does not depend on the
author's directory structure.

### 2. Configure EDA executable paths if necessary

If all tools are on `PATH`, skip this step. Otherwise export their executable
paths before setup, doctor, and start:

```bash
export YOSYS=/path/to/yosys
export STA=/path/to/sta
export VERILATOR=/path/to/verilator
export COGITO_CLAUDE=/path/to/claude
```

An alternative for EDA tools is a machine-local
`cogito_webui/tools.local.json` file:

```json
{
  "yosys": "/path/to/yosys",
  "sta": "/path/to/sta",
  "verilator": "/path/to/verilator"
}
```

This file is ignored by Git and excluded from the submission archive. Both the
launcher and WebUI read it. Environment variables take precedence over this
file, and `cogito_webui/tools.json` provides portable `null` defaults that use
`PATH`.

### 3. Install the frozen application dependencies

```bash
python3 cogito setup
```

Setup performs the following work:

1. Requires Python 3.12 or newer.
2. Selects Node 22 or newer, installing the pinned private Node runtime when the
   available system Node is too old.
3. Creates `.venv` when it does not exist.
4. Installs the exact Python dependency versions from `requirements.txt`,
   including the vendored ShinkaEvolve fork.
5. Installs Headless and its locked Node dependencies under `.tools` with
   `npm ci`.

Setup needs network access to the configured Python and npm package registries.
It does not download or install Yosys, OpenSTA, Verilator, Graphviz, Make, a C++
compiler, or Claude Code.

### 4. Verify the installation

```bash
python3 cogito doctor
```

Doctor checks Node, Yosys, OpenSTA, Verilator, Graphviz, Make, the C++ compiler,
the Slang plugin, the optional Claude CLI/Headless connection, the Python
runtime, and both built-in design contracts. Resolve every `FAIL` before
starting. `OPTIONAL` for Claude CLI is acceptable when using API mode;
`OPTIONAL` for Slang is acceptable only when no advanced SystemVerilog design
will be uploaded.

### 5. Configure Claude

For **Claude Code CLI mode**, install Claude Code, run it once in a terminal,
and complete its authentication flow. Verify that Cogito can find it:

```bash
claude --version
python3 cogito doctor
```

For **Anthropic API mode**, no Claude CLI login is needed. The WebUI prompts for
the API key after API mode is selected. The Anthropic account must have enough
credit for the requested run.

### 6. Start Cogito

```bash
python3 cogito start
```

The server listens on `127.0.0.1:5173` by default. Open:

```text
http://127.0.0.1:5173
```

Useful launch variants:

```bash
python3 cogito start --open
python3 cogito start --port 5174
PORT=5174 python3 cogito start
```

The first opens the browser automatically. The other two select a different
port. Keep the default localhost binding unless network access is intentionally
required.

To launch the server in the background:

```bash
nohup python3 cogito start > /tmp/cogito-webui.log 2>&1 &
```

Stop foreground or background servers with:

```bash
python3 cogito stop
```

## Running an optimization in the WebUI

1. Select **Liberica**, **Excelsa**, or **Upload your own**.
2. Set the cost budget. Cogito requires at least `$0.50`.
3. Select **Claude Code CLI** or **Anthropic API**.
4. For API mode, enter the API key in the masked field.
5. Press **Optimize**.

The Optimize button remains disabled until the design, valid budget, and Claude
connection are selected. API mode additionally requires a nonempty key.

Initial mapping is rebuilt from the supplied RTL on every run. No downloaded
build artifacts are required. A budget is a ceiling rather than a guarantee of
timing closure. A model request already in flight can cause a small overrun.

Use **Running… stop** in the WebUI or `python3 cogito stop` to cancel active
work. Use **Save log** to preserve logs before the next run.

## Claude connection behavior

### CLI mode

CLI mode uses the authenticated Claude Code session through Headless. Headless
requires Node.js 22 or newer. Cogito ensures its subprocesses inherit the Node
runtime selected by the launcher.

The supplied tasks default to Claude Sonnet 5. If the installed Claude account
uses a different supported model name:

```bash
export COGITO_MODEL='headless/claude@sonnet?effort=medium'
```

`SHINKA_HEADLESS_COMMAND` may select another Headless executable or command.
Shell-quote values containing spaces. `COGITO_CLAUDE` selects the Claude CLI
executable.

### API mode

API mode calls the Anthropic Messages API directly and does not invoke the
Claude CLI. The current run's bounded evidence packet is placed directly in the
proposal prompt.

The API key exists only in the selected run process environment. Cogito does
not put it in command-line arguments, task files, logs, run state, reports, or
browser storage. The field is cleared after launch, and a new run requires the
key to be entered again. CLI runs do not inherit a key entered for API mode.

`COGITO_API_MODEL` overrides the direct Anthropic model. For an advanced
terminal launch, set `COGITO_PROVIDER=api` and `ANTHROPIC_API_KEY` before running
the orchestrator. API usage is measured from response token counts against the
same run budget.

The model request format and bundled fallback pricing follow Anthropic's Sonnet
5 documentation:

- <https://platform.claude.com/docs/en/models/sonnet-5/migration-guide>
- <https://platform.claude.com/docs/en/api/messages/create>

## Uploading a custom design

Choose **Upload your own**, navigate to the project directory, and press
**Use this folder**. Select the whole design folder, including RTL, headers,
constraints, and configuration; do not select only one RTL subdirectory when
the design depends on files elsewhere in the project.

Cogito discovers:

- Synthesizable `.v` and `.sv` sources.
- `.vh` and `.svh` headers.
- Include-directory structure.
- A single unambiguous top module.
- An SDC constraint file and Liberty timing library when supplied.
- Optional memory and other supported auxiliary files.

Testbench files are ignored. Uploaded Python and build scripts are not executed.
The source folder must contain no unrelated top-level designs.

Cogito tries the native Yosys Verilog reader first and falls back to the Slang
SystemVerilog frontend. The selected frontend is frozen into the prepared design
and reused for syntax checking, golden/candidate elaboration, formal equivalence,
synthesis, heuristics, and initial/final graph generation.

If the project supplies no SDC, Cogito generates a 10 ns baseline clock
constraint. If it supplies no Liberty file, Cogito uses the bundled Nangate45
typical library. The WebUI reports which defaults were used. For meaningful
timing results, provide constraints and a library that match the design and
technology.

Preparation runs before Optimize becomes available. It rejects ambiguous tops,
unsupported RTL, unresolved include or constraint dependencies, and designs
that fail formal self-check, synthesis, or STA. This preflight makes later
candidate failures attributable to changes rather than an unusable baseline.

All discovered RTL and header files are editable during optimization. Cogito
preserves their relative paths and include structure, writes candidates into
separate working trees, and proves them against an immutable golden snapshot.
It does not edit the original uploaded folder. Constraints, memory data, and the
Liberty library remain frozen.

A project may provide `cogito.json` to use a compatible custom task/evaluator.
In that case, the task defines the editable scope and verification contract.

The supplied folders behave like their preset cards:

- `custom_designs/custom_design_v2` is Liberica.
- `custom_designs/custom_design_v1` is Excelsa.

Select the whole design folder containing `cogito.json`. Their SDC source,
generated-clock, and CDC constraints are rebuilt from the mapped netlist for
each run.

## What Cogito verifies

Each admissible candidate is checked in stages:

1. Candidate format and frozen-input contract.
2. RTL syntax and elaboration.
3. Fresh Yosys formal equivalence against the golden RTL.
4. Technology mapping with the selected Liberty library.
5. Static timing analysis with the selected SDC.
6. Cell count, area, timing-closure, and optional vectorless power objectives.

Equivalence uses Yosys `equiv_*` passes directly. EQY is not invoked. The WebUI
shows equivalence separately from synthesis or timing rejection, so a mapping
failure is not presented as a failed proof.

Final RTL is freshly verified before publication. Timing results are pre-layout
setup estimates with ideal clocks. Power is vectorless and is not workload or
signoff power.

## Results, reports, and logs

Default locations are relative to the repository root:

| Artifact | Location |
| --- | --- |
| Runs | `sum_agent/runs/launch_*/` |
| Prepared uploads | `cogito_webui/generated_designs/` |
| Final report | `<run>/final_report.json` |
| Verified single-file RTL | `<run>/shinka/verification/<winner-id>/candidate.sv` or `.v` |
| Verified multi-file RTL | `<run>/shinka/verification/<winner-id>/rtl/` |
| Candidate evaluation | `<run>/shinka/gen_<n>/results/` |

For multi-file designs, `candidate.sv` is the optimizer's packed multi-file
document. Use the exported `rtl/` directory as normal HDL source.

Cogito produces four deterministic, programmatic reports without model calls:

```text
<run>/reports/yosys.txt
<run>/reports/opensta.txt
<run>/reports/equivalence.txt
<run>/reports/failures.txt
```

Equivalent reports are produced for each evaluated candidate and the freshly
verified winner:

```text
<run>/shinka/gen_<n>/results/reports/
<run>/shinka/verification/<winner-id>/results/reports/
```

The Run log prints their paths. The run-level report indexes candidate reports.
Failure analysis covers tool errors, unproven equivalence, remaining timing
violations, and runner/API failures.

The UI contains the timing graph, heuristic findings, per-generation timing,
area and power, equivalence state, Shinka visualizer, and final report. Full UI
errors are sent to the browser developer console. Press `F12`, select
**Console**, and look for entries beginning with `[Cogito]`. Folder-picker
messages stay short so errors do not expand the **Use this folder** button.

## Validation without model calls

Analyze either built-in design without invoking Claude:

```bash
.venv/bin/python sum_agent/orchestrator.py \
  --design custom_designs/custom_design_v2/rtl/benchmark.sv \
  --config sum_agent/design.json --budget 1 --analysis-only

.venv/bin/python sum_agent/orchestrator.py \
  --design custom_designs/custom_design_v1/rtl/benchmark.sv \
  --config sum_agent/design_excelsa.json --budget 1 --analysis-only
```

Run a complete seed evaluation with formal proof, synthesis, STA, and no model
calls:

```bash
.venv/bin/python shinka_optimizer/run.py \
  --task shinka_optimizer/tasks/window_engine_timing_ppa_v10.json \
  --results shinka_optimizer/runs/liberica-check --check

.venv/bin/python shinka_optimizer/run.py \
  --task shinka_optimizer/tasks/window_engine_excelsa.json \
  --results shinka_optimizer/runs/excelsa-check --check
```

Use a new results directory for each check.

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `YOSYS` | Yosys executable with ABC and, for advanced SystemVerilog, Slang |
| `STA` | OpenSTA executable |
| `VERILATOR` | Verilator executable |
| `NODE` | Explicit Node.js 22+ executable; overrides automatic selection |
| `COGITO_PYTHON` | Alternate Python environment containing the included Shinka runtime |
| `COGITO_CLAUDE` | Claude Code CLI executable |
| `SHINKA_HEADLESS_COMMAND` | Alternate Headless command |
| `COGITO_MODEL` | CLI-mode Shinka model string |
| `COGITO_API_MODEL` | Direct Anthropic API model name |
| `COGITO_PROVIDER` | Terminal mode connection: `cli` or `api` |
| `ANTHROPIC_API_KEY` | API credential for terminal API runs |
| `PORT` | WebUI port; default `5173` |

Explicit environment values override the portable defaults and
`cogito_webui/tools.local.json`.

## Troubleshooting

### Node.js 20 or older is reported

Run:

```bash
python3 cogito setup
python3 cogito doctor
```

Setup installs the pinned private Node 22 runtime when necessary. Remove an
invalid `NODE` override because explicit overrides must themselves point to
Node 22 or newer.

### `Can't load module './slang'`

Cogito is using a Yosys build without the Slang plugin. Configure a build that
passes:

```bash
yosys -Q -T -p 'plugin -i slang; help read_slang'
```

Set `YOSYS` or `cogito_webui/tools.local.json`, restart Cogito, and rerun
`python3 cogito doctor`.

### Port 5173 is already in use

Stop the existing server:

```bash
python3 cogito stop
```

Or use another port:

```bash
python3 cogito start --port 5174
```

### Anthropic reports insufficient credit

This is an Anthropic billing-account response. Add API credit or switch to an
authenticated Claude Code CLI connection, then start a new run. Cogito does not
retry a rejected billing request or spend beyond the selected connection.

### An optimization candidate fails

Check the Equivalence and Evaluation columns separately, open the report paths
printed in the Run log, and inspect the browser console. Formal success does not
imply that synthesis or timing analysis also succeeded.

## Repository and submission contents

`ShinkaEvolve/` contains the runtime fork required by Cogito, including the
timeout and seed-resume fixes. It is vendored source rather than a Git submodule.
See [`ShinkaEvolve/README.txt`](ShinkaEvolve/README.txt) and
[`ShinkaEvolve/LICENSE`](ShinkaEvolve/LICENSE).

The Nangate45 Liberty library and attribution are under
[`third_party/nangate45/`](third_party/nangate45/).

The optimizer's `.txt.enc` guidance and task-goal assets are runtime inputs.
Cogito decrypts them automatically with a bundled key; no password, service, or
extra setup is required. This mechanism is obfuscation against casual browsing,
not access control. Anyone who runs the application can recover the plaintext.
Decrypted prompts and evidence can appear in local run artifacts, which must not
be committed or submitted.

Generated runs, prepared uploads, local dependencies, caches, local EDA paths,
and secrets are ignored by Git. Publish the source files, including:

- `requirements.txt`
- `.tools/package.json` and `.tools/package-lock.json`
- `.tools/node-runtime/package.json` and its lockfile
- the vendored `ShinkaEvolve` source
- the Nangate45 library and notices
- encrypted guidance assets

Do not publish `.venv`, `node_modules`, generated designs, run directories,
logs, API keys, or `cogito_webui/tools.local.json`. A GitHub download of the
source repository then needs only the documented system tools and
`python3 cogito setup`.
