# Digital Lab Coach (DLC)

[![Download](docs/download_button.svg)](https://github.com/KraLurmumcoelcarix-173/Digital-Lab-Coach-v0.1.2.3/releases/latest/download/DigitalLabCoach.zip)

A hybrid deterministic-checker + LLM feedback tool for debugging
[Digital](https://github.com/hneemann/Digital) circuit labs.
Three layers: structural analysis (Layer 1), conceptual explanation
(Layer 2), and a machine-verified debugging + test-coverage coach
(Layer 3) — every LLM fix proposal is re-run against the official tests
before a student sees it. 

We aim to improve quality and effectiveness of introductory hardware science
education and explore new means of interactive hardware design debugging.

![Dashboard view of cpu](docs/screenshots/dashboard.png)
![Dashboard view of mode A gif](docs/screenshots/modeA_sample.gif)

## Status
v0.1.2.3 (2026/9/24): The course proxy runs on Carolina CloudApps, admin page rewritten.
v0.1.2.2 (2026/9/23): Option A (local proxy + LAN) setup flow rewritten.
v0.1.2.1 (2026/9/17): Interface available in multi-language && a few small bug fixing.
v0.1.2 (2026/9/10): Mode A supports higher fixes with optimized latency and cost, signal flow walkthrough feature added in Layer 2.
v0.1.1 (2026/8/24): Supports 311 Digital transistor lab.
v0.1.0 (2026/8/23): first packaged release.

## Table of contents

- [Which start flow are you?](#which-start-flow-are-you)
- [Quick start (students)](#quick-start-students)
  - [Working offline](#working-offline)
  - [Telemetry statement](#telemetry-statement)
  - [Uninstalling](#uninstalling)
- [Instructor setup](#instructor-setup)
- [File layout](#file-layout)
- [Design](#design)
- [Developer setup](#developer-setup)
- [Troubleshooting (Windows): Smart App Control](#troubleshooting-windows-uv-run-blocked-by-smart-app-control)
- [Digital.jar for per-row test verification](#developer-optional-setup-digitaljar-for-per-row-test-verification)
- [License](#license)
- [Upstream](#upstream)
- [Acknowledgement](#acknowledgement)

## Which start flow are you?

- **Student in a course using DLC** → Quick start (students) below.
  Your instructor gives you a course-server URL + token — you do NOT
  need any API key.
- **Instructor running DLC for a course** → [docs/RELEASE_GUIDE.md](docs/RELEASE_GUIDE.md).
- **Developer** → Developer setup below.

## Quick start (students)

1. Hit the **Download** button at the top of this page and unzip it anywhere.
2. Windows and macOS/Linux are both supported: inside the unzipped folder, double-click
   **`START_HERE.bat`** on Windows, or run **`./start.sh`** on
   macOS/Linux. The first run installs its own toolchain and takes a
   few minutes; your browser then opens the app at
   `http://127.0.0.1:8765`. If macOS answers *permission denied*, run
   `chmod +x start.sh uninstall.sh` once in that folder and try again.

3. Read the consent form that opens on the first start and choose Agree
   or Decline.
4. Walk through the tutor that opens next.
5. `Digital.jar`: the first run asks where it is - the same jar you run
   labs with (see the Digital.jar section below if you don't have one). If
   you closed that dialog, it is under **Settings → Digital.jar**.
6. Open **Settings (gear icon, top right)**:
   - **Course server**: paste the **URL + course token** from your
     instructor and save; it answers *connected — token accepted ✓*. That
     powers all AI features. If your instructor announces a new URL or token
     later, paste it in the same place (press **Disconnect** first if an old one is shown).
   - **Language**: pick yours if you wish; the translation cannot yet be guaranteed to 
     sound natural. AI answers stay in English.

![Course server settings](docs/screenshots/settings_course_server.png)

7. Upload your `.dig` files and start debugging: interactive graph, structural
   issues, per-row tests, signal flow, and the Layer 2/3 AI coach.

![A verified Mode A fix card](docs/screenshots/mode_a_card.png)

### The Layer 2 summary and its walkthrough

Summarize circuit on the L2 Library tab returns six cards. The
subcircuit card lists every child with the lab's one-line role
above what the model says about it. The signal-flow card traces
one real test row and can be played as walkthrough.

![Signal-flow walkthrough on the Dashboard](docs/screenshots/signal_flow.gif)

### Working offline

Everything deterministic — the graph, structural issues, per-row tests,
signal flow, subcircuit drill-in, the Layer 2 walkthrough — works with no
internet at all. Only the AI coach needs the course-server connection.

### Telemetry Statement

DLC records anonymized usage events (feature clicks, Layer 1 verdicts, test
runs, coach outcomes, and - between two uploads of the same file - how many
components and wires changed and whether the edit touched what the coach had
named; counts and element kinds only, never the circuit itself) keyed to a
hashed machine id only. Related codes are public
and stored at proxy/ and telemetry/, DLC never modifies a student's 
uploaded files. Events sync to the course server for course-improvement research.
This process begins if and only if admin gains IRB permission from the department. 
When the course server declares a study, DLC asks each student once, on the
first start after connecting, with the UNC information sheet
(`data/consent/COMP311_fa26.md`). Only machines whose student agreed ever send
events; a declined machine records nothing at all. A short optional feedback
question follows some coach answers on agreed machines.
The first round of experimental use is planned to be shut down around December.

When instructor's proxy server shuts down, DLC's AI features will be offline regardless
of Internet connections. 

Deleting and re-downloading the tool continues the same anonymous record. 

DLC dev team is not responsible for any mis-behaviors of modifying students' files outside 
UNC 311 classroom. You will need IRB permission from your department and work on your own fork
of DLC in order to apply it to student and collect related student data. 

### Uninstalling

Run **`UNINSTALL.bat`** / **`./uninstall.sh`** removes the tool's local
data folder `~/.dlc` and delete the unzipped folder.

## Instructor setup

[docs/RELEASE_GUIDE.md](docs/RELEASE_GUIDE.md):

- **Quick setup, Steps 1 to 6**
- **Optional setup, Appendix A**: fork and adapt the limits, the lab
  manifests, the official tests, the ROM programs or the lecture list, and
  build your own zip. Deeper references: [proxy/README.md](proxy/README.md)
  and [docs/MANIFEST_GUIDE.md](docs/MANIFEST_GUIDE.md).
- **Operating the server, Appendix B**: rotating tokens, updating the code,
  backups, what breaks.

Use the built-in data collection only with IRB approval from your
department.

## File layout

| Path | Role |
|---|---|
| `dlc/parser/` | Reads `.dig` XML into structured Python objects: components, wires, nets, signal-flow graph.
| `dlc/facts/` | Extracts a JSON-serializable bundle of facts the LLM and deterministic checkers consume: inventory, per-net widths, per-component topology, structural bug list.
| `dlc/testing/` | Reads each Testcase's embedded test rows out of the `.dig`, parses Digital's CLI output, and pinpoints which specific rows fail.
| `dlc/analyzer/` | Deterministic checkers - wire completeness, bit widths, combinational loops, interface conformance, sequential timing. Shallow (top circuit) and deep (whole subcircuit tree) variants.
| `dlc/sim/` | Deterministic value evaluator (`simulator.py`) that computes the value on every net for a test row, with register state for clocked designs and recursive subcircuit evaluation; `models.py` holds the formula models Layer 3 substitutes for passing subcircuits. Powers the signal-flow-on-row-click UI and the subcircuit drill-in.
| `dlc/web/` | FastAPI server (`server.py`) + browser front-end (`static/`) for the web app: interactive graph, structural-issue overlay, per-row test runner, signal-flow-on-row-click, subcircuit drill-in, and the Layer 2/3 coach.
| `dlc/l3/` | Layer 3: Mode A debugger (evidence, clustering, hypothesis verification) and Mode B coverage coach (manifests, program coach, row injection).
| `dlc/llm/` | LLM client wrapper and versioned prompts for conceptual explanation + credibility grading (Layer 2) and strategic debugging (Layer 3).
| `dlc/telemetry/` | Anonymous machine identity, per-interaction logging to a local SQLite spool, and the shipper that syncs it to the course proxy.
| `proxy/` | The course proxy server an instructor deploys: API-key custody, per-machine daily limits (re-download-proof), global daily circuit breaker, telemetry ingest, admin dashboard/summary/export. `proxy/Dockerfile` and `proxy/openshift/` package it for Carolina CloudApps / OpenShift (Option B).
| `dlc/cli/` | Command-line entrypoint that wires the layers together.
| `prompts/` | Versioned LLM prompt templates - one file per prompt variant, consumed by `dlc/llm/`.
| `data/manifests/` | One manifest per lab (`cpu.json`, `cpu_new.json`, …): categories, subcircuit roles and models, program decode.
| `data/sample_circuits/` | Test fixtures - public sample circuits and buggy circuits created by the authors. No course answer circuits live in this repository.
| `docs/` | Instructor guides (manifest, ROM payload, release runbook) and screenshots; `docs/dev/` holds developer notes (file-format lore, the Layer 3 contract, manual test snippets, function plan).
| `tests/` | pytest unit tests, one file per source module.
| `START_HERE.bat` / `start.sh` | One-click student launchers
| `UNINSTALL.bat` / `uninstall.sh` | Removes DLC and the local `~/.dlc` data folder.
| `scripts/` | Maintainer utilities - `make_release_zip.py` builds the student release zip.

## Design

Mode A design flow: 

[![Digital Lab Coach pipeline](docs/screenshots/mode-A-design.png)](docs/screenshots/mode-A-design.png)

## Developer setup

(For best experience, run the setup and testing flow using bash.)

Need Python version >=3.12; 3.12 is best for developing.

**Linux only — install tkinter at the OS level:**
`uv`-managed Python and many distro Pythons don't bundle tkinter.
DLC needs it for the first-run Digital.jar file-picker dialog and
for 3 file-picker tests in the suite. macOS and Windows ship tkinter
with python.org Python — skip this step there.

```bash
# Debian / Ubuntu
sudo apt install python3-tk
# Fedora / RHEL
sudo dnf install python3-tkinter
# Arch
sudo pacman -S tk
```

**General:**
```bash
# Install uv once (skip if already installed)
# macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Clone and run tests
git clone <repo-url>
cd digital-lab-coach
uv run pytest
```
After all tests are green you are all set — run the web app with:

```bash
uv sync
uv run python -m dlc.web.server
```

**Side notes**

 1. The shell installer only updates the shell it's run from. If you
    install `uv` via Git Bash but want to use it from PowerShell, run
    the PowerShell installer too.

 2. After install, **close and reopen** your terminal (restart VS Code
    if it still can't find `uv`.)

 3. PowerShell doesn't always parse multi-line `python -c "..."` blocks
    cleanly. For the manual snippets in `docs/dev/test_notes.md`, use Git
    Bash, or save the script to a `.py` file and run `uv run python script.py`.

## Troubleshooting (Windows): `uv run` blocked by Smart App Control

**Symptom** — `uv run python ...` fails *before* the app starts:

```
error: Failed to spawn: `python`
  Caused by: ... (os error 4551)
# or, after forcing a system Python:
Querying Python at `...\WindowsApps\python3.exe` failed (exit code 0x800711c7)
```

`os error 4551`:
an application control policy has blocked this file. Windows 11's **Smart App
Control** can switch itself from Evaluation to On (e.g. after an update),
and then it blocks unsigned executables — including the Python `uv` downloads
(python-build-standalone) and the Microsoft Store `python3.exe` alias stub. A
`.venv` built on a now-blocked interpreter stops launching too. This is an
environment/OS block.

**Fix — install a *signed* Python and rebuild the venv:**

1. **Disable the Store alias stubs** so they stop shadowing the real Python:
   Settings → Apps → Advanced app settings → App execution aliases → turn
   **off** `python.exe`, `python3.exe` and `pythonw.exe`.
2. **Install a signed Python 3.12** from <https://www.python.org> (PSF-signed;
   tick "Add python.exe to PATH"). Verify it isn't blocked: `python --version`.
   If Smart App Control still blocks it, install **Python 3.12 from the
   Microsoft Store** instead — Store apps are always trusted by Smart App Control.
3. **Delete the dead venv and rebuild** against the signed Python (Git Bash):

```bash
rm -rf .venv
uv venv --python "C:/Users/<you>/AppData/Local/Programs/Python/Python312/python.exe"
uv sync
uv run python -m dlc.web.server
```

Don't turn Smart App Control *off* to fix this — it is one-way (you can't
re-enable it without reinstalling Windows). Use a signed Python instead.


## Developer Optional setup: Digital.jar for per-row test verification

DLC's structural analysis works on any `.dig` file with no extra setup.

**For per-row pass/fail diagnostics and failing test analysis**, the tool
runs Digital's CLI as a subprocess, so it needs to know where your `Digital.jar` is.

### Setting it up
Download Digital from
<https://github.com/hneemann/Digital>, extract anywhere, and let the first-run dialog find your jar.

If you'd rather configure it manually:

```bash
# Option A
uv run python -c "from dlc.testing.config import set_digital_jar_path; set_digital_jar_path(r'PATH_TO_YOUR_Digital.jar')"

# Option B
# macOS / Linux
export DIGITAL_JAR=/path_to_Digital/Digital.jar
# Windows PowerShell
$env:DIGITAL_JAR = "C:\path_to_Digital\Digital.jar"
```

## License

GPL-3.0. See LICENSE.

## Upstream

Built to read .dig files produced by [Digital](https://github.com/hneemann/Digital),
an open-source educational circuit simulator (GPL-3.0).

## Acknowledgement 

Great thanks to UNC 2025 - 2026 Comp 311 team and all 311 instructors

Great thanks to hneemann




