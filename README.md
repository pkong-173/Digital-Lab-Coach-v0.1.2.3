# Digital Lab Coach (DLC)

[![Download](docs/download_button.svg)](https://github.com/pkong-173/Digital-Lab-Coach-v0.1.2.3/releases/latest/download/DigitalLabCoach.zip)

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
- [Developer setup + Troubleshooting (Windows) + Digital.jar](#developer-setup)
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

[docs/dev_setup_and_troubleshooting.md](docs/dev_setup_and_troubleshooting.md)

## License

GPL-3.0. See LICENSE.

## Upstream

Built to read .dig files produced by [Digital](https://github.com/hneemann/Digital),
an open-source educational circuit simulator (GPL-3.0).

## Acknowledgement 

Great thanks to UNC 2025 - 2026 Comp 311 team and all 311 instructors

Great thanks to hneemann




