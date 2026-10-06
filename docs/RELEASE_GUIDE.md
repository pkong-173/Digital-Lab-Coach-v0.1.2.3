# Instructor Guide: set up DLC for a course (Last updated: 9/28/26)

Everything optional, such as forking DLC for another course, changing limits or
lab manifests, sits in [Appendix A](#appendix-a-optional-setup-fork-and-adapt).
Proxy operation and troubleshooting sit in
[Appendix B](#appendix-b-operating-the-course-server).

## Step 1: Get a Claude API key

1. Copy the key

DLC caps spend: 600 calls and $20 per day for the whole class by default,
plus per-student and per-machine daily limits.

## Step 2: Generate the course token and the admin token

Run these two lines in any terminal that has Python:

```bash
python -c "import secrets; print('course-' + secrets.token_urlsafe(18))"
python -c "import secrets; print('admin-'  + secrets.token_urlsafe(18))"
```

Fill in this **course card** and keep it in a private note for look up.

| Course card | Your value | Who sees it |
|---|---|---|
| API key | `sk-ant-…` | the course server only |
| Course token | `course-…` | students and you |
| Admin token | `admin-…` | you only |
| Course server URL | filled in at Step 4 | students and you |

## Step 3: Put the three values into the course server

Pick one place to run the server:

| | Option B: Carolina CloudApps | Option A: your own laptop |
|---|---|---|
| Reachable from | anywhere, HTTPS | the same Wi-Fi only, plain HTTP |
| Stays up | always, restarts itself | while the laptop is awake and the terminal open |
| Good for | a semester of use| a smoke test or one section in one room |
| Setup | ~ 20 minutes | ~ 5 minutes |

Do **3B** or **3A**, not both.

### 3B: Carolina CloudApps

[Carolina CloudApps](https://cloudapps.unc.edu) is UNC's OpenShift cluster,
free to UNC affiliates. Open `console.apps.cloudapps.unc.edu`, Developer
view. You have exactly one project, named after your Onyen; it is
`<project>` below. **Never delete it**: users cannot create projects, and
only ITS can restore one (a ticket to CloudApps support).

**3B.1 The Secret.** Left menu **Secrets** → **Create** → **Key/value
secret**. Name `dlc-proxy-secrets`, then add three kv pairs from note:
`ANTHROPIC_API_KEY`, `DLC_COURSE_TOKEN`, `DLC_ADMIN_TOKEN`. Create.

**3B.2 Import the server.** **+Add** → **Import from Git**, fill in top to
bottom, then **Create**:

| Field | Value |
|---|---|
| Git Repo URL | `https://github.com/pkong-173/Digital-Lab-Coach-v0.1.2.3` (or your fork), wait for *Validated* |
| Git reference | `master` |
| Context dir | `/` |
| Edit Import Strategy | **Dockerfile**, path `proxy/Dockerfile` |
| Application name / Name | `dlc` / `dlc-proxy` |
| Build option | BuildConfig, advanced options untouched, its environment variables empty |
| Resource type | Deployment |
| Advanced Deployment option → Environment variables | **Add from ConfigMap or Secret** three times: Name `ANTHROPIC_API_KEY` from `dlc-proxy-secrets` key `ANTHROPIC_API_KEY`, the same for `DLC_COURSE_TOKEN` and `DLC_ADMIN_TOKEN`; then **Add value** twice: Name `DLC_STUDY_ID`, Value your IRB number, e.g. `26-2770`; Name `DLC_SURVEY_RATE`, Value `0.35` (leave both out while no study runs) |
| Target port | `8080` |
| Create a route | ticked; advanced Routing: **Secure Route**, TLS termination **Edge**, Insecure traffic **Redirect**, everything else empty |

The build takes about a minute; the circle in Topology turns dark blue when
the server runs.

**3B.3 Storage.** Topology → click the circle → **Actions** → **Add
Storage**: Create new claim, the default StorageClass, name
`dlc-proxy-data`, Single user (RWO), 1 GiB, mount path `/data`. Save.
Without this step every redeploy would wipe the data.

**3B.4 Restart policy and health checks.** **Actions** → **Edit Deployment**
→ Strategy type **Recreate** → Save. Then **Actions** → **Add Health
Checks**: a Readiness probe and a Liveness probe, both HTTP GET on
`/v1/health`, port 8080. Add.

Your course server URL is `https://dlc-proxy-<project>.apps.cloudapps.unc.edu`.
Write it on the card note.

### 3A: Your own laptop

Open **one** terminal in a clone of this repository and paste the block for
your OS with the card's values. Keep the window open: closing it stops the
server.

**Windows, Command Prompt**

```bat
cd C:\path\to\DLC
set ANTHROPIC_API_KEY=sk-ant-...
set DLC_COURSE_TOKEN=course-...
set DLC_ADMIN_TOKEN=admin-...
set DLC_PROXY_DB=C:\dlc-proxy\dlc_proxy.db
set DLC_STUDY_ID=26-2770
set DLC_SURVEY_RATE=0.35
uv run uvicorn proxy.dlc_proxy:app --host 0.0.0.0 --port 8321
```

**Windows, PowerShell** (here `set X=Y` does nothing; use these lines)

```powershell
cd C:\path\to\DLC
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:DLC_COURSE_TOKEN = "course-..."
$env:DLC_ADMIN_TOKEN = "admin-..."
$env:DLC_PROXY_DB = "C:\dlc-proxy\dlc_proxy.db"
$env:DLC_STUDY_ID = "26-2770"
$env:DLC_SURVEY_RATE = "0.35"
uv run uvicorn proxy.dlc_proxy:app --host 0.0.0.0 --port 8321
```

**macOS / Linux**

```bash
cd ~/path/to/DLC
export ANTHROPIC_API_KEY=sk-ant-...
export DLC_COURSE_TOKEN=course-...
export DLC_ADMIN_TOKEN=admin-...
export DLC_PROXY_DB=$HOME/dlc-proxy/dlc_proxy.db
export DLC_STUDY_ID=26-2770
export DLC_SURVEY_RATE=0.35
uv run uvicorn proxy.dlc_proxy:app --host 0.0.0.0 --port 8321
```

`DLC_STUDY_ID` switches on the consent sheet and the feedback survey.
Leave that line out while no study runs. `DLC_SURVEY_RATE` is the chance that a survey follows a coach answer
(`0.35` above is the default; `1` when you test, `0` for no surveys).

A clean start prints no `WARNING:` line. Then find the laptop's address on
the network: Windows `ipconfig` (the IPv4 Address of the connected adapter),
macOS `ipconfig getifaddr en0`, Linux `hostname -I`. Your course server URL
is `http://<LAN address>:8321`. Write it on the card.

## Step 4: Check it once

Open the course server URL plus `/v1/health` in a browser. Four flags must
read `true`:

| Flag | `false` means |
|---|---|
| `course_token_set` | the server refuses every student until the course token is set |
| `admin_token_set` | the dashboard rejects every admin token |
| `key_configured` | students see "the course server has no API key configured" |
| `key_format_ok` | the key was pasted wrong |

`study_id` shows the IRB number when a study is on, `null` otherwise. With
`null` no student is ever asked for consent and no usage data ever leaves a
machine.

Option B: a `false` means an environment row in 3B.2 is missing or misnamed.
Option A: a `false` means a `set` or `export` line was skipped; fix it and
run the last line again.

## Step 5: Distribute to students

Send students two things: the course server URL and the course token, plus
a link to the README's Student quick setup. Keep the admin token to
yourself.

Students paste the URL and the token under Settings → Course server in DLC;
it answers *connected — token accepted ✓*. Nothing else is needed on their
side other picking digital.jar location.

## Step 6: Use it yourself and watch the dashboard

To use DLC yourself, follow the README's Student quick setup with the same
URL and token.

The dashboard is the course server URL plus `/admin/view`, opened with the
admin token: machines seen, activity per day, LLM calls and estimated spend
against the caps, Layer 1 verdicts, test runs, and coach outcomes for both
modes.

![Course dashboard](screenshots/admin_dashboard.png)
![Course dashboard, stats](screenshots/admin_dashboard2.png)

Use the data collection only with IRB approval from your department.

## Student setup

Same as the README's Student quick setup

## Appendix A: Optional setup (fork and adapt)

Only for a course that changes how DLC works. Fork the repository, change
what the table says, run the suite (`uv run pytest -q`), then do Steps 1 to 6 above
with your fork as the source. And A.6 below builds your own release zip.

### A.1 What to adapt and where

| To adapt | Where | Guide |
|---|---|---|
| The official tests students are graded against | `data/official_tests_defaults.json`, or Settings → Official tests in the running app | [instructor_rom_config.md](instructor_rom_config.md) for labs with a ROM program |
| Lab manifests: categories, subcircuit roles and formula models, program decode | one file per lab in `data/manifests/` | [MANIFEST_GUIDE.md](MANIFEST_GUIDE.md) |
| The lecture list the AI cites | `SYLLABUS_311` in `dlc/llm/explain.py` | A.3 |
| Daily caps, per-machine budgets, the whole-class breaker | A.2 | |
| The course server itself | `proxy/dlc_proxy.py` | [proxy/README.md](../proxy/README.md): endpoints, variables, container |
| Everything else | A.5 | |

### A.2 Changing the limits

| Layer | Counts | Default | Change it in |
|---|---|---|---|
| Per-student daily caps | runs per day, on the student's machine | Mode A 1, Mode B 2 | `CAPS` at the top of `dlc/l3/limits.py`; counted only when the app runs with `DLC_ENFORCE_LIMITS=1`, which the launchers set |
| Per-machine backstop | LLM calls per day per machine, on the server | modeA 4, modeB 4, grade 2, explain 2 | `CALL_BUDGETS` at the top of `proxy/dlc_proxy.py` |
| Whole-class breaker | calls per day and estimated $ per day | 600 calls, $20 | env `DLC_GLOBAL_DAILY_CALLS`, `DLC_GLOBAL_DAILY_USD` on the server |

A Mode A run counts against the student cap only when it delivers a verified
card. One Mode A run makes one to four LLM calls and one Mode B run two or
three, so the per-machine backstop counts calls, not runs; When the breaker trips, 
every AI request answers "the course server has reached its daily capacity" until midnight.

### A.3 Adapting the course syllabus (Layer 2 lecture tags)

1. Edit `SYLLABUS_311` near the top of `dlc/llm/explain.py`: one line per
   lecture, `Lecture N: topic`. The Layer 2 summary and its grader both tag
   lectures against this list.
2. Optional: the course name "UNC COMP 311" also appears in the prompt
   headers under `prompts/` and in `dlc/llm/explain.py`.
3. Restart the server.

### A.4 Subcircuits as formula models (Layer 3 Mode A)

Mode A starts only once every subcircuit passes its own tests, and it then
evaluates a passing child through its formula model instead of gate by gate.
Nothing to configure for the shipped 311 labs: a model is picked by the
child's interface and used only after it reproduces every row of the child's
own testcase; a child without a testcase is simulated as drawn. To name,
force or switch off a model per file, add a `subcircuits` block to the lab
manifest ([MANIFEST_GUIDE.md](MANIFEST_GUIDE.md)); the same block carries
each subcircuit's one-line `role`. Layer 1's signal flow never uses models.

Two built-in manifests: `data/manifests/cpu.json` for the eight-instruction
CPU lab subset and `data/manifests/cpu_new.json` for the 37-instruction RV32I
CPU. For RV32I the Coverage Coach runs the program through a small
interpreter, follows branches and jumps, and splices extensions in front of
the `jal x0, 0` halt loop.

### A.5 Where to change what

Restart the app, or the server, after changing any of these.

| To change… | Edit / set |
|---|---|
| Daily caps, per-machine budgets, whole-class breaker | A.2 |
| Course token / admin token | server variables `DLC_COURSE_TOKEN`, `DLC_ADMIN_TOKEN`; the Secret `dlc-proxy-secrets`|
| Where the server keeps its data | variable `DLC_PROXY_DB` (default `./dlc_proxy.db`; the container uses `/data/dlc_proxy.db` on its volume) |
| Which model each Layer 3 mode uses | the picker on each Layer 3 board (Sonnet default or Opus, per run); the default comes from env `DLC_L3_DEBUG_MODEL` / `DLC_L3_PROPOSE_MODEL`, else `l3_debug_model` / `l3_propose_model` in `~/.dlc/config.json` |
| LLM call timeout | env `DLC_LLM_TIMEOUT` (seconds, default 180) |
| The research study on or off | env `DLC_STUDY_ID` on the server (the IRB number; unset = no study)|
| How often the feedback survey asks | env `DLC_SURVEY_RATE` on the server (0–1, default `0.35` in `_survey_rate()` in `proxy/dlc_proxy.py`); the first-time rule, the 20-minute gap, the 2-per-session cap and the half rate for Layer 1 and the walkthrough are the constants at the top of `dlc/web/static/consent.js` |
| Lecture list Layer 2 cites | `SYLLABUS_311` in `dlc/llm/explain.py` (A.3) |
| Lab categories, subcircuit roles and formula models, program decode | one manifest per lab in `data/manifests/` ([MANIFEST_GUIDE.md](MANIFEST_GUIDE.md)) |
| Files Mode A analyzes even when most rows fail | the `no_lazy_gate` list in that lab's manifest; the shipped CPU manifests list the control unit |
| Official tests | Settings → Official tests (`~/.dlc/official_tests.json`); shipped defaults in `data/official_tests_defaults.json` |
| The program a lab's instruction ROM must hold | the `runtime` entry in `data/official_tests_defaults.json` ([instructor_rom_config.md](instructor_rom_config.md)) |
| The formula models themselves | `dlc/sim/models.py`, one function per known subcircuit |
| Digital.jar location | first-run dialog, Settings, or env `DIGITAL_JAR` |
| Release version | `version` in `pyproject.toml`|

### A.6 Build your own release zip

1. Bump `version` in `pyproject.toml` and the Status line at the top of the
   README; commit.
2. `uv run python scripts/make_release_zip.py` writes `dist/DigitalLabCoach.zip`.
3. `git tag v0.1.2.3` and `git push origin v0.1.2.3`.
4. GitHub → Releases → Draft a new release → the tag → attach the zip with
   exactly that filename → Publish. The README's Download button serves the
   latest release.

## Appendix B: Operating the course server

### B.1 Rotate a token or swap the API key

1. Generate the new value (Step 2's command for a token) and update the card.
2. Option B: Secrets → `dlc-proxy-secrets` → Actions → Edit Secret → change
   the value → Save; then Topology → Actions → **Restart rollout**. Option A:
   stop the server with Ctrl+C and run the block from step 3A again with the new
   value.
3. A new course token is announced to students, who paste it under Settings →
   Course server. History and limits are untouched.

### B.2 Update the server code

Option B: push to the branch you imported, then Builds → `dlc-proxy` →
Actions → **Start build**. The new image deploys itself in about a minute;
the data stays on the volume. Option A: Ctrl+C, `git pull`, run the block
again. Changes under `proxy/` or `dlc/llm/` need this; anything else ships
in the student zip instead.

### B.3 Pause, back up, resume

Option B: Topology → circle → Details → the ↓ arrow sets the pod count to 0;
↑ brings it back with everything in place. Option A: the data is the
one file named in `DLC_PROXY_DB`; copy it.

### B.4 Prove it works from a second device

Option B: open the health URL on a phone with Wi-Fi off. JSON means students
can reach it from anywhere. Option A: on a second laptop on the same Wi-Fi,
paste the URL and the course token into DLC and run one AI feature; the
dashboard then lists that laptop. On that laptop `http://localhost:8321`
must fail, which proves nobody reaches the AI without the real URL.

### B.5 What breaks Option A

- **Firewall.** Windows Defender blocks port 8321 until you allow it on
  private and public networks; macOS asks once.
- **The address moves.** Check `ipconfig` or `ipconfig getifaddr en0` before
  each class, or reserve the address on the router.
- **Client isolation.** Campus Wi-Fi often stops laptops reaching each other.
  If the health URL works on the laptop but not on a second one, this is
  why, and only Option B gets around it.
- **Sleep.** A closed lid stops the server. macOS: `caffeinate -i uv run
  uvicorn …`; Windows: a power plan that never sleeps while plugged in.
- **Off campus.** A LAN address works on that network only; students at home
  get can't be reached.

### B.6 What breaks Option B

- **The build fails pulling `python:3.12-slim`.** Docker Hub's pull limit;
  start the build again a little later.
- **A health flag is `false`.** An environment row in 3B.2 is missing or its
  Name differs from the Secret key.
- **The pod restarts in a loop after Add Storage.** Resources → the pod →
  View logs; a permission error on `/data` means the storage class does not
  suit a single-user volume, pick the other one.
- **The pod count shows 0 one morning.** Something set the replica count to
  0; the ↑ arrow in Details brings it back and nothing was lost. The
  Deployment's Events tab shows the time; if it recurs without a click,
  the cluster is idling the app.
- **Students get *can't be reached*.** They pasted `http://`, a port, or
  `/admin/view` on the end. The URL is the bare `https://…` host.
- **No Dockerfile choice in Import Strategy.** The cluster forbids
  Dockerfile builds; ask its admins, or use Option A.
- **The project is gone.** Deleting the project deletes the server, the
  Secret, the volume and the data, and you cannot create a new one. Ask
  CloudApps support to restore it, then do 3B.1 to 3B.4 again.