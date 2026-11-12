window.dlcFirstRun = new Promise((resolve) => { window.__dlcFirstRunDone = resolve; });

(function () {
  function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  async function getJson(url) { const r = await fetch(url); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }
  async function postJson(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let b = null; try { b = await r.json(); } catch {}
    return { ok: r.ok, status: r.status, body: b };
  }

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
      gate.querySelector("#dlc-connect-skip").addEventListener("click", done);
      gate.querySelector("#dlc-connect-go").addEventListener("click", async () => {
        const url = (gate.querySelector("#dlc-connect-url").value || "").trim();
        const token = (gate.querySelector("#dlc-connect-token").value || "").trim();
        if (!url) { msg.textContent = "The course server URL is required."; msg.className = "dlc-gate-msg err"; return; }
        msg.textContent = "Connecting…"; msg.className = "dlc-gate-msg";
        const r = await postJson("/api/config/proxy", { url, token });
        if (!r.ok || !r.body || !r.body.ok) { msg.textContent = "Save failed."; msg.className = "dlc-gate-msg err"; return; }
        const [text, bad] = VERIFY_TEXT[r.body.verify] || ["Saved.", false];
        msg.textContent = text; msg.className = "dlc-gate-msg " + (bad ? "err" : "ok");
        if (typeof renderProxyState === "function") renderProxyState();
        if (typeof refreshKeyChip === "function") refreshKeyChip();
        if (r.body.verify === "accepted") setTimeout(done, 700);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    try { await courseServerGate(); } catch {}
    try { window.__dlcFirstRunDone(); } catch {}
  });
})();
