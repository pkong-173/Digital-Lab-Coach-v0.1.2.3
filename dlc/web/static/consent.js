window.dlcFirstRun = new Promise((resolve) => { window.__dlcFirstRunDone = resolve; });

(function () {
  const SURVEY_MIN_GAP_MS = 20 * 60 * 1000;
  const SURVEY_FIRST_GAP_MS = 60 * 1000;   // between the first-time questions of two features
  const SURVEY_MAX_PER_FEATURE = 2; // per browser session
  const SURVEY_TIMEOUT_MS = 3 * 60 * 1000;
  let studyState = null;
  let consentOpen = false;
  const surveyShown = {};

  function esc(s) { return typeof escapeHtml === "function" ? escapeHtml(String(s)) : String(s); }
  function log(kind, details) { try { if (typeof logEvent === "function") logEvent(kind, details || {}); } catch {} }
  function flush() { try { if (typeof flushTelemetry === "function") flushTelemetry(); } catch {} }
  function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  async function getJson(url) { const r = await fetch(url); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
  async function postJson(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let b = null; try { b = await r.json(); } catch {}
    return { ok: r.ok, status: r.status, body: b };
  }
  function renderMd(md) {
    if (typeof mdLiteRender === "function") return mdLiteRender(md);
    return `<pre style="white-space:pre-wrap">${esc(md)}</pre>`;
  }

  /* ---------------------------------------------------- 1. course server */
  const VERIFY_TEXT = {
    accepted: ["connected — token accepted ✓", false],
    bad_token: ["server reachable but the token was REJECTED — check the exact string from your instructor", true],
    unreachable: ["saved, but the course server can't be reached right now — check the URL", true],
  };

  async function courseServerGate() {
    let pr = null;
    try { pr = await getJson("/api/config/proxy"); } catch { return; }
    if (!pr || pr.configured) return;
    let asked = false;
    try { asked = localStorage.getItem("dlc_connect_asked") === "1"; } catch {}
    if (asked) return;
    return new Promise((resolve) => {
      const gate = el(`
        <div class="dlc-gate" id="dlc-connect-gate" role="dialog" aria-modal="true">
          <div class="dlc-gate-card">
            <h3>Connect to your course server</h3>
            <p class="muted">Your instructor gave you a server URL and a course token. Paste them once;
              they land in Settings → Course server, where you can change them later.
              No personal API key is needed.</p>
            <label style="font-size:12.5px;color:#374151">Course server URL</label>
            <input id="dlc-connect-url" class="text-input" type="text" placeholder="https://dlc-proxy-....apps.cloudapps.unc.edu"/>
            <label style="font-size:12.5px;color:#374151;margin-top:8px;display:block">Course token</label>
            <input id="dlc-connect-token" class="text-input" type="password" placeholder="course-..."/>
            <div class="dlc-gate-msg" id="dlc-connect-msg"></div>
            <div class="dlc-gate-row">
              <button id="dlc-connect-skip" class="btn-ghost">I don't have one yet</button>
              <div class="spacer"></div>
              <button id="dlc-connect-go" class="btn">Connect</button>
            </div>
          </div>
        </div>`);
      document.body.appendChild(gate);
      const msg = gate.querySelector("#dlc-connect-msg");
      const done = () => { gate.remove(); try { localStorage.setItem("dlc_connect_asked", "1"); } catch {} resolve(); };
      gate.querySelector("#dlc-connect-skip").addEventListener("click", () => { log("connect_gate_skipped", {}); done(); });
      gate.querySelector("#dlc-connect-go").addEventListener("click", async () => {
        const url = (gate.querySelector("#dlc-connect-url").value || "").trim();
        const token = (gate.querySelector("#dlc-connect-token").value || "").trim();
        if (!url) { msg.textContent = "The course server URL is required."; msg.className = "dlc-gate-msg err"; return; }
        msg.textContent = "Connecting…"; msg.className = "dlc-gate-msg";
        const r = await postJson("/api/config/proxy", { url, token });
        if (!r.ok || !r.body || !r.body.ok) { msg.textContent = "Save failed."; msg.className = "dlc-gate-msg err"; return; }
        const [text, bad] = VERIFY_TEXT[r.body.verify] || ["Saved.", false];
        msg.textContent = text; msg.className = "dlc-gate-msg " + (bad ? "err" : "ok");
        log("connect_gate_saved", { verify: r.body.verify || "n/a" });
        if (typeof renderProxyState === "function") renderProxyState();
        if (typeof refreshKeyChip === "function") refreshKeyChip();
        if (r.body.verify === "accepted") setTimeout(done, 700);
      });
    });
  }

  /* ------------------------------------------------------- 2. consent */
  function signaturePad(canvas) {
    const ctx = canvas.getContext("2d");
    const scale = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 460, h = canvas.clientHeight || 110;
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    ctx.scale(scale, scale);
    ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.strokeStyle = "#111827";
    let drawing = false, drawn = false;
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    canvas.addEventListener("pointerdown", (e) => { drawing = true; drawn = true; const [x, y] = pos(e); ctx.beginPath(); ctx.moveTo(x, y); canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointermove", (e) => { if (!drawing) return; const [x, y] = pos(e); ctx.lineTo(x, y); ctx.stroke(); });
    const stop = () => { drawing = false; };
    canvas.addEventListener("pointerup", stop); canvas.addEventListener("pointercancel", stop);
    return {
      clear() { ctx.clearRect(0, 0, w, h); drawn = false; },
      dataUrl() { return drawn ? canvas.toDataURL("image/png") : null; },
    };
  }

  function showConsentModal(st, md) {
    consentOpen = true;
    return new Promise((resolve) => {
      const gate = el(`
        <div class="dlc-gate" id="dlc-consent-gate" role="dialog" aria-modal="true" data-i18n-skip>
          <div class="dlc-gate-card">
            <h3>Research participation</h3>
            <p class="muted">Please read the information sheet. Taking part is voluntary; every feature of
              DLC works either way. You decide once.</p>
            <div class="dlc-consent-doc" id="dlc-consent-doc">${renderMd(md)}</div>
            <div class="dlc-consent-form">
              <label for="dlc-consent-name">Type your full name (this is your electronic signature)</label>
              <input id="dlc-consent-name" class="text-input" type="text" autocomplete="name" placeholder="First Last"/>
              <div class="dlc-sig-wrap">
                <label>Optional: draw your signature</label>
                <canvas id="dlc-consent-sig" class="dlc-sig-canvas"></canvas>
                <div class="dlc-sig-tools"><button type="button" id="dlc-sig-clear" class="btn-ghost">Clear</button>
                  <span>mouse or finger</span></div>
              </div>
              <label class="check"><input type="checkbox" id="dlc-consent-agree"/>
                <span>I am 18 or older, I have read the information above, and I agree to take part in this research study.</span></label>
            </div>
            <div class="dlc-gate-msg" id="dlc-consent-msg"></div>
            <div class="dlc-gate-row">
              <button id="dlc-consent-decline" class="btn-ghost">Decline</button>
              <div class="spacer"></div>
              <button id="dlc-consent-yes" class="btn" disabled>Agree and continue</button>
            </div>
          </div>
        </div>`);
      document.body.appendChild(gate);
      const name = gate.querySelector("#dlc-consent-name");
      const box = gate.querySelector("#dlc-consent-agree");
      const yes = gate.querySelector("#dlc-consent-yes");
      const msg = gate.querySelector("#dlc-consent-msg");
      const pad = signaturePad(gate.querySelector("#dlc-consent-sig"));
      gate.querySelector("#dlc-sig-clear").addEventListener("click", () => pad.clear());
      const update = () => { yes.disabled = !(box.checked && name.value.trim().length >= 2); };
      name.addEventListener("input", update); box.addEventListener("change", update);
      log("consent_shown", { version: st && st.version, study_id: st && st.study_id });

      const send = async (decision) => {
        msg.textContent = "Saving…"; msg.className = "dlc-gate-msg";
        const r = await postJson("/api/consent", {
          decision, name: name.value.trim(), signature: decision === "agreed" ? pad.dataUrl() : null,
        });
        if (!r.ok || !r.body || !r.body.ok) {
          msg.textContent = (r.body && r.body.detail) || "Could not save your answer. Please try again.";
          msg.className = "dlc-gate-msg err"; return;
        }
        studyState = r.body.state || studyState;
        if (decision === "agreed") { log("consent_agreed", { version: r.body.version }); flush(); }
        gate.remove();
        consentOpen = false;
        renderResearchCard();
        resolve(decision);
      };
      yes.addEventListener("click", () => send("agreed"));
      gate.querySelector("#dlc-consent-decline").addEventListener("click", () => {
        if (!confirm("Decline the research study? DLC keeps working with every feature and records nothing about your use for the study. The course server only counts AI requests per computer to apply the daily limit.")) return;
        send("declined");
      });
    });
  }

  async function consentGate() {
    if (consentOpen || document.getElementById("dlc-consent-gate")) return;
    let st = null;
    try { st = await getJson("/api/consent/state?refresh=1"); } catch { return; }
    studyState = st;
    if (!st || !st.required || !st.study_id) return;
    let md = "";
    try { md = await (await fetch("/api/consent/text")).text(); } catch {}
    if (!md.trim()) return;
    return showConsentModal(st, md);
  }

  function watchSettingsSave() {
    const btn = document.getElementById("proxy-save-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      let tries = 0;
      const tick = async () => {
        tries += 1;
        let pr = null;
        try { pr = await getJson("/api/config/proxy"); } catch {}
        if (pr && pr.configured) { await consentGate(); renderResearchCard(); return; }
        if (tries < 8) setTimeout(tick, 800);
      };
      setTimeout(tick, 900);
    });
  }

  /* ------------------------------------------------ 3. settings card */
  async function renderResearchCard() {
    const state = document.getElementById("set-research-state");
    if (!state) return;
    let st = studyState;
    try { st = await getJson("/api/consent/state"); studyState = st; } catch {}
    if (!st || !st.study_id) {
      state.textContent = "no study on this course server — nothing is asked";
      state.classList.remove("settings-bad");
      return;
    }
    const when = st.decided_at ? new Date(st.decided_at * 1000).toLocaleDateString() : "";
    if (st.decision === "agreed") {
      state.textContent = `taking part in study ${st.study_id}${st.name ? ", signed " + st.name : ""}${when ? ", " + when : ""}`;
      state.classList.remove("settings-bad");
    } else if (st.decision === "declined") {
      state.textContent = `declined${when ? " on " + when : ""} - nothing is recorded for the study`;
      state.classList.remove("settings-bad");
    } else {
      state.textContent = `study ${st.study_id} is on — the sheet appears on the next start`;
      state.classList.add("settings-bad");
    }
  }

  /* ----------------------------------------------- 4. feedback survey */
  // One question at a time, one click each. The features with a full
  // answer (coach output) get two questions; Layer 1 cards and the
  // walkthrough get one. An optional comment box closes every path.
  const QUESTIONS = {
    modeA: { intro: "the Mode A fix analysis", rate: 1, steps: [
      ["helpful", "Was this feedback helpful?", [["yes", "Yes"], ["somewhat", "Somewhat"], ["no", "No"]]],
      ["answered", "Did it answer your question or help you identify the problem?", [["yes", "Yes"], ["partially", "Partially"], ["no", "No"]]]] },
    modeB: { intro: "the Coverage Coach proposals", rate: 1, steps: [
      ["helpful", "Was this feedback helpful?", [["yes", "Yes"], ["somewhat", "Somewhat"], ["no", "No"]]],
      ["answered", "Did it answer your question or help you identify the problem?", [["yes", "Yes"], ["partially", "Partially"], ["no", "No"]]]] },
    explain: { intro: "the Layer 2 summary", rate: 1, steps: [
      ["helpful", "Was this feedback helpful?", [["yes", "Yes"], ["somewhat", "Somewhat"], ["no", "No"]]],
      ["answered", "Did it answer your question or help you identify the problem?", [["yes", "Yes"], ["partially", "Partially"], ["no", "No"]]]] },
    l1: { intro: "the issue cards", rate: 0.5, steps: [
      ["helpful", "Did the issue cards point you to the right place?", [["yes", "Yes"], ["somewhat", "Somewhat"], ["no", "No"]]]] },
    walkthrough: { intro: "the signal-flow walkthrough", rate: 0.5, steps: [
      ["helpful", "Did the walkthrough help you see the problem?", [["yes", "Yes"], ["somewhat", "Somewhat"], ["no", "No"]]]] },
  };

  // which features already asked once on this machine: {feature: time}
  function surveySeen() {
    try { return JSON.parse(localStorage.getItem("dlc_survey_seen") || "{}") || {}; } catch { return {}; }
  }
  function markSurveyShown(feature) {
    const seen = surveySeen();
    seen[feature] = Date.now();
    try { localStorage.setItem("dlc_survey_seen", JSON.stringify(seen)); } catch {}
    try { localStorage.setItem("dlc_survey_last", String(Date.now())); } catch {}
  }

  function surveyAllowed(feature, force) {
    const q = QUESTIONS[feature];
    if (!q) return false;
    if (!studyState || !studyState.study_id || studyState.decision !== "agreed") return false;
    if (consentOpen || document.getElementById("dlc-survey")) return false;
    if (force) return true;
    const rate = Number(studyState.survey_rate || 0) * q.rate;
    if (!(rate > 0)) return false;
    let last = 0; try { last = Number(localStorage.getItem("dlc_survey_last") || 0); } catch {}
    // the first time each feature answers on this machine it always asks, at
    // least a minute after any other question; from then on the survey rate,
    // a 20-minute gap and a per-session cap decide
    if (!surveySeen()[feature]) return Date.now() - last >= SURVEY_FIRST_GAP_MS;
    if ((surveyShown[feature] || 0) >= SURVEY_MAX_PER_FEATURE) return false;
    if (Date.now() - last < SURVEY_MIN_GAP_MS) return false;
    return Math.random() < rate;
  }

  window.dlcMaybeAskFeedback = function (feature, filename, force) {
    if (!surveyAllowed(feature, !!force)) return;
    const q = QUESTIONS[feature];
    surveyShown[feature] = (surveyShown[feature] || 0) + 1;
    markSurveyShown(feature);
    const answers = {};
    let step = 0, closed = false, timer = null;
    const box = el(`
      <div class="dlc-survey" id="dlc-survey" role="dialog" aria-label="Feedback">
        <button class="close" id="dlc-survey-x" title="Skip">&times;</button>
        <h4>Quick question about ${esc(q.intro)}</h4>
        <div id="dlc-survey-body"></div>
      </div>`);
    document.body.appendChild(box);
    log("feedback_survey_shown", { feature, filename });

    const finish = (reason) => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      const answered = Object.keys(answers).length > 0;
      if (answered) {
        log("feedback_survey", { feature, filename, helpful: answers.helpful || null,
                                 answered: answers.answered || null,
                                 comment: answers.comment || "", comment_len: (answers.comment || "").length });
      } else {
        log(reason === "timeout" ? "feedback_survey_timeout" : "feedback_survey_skipped", { feature, filename });
      }
      flush();   // answered or skipped, it reaches the server right away
      if (answered && reason === "done") {
        box.innerHTML = `<h4>Thank you.</h4>`;
        setTimeout(() => box.remove(), 1000);
      } else {
        box.remove();
      }
    };
    const render = () => {
      const body = box.querySelector("#dlc-survey-body");
      if (step < q.steps.length) {
        const [key, text, opts] = q.steps[step];
        body.innerHTML = `<div class="q">${esc(text)}</div><div class="opts">${
          opts.map(([v, label]) => `<button class="btn-ghost" data-sv="${esc(v)}">${esc(label)}</button>`).join("")}</div>`;
        body.querySelectorAll("[data-sv]").forEach((b) => b.addEventListener("click", () => {
          answers[key] = b.dataset.sv;
          step += 1;
          render();
        }));
      } else {
        body.innerHTML = `<div class="q">Optional: what could have made it more helpful?</div>
          <textarea id="dlc-sv-comment" class="text-input" maxlength="300" placeholder="a sentence is plenty"></textarea>
          <div class="dlc-gate-row"><div class="spacer"></div><button id="dlc-sv-done" class="btn">Done</button></div>`;
        body.querySelector("#dlc-sv-done").addEventListener("click", () => {
          answers.comment = (body.querySelector("#dlc-sv-comment").value || "").trim().slice(0, 300);
          finish("done");
        });
      }
    };
    box.querySelector("#dlc-survey-x").addEventListener("click", () => finish("skip"));
    timer = setTimeout(() => finish("timeout"), SURVEY_TIMEOUT_MS);
    render();
  };

  /* ---------------------------------------------------------- boot */
  document.addEventListener("DOMContentLoaded", async () => {
    try { await courseServerGate(); } catch {}
    try { await consentGate(); } catch {}
    try { await renderResearchCard(); } catch {}
    watchSettingsSave();
    try { window.__dlcFirstRunDone(); } catch {}
  });
})();
