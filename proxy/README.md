# DLC course proxy

One small server the instructor runs. It does three jobs:

1. **Key custody**: your Anthropic API key lives only here (env var).
   Students' tools relay LLM calls through `/v1/llm`; 

2. **Machine-keyed limits that survive re-downloads**: every install
   reports an anonymous id derived from the OS machine identifier. The
   proxy enforces per-day call budgets per feature (Mode A, Mode B,
   grading, explain) as the wipe-proof backstop behind the client's
   own per-analysis limits.

3. **Telemetry ingest**: students' local event spools sync here. 
   `/admin/summary` shows machines, event counts and an LLM spend estimate.

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
| `DLC_COURSE_TOKEN` | what students paste. Unset, the proxy refuses every `/v1/llm` and `/v1/events` request (503) |
| `DLC_ADMIN_TOKEN` | opens `/admin/*`. Unset, every admin request is refused |
| `DLC_PROXY_DB` | the SQLite ledger (default `./dlc_proxy.db`; the container sets `/data/dlc_proxy.db`); keep it outside the repo |
| `DLC_STUDY_ID` | the IRB study number, e.g. `26-2770`. Set, the tool shows the consent sheet once per machine and events ship only from machines that agreed; unset, nobody is asked and nothing ships |
| `DLC_SURVEY_RATE` | chance (0–1, default `0.35`) that a one-question feedback survey follows a coach answer on an agreed machine, after each feature's first time; `1` for testing |
| `DLC_TIMEZONE` | the course time zone (default `America/New_York`) |

`GET /v1/health` reports `course_token_set`, `admin_token_set`,
`key_configured` and `key_format_ok`; all four must read `true` before
class. It also carries `study_id` and `survey_rate`, which the tool reads to
decide whether to ask for consent. Students paste the course server URL -
`http://<proxy machine's LAN address>:8321` under Option A,
`https://dlc-proxy-<project>.apps.cloudapps.unc.edu` under Option B - plus
the course token under Settings → Course server; the tool stores them in
`~/.dlc/config.json` as `proxy_url` / `proxy_token`.

## Endpoints

| Route | What |
|---|---|
| `POST /v1/llm` | LLM relay (course-token gated): checks the machine's daily budget, attaches your key, forwards through the same client wrapper the tool uses, logs usage. With no key on the proxy it answers "no API key configured — tell your instructor" and spends nothing. |
| `POST /v1/events` | Telemetry batch ingest, deduped on (machine, row id); stamps each machine's authoritative first-seen date. |
| `POST /v1/consent` | Stores a machine's consent decision: install id, sheet version, agreed or declined, typed name, drawn signature. A declined decision deletes that machine's events. |
| `GET /v1/health` | Liveness, counts, the four configuration flags above, `study_id` and `survey_rate`. |
| `GET /admin/view` | The dashboard; asks for the admin token once. Its JSON feeds are `/admin/summary`, `/admin/daily`, `/admin/events`, `/admin/llm_texts`, `/admin/stats`, `/admin/research` (`?token=…` or header `X-DLC-Admin-Token`). |
| `GET /admin/signatures.zip` | Every drawn signature as `sig_<id>.png`, the names `consents.csv` refers to, plus `index.csv`. |
| `GET /admin/consents.html` | A printable consent log: every decision with the typed name and the signature inline. |

## Notes

- Per-machine budgets live in `CALL_BUDGETS` at the top of `dlc_proxy.py`
  (per machine, per server-day, per feature); the whole-class breaker is
  `DLC_GLOBAL_DAILY_CALLS` (600) and `DLC_GLOBAL_DAILY_USD` (20) in the
  environment. The README's *Changing the limits* section shows all three
  layers side by side.
- Storage is one SQLite file — back it up by copying it, or by the CSV
  exports. Under Option B it sits on the volume, which survives restarts,
  redeploys and rebuilds; only deleting the volume claim removes it. The
  release zip never includes `.db` files, but keep the ledger outside the
  repo anyway.
- HTTPS: Option B gives it to you — the route terminates TLS at the
  cluster edge with a valid certificate, so tokens never travel in the
  clear. Plain HTTP is fine for the Option A second-computer smoke test on
  a LAN.
- Updating the proxy never touches the ledger: restart it on the laptop
  (Option A) or push and *Start build* (Option B). The relay reuses
  `dlc/llm/client.py` (same request shaping, timeouts, and reasoning-model
  handling as the tool itself), so keep the deployed branch in step with
  your fork.
