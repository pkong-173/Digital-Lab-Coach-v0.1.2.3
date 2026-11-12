# DLC course proxy

One small server the instructor runs. It does two jobs:

1. **Key custody**: your Anthropic API key lives only here (env var).
   Students' tools relay LLM calls through `/v1/llm`; 

2. **Machine-keyed limits that survive re-downloads**: every install
   reports an anonymous id derived from the OS machine identifier. The
   proxy enforces per-day call budgets per feature (Mode A, Mode B,
   grading, explain) as the wipe-proof backstop behind the client's
   own per-analysis limits.

## What it stores

One row per AI request: the hashed machine id, the time, the feature, the
model, whether the call worked, the token counts, the duration and, for a
failed call, the provider's error message. One row per machine: its hashed id,
the day it was first seen and the time it was last seen. The limits and the
spend estimate are computed from those rows. It stores no prompts, no answers
and nothing else about what a student does in the tool.

## Run it

Two ways, both in the instructor guide
[../docs/RELEASE_GUIDE.md](../docs/RELEASE_GUIDE.md):

- **On your own laptop** (Option A): four variables in one terminal
  window, then
  ```bash
  uv run uvicorn proxy.dlc_proxy:app --host 0.0.0.0 --port 8321
  ```
- **On Carolina CloudApps / any OpenShift cluster** (Option B): the
  console builds `Dockerfile` in this folder straight from your GitHub
  fork, the three variables live in one Secret, the ledger on a 1 GiB
  volume mounted at `/data`, and students get an HTTPS URL that works from
  anywhere. `openshift/dlc-proxy.yaml` is the same setup for the `oc`
  command line. The image builds from the repo root
  (`docker build -f proxy/Dockerfile .`), listens on `$PORT` (8080) and
  runs as a non-root user.

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | the key the relay uses; no endpoint ever returns it |
| `DLC_COURSE_TOKEN` | what students paste. Unset, the proxy refuses every student request (503) |
| `DLC_ADMIN_TOKEN` | opens the dashboard and its data under `/admin/*`; Unset, every admin request is refused |
| `DLC_PROXY_DB` | the SQLite ledger (default `./dlc_proxy.db`; the container sets `/data/dlc_proxy.db`); keep it outside the repo |
| `DLC_TIMEZONE` | the course time zone (default `America/New_York`) |

`GET /v1/health` reports `course_token_set`, `admin_token_set`,
`key_configured` and `key_format_ok`; all four must read `true` before
class. Students paste the course server URL -
`http://<proxy machine's LAN address>:8321` under Option A,
`https://dlc-proxy-<project>.apps.cloudapps.unc.edu` under Option B - plus
the course token under Settings → Course server; the tool stores them in
`~/.dlc/config.json` as `proxy_url` / `proxy_token`.

## Endpoints

| Route | What |
|---|---|
| `POST /v1/llm` | LLM relay (course-token gated): checks the machine's daily budget, attaches your key, forwards through the same client wrapper the tool uses, and counts the call for that machine. Neither the prompt nor the answer is stored. With no key on the proxy it answers "no API key configured - tell your instructor" and spends nothing. |
| `POST /v1/events` | Token check: the tool calls it when a student saves the course server URL and token. Only the machine id in the request is read, to note the day the machine was first seen; nothing else is kept. (The path name is historical.) |
| `POST /v1/<anything else>` | Token check only; the request body is not read. Earlier versions of the tool post more than this server takes. |
| `GET /v1/health` | Liveness, counts and the four configuration flags above. |
| `GET /admin/view` | The dashboard; asks for the admin token once. Its JSON feeds are `/admin/summary`, `/admin/daily` and `/admin/stats` (header `X-DLC-Admin-Token`). |
| `GET /admin/export.csv` | The dashboard's tables as CSV: `?table=daily` (default; AI calls per day, machine and feature) or `?table=machines`; same header. The dashboard has a button for each. |

## Notes

- Per-machine budgets live in `CALL_BUDGETS` at the top of `dlc_proxy.py`
  (per machine, per server-day, per feature); the whole-class breaker is
  `DLC_GLOBAL_DAILY_CALLS` (600) and `DLC_GLOBAL_DAILY_USD` (20) in the
  environment. The README's *Changing the limits* section shows all three
  layers side by side.
- Storage is one SQLite file, the ledger, with two tables: `machines` and
  `llm_calls`. Back it up by copying it. Under Option B it sits on the
  volume, which survives restarts, redeploys and rebuilds; only deleting
  the volume claim removes it. Keep it outside of repo anyway.
- A database file written by an earlier version is used as it is: this
  version reads and writes only those two tables. To start clean, stop the
  server, delete the file and start it again.
- HTTPS: Option B gives it to you — the route terminates TLS at the
  cluster edge with a valid certificate, so tokens never travel in the
  clear. Plain HTTP is fine for the Option A second-computer smoke test on
  a LAN.
- Time limit under Option B: the OpenShift router gives up on a request
  that is still unanswered after 30 seconds unless the route says
  otherwise, while a model call may take up to `DLC_LLM_TIMEOUT` (180 s).
  The route therefore carries the annotation
  `haproxy.router.openshift.io/timeout: 240s` (step 3B.5 of the release
  guide); without it a slow answer reaches the student as HTTP 504 while
  the server still finishes and counts the call.
- Updating the proxy never touches the ledger: restart it on the laptop
  (Option A) or push and *Start build* (Option B). The relay reuses
  `dlc/llm/client.py` (same request shaping, timeouts, and reasoning-model
  handling as the tool itself), so keep the deployed branch in step with
  your fork.
