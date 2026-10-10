// <ada-chat-card> — text chat card + secret-drop modal.
//
// Vanilla web component (the ada-*-card standard, docs/kb/
// ada-member-invite.md §Component standard): no build, no framework — the
// same file serves ada-pi-pwa (/chat.html), an HA sidebar panel, and CMS
// embeds unchanged. Target matrix: iPhone 15 Safari + Add-to-Home-Screen.
//
// Chat protocol mirrors pwa/guest/guest.js: {type:"text", text} over /ws,
// assistant replies stream as assistant_transcript_delta events.
//
// The .acc-secret button opens the secret-drop modal (card
// ada-secret-drop): target host + secret name + value post to
// POST /api/secret-drop. The value leaves this page exactly once, in the
// POST body — it is never rendered, logged, or kept in the DOM after
// submit. The UI shows only the receipt (path + sha256 first8).
//
// Auth: the caller's paired key from localStorage "ada_api_key" (shared
// with the voice PWA), a ?api_key= URL param, or the session cookie.
// api-base attribute overrides the API root (default: the mount base of
// the page hosting the card).
//
//   <ada-chat-card></ada-chat-card>
//   <ada-chat-card api-base="https://ada.example.com"></ada-chat-card>

const ACC_HOSTS = ["idc03", "tony-dell", "tony-omen", "idc02"];
const ACC_NAME_RE = /^[a-z0-9._-]{1,64}$/;

class AdaChatCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._ws = null;
    this._assistantEntry = null;
  }

  connectedCallback() { this._render(); }

  disconnectedCallback() {
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
  }

  _apiBase() {
    const attr = this.getAttribute("api-base");
    if (attr) return attr.replace(/\/+$/, "");
    return location.pathname.replace(/\/[^/]*$/, "") || "";
  }

  _apiKey() {
    const p = new URLSearchParams(location.search).get("api_key");
    if (p) { localStorage.setItem("ada_api_key", p); return p; }
    return localStorage.getItem("ada_api_key") || "";
  }

  _deviceId() {
    let id = localStorage.getItem("ada_device_id");
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random()).replace(/-/g, "");
      localStorage.setItem("ada_device_id", id);
    }
    return id;
  }

  async _fetch(path, opts = {}) {
    const r = await fetch(this._apiBase() + path, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        "X-Api-Key": this._apiKey(),
        "X-Device-Id": this._deviceId(),
        ...(opts.headers || {}),
      },
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
    return d;
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // -- chat ---------------------------------------------------------------

  _logEl() { return this.shadowRoot.getElementById("acc-log"); }

  _addMsg(who, text, cls) {
    const el = document.createElement("div");
    el.className = `msg ${cls}`;
    const label = document.createElement("span");
    label.className = "who";
    label.textContent = who;
    const body = document.createElement("div");
    body.textContent = text;
    el.append(label, body);
    const log = this._logEl();
    log.append(el);
    log.scrollTop = log.scrollHeight;
    return { el, body };
  }

  _systemLine(text) {
    const el = document.createElement("div");
    el.className = "msg system";
    el.textContent = text;
    const log = this._logEl();
    log.append(el);
    log.scrollTop = log.scrollHeight;
  }

  // Machine voice — flat speechSynthesis announcements (barks, notifies).
  // The text chat renders them as a bracketed '[machine voice]' line and
  // NEVER auto-plays: the voice only sounds when the user presses ▶.
  _machineVoiceLine(text) {
    const el = document.createElement("div");
    el.className = "msg machine-voice";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "machine-voice-play";
    play.textContent = "▶";
    play.title = "Play machine voice";
    play.addEventListener("click", () => this._speakMachine(text));
    const tag = document.createElement("span");
    tag.className = "mv-tag";
    tag.textContent = "[machine voice] ";
    const body = document.createElement("span");
    body.textContent = text;
    el.append(play, tag, body);
    const log = this._logEl();
    log.append(el);
    log.scrollTop = log.scrollHeight;
  }

  _speakMachine(text) {
    try {
      if (!("speechSynthesis" in window)) return;
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.15; u.pitch = 0.85; u.volume = 0.9;
      const en = speechSynthesis.getVoices()
        .find(v => /^en[-_]US/i.test(v.lang)) || null;
      if (en) u.voice = en;
      speechSynthesis.speak(u);
    } catch (_) {}
  }

  _status(t) { this.shadowRoot.getElementById("acc-status").textContent = t; }

  async _ensureSession() {
    const key = this._apiKey();
    if (!key) return; // auth may be off, or cookie already set
    try {
      await fetch(`${this._apiBase()}/api/auth/session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: key, path: this._apiBase() || "/" }),
      });
    } catch (_) { /* cookie auth optional */ }
  }

  async _connect() {
    if (this._ws) return;
    this._status("Connecting…");
    await this._ensureSession();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const key = this._apiKey();
    // channel=chat marks this as a text-channel session (like the
    // telegram/line relays): text-chat instructions, no voice stack —
    // and its turns carry the [chat] label in the shared voice+chat
    // history so Ada's voice sessions can read them.
    const url = `${scheme}://${location.host}${this._apiBase()}/ws`
      + `?device_id=${encodeURIComponent(this._deviceId())}&channel=chat`
      + (key ? `&api_key=${encodeURIComponent(key)}` : "");
    const ws = new WebSocket(url);
    this._ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onmessage = (m) => {
      if (typeof m.data !== "string") return; // binary frames are audio
      try { this._onEvent(JSON.parse(m.data)); } catch (_) { /* ignore */ }
    };
    ws.onclose = (e) => {
      this._ws = null;
      this._setComposerEnabled(false);
      this._status(e.code === 4401
        ? "Access denied — check your key" : "Disconnected");
      this.shadowRoot.getElementById("acc-connect").textContent = "Connect";
    };
    ws.onerror = () => this._status("Connection error");
    this.shadowRoot.getElementById("acc-connect").textContent = "Reconnect";
  }

  _onEvent(ev) {
    switch (ev.type) {
      case "ready":
        this._setComposerEnabled(true);
        this._status("Connected");
        break;
      case "assistant_transcript_delta":
        if (!this._assistantEntry)
          this._assistantEntry = this._addMsg("Ada", "", "ada streaming");
        this._assistantEntry.body.textContent += ev.text;
        this._logEl().scrollTop = this._logEl().scrollHeight;
        break;
      case "response_started":
      case "response_completed":
        if (this._assistantEntry) {
          this._assistantEntry.el.classList.remove("streaming");
          this._assistantEntry = null;
        }
        break;
      case "user_transcript":
        this._addMsg("You (voice)", ev.text, "mine");
        break;
      case "channel_activity":
        // Unified voice+chat history: a turn that happened on one of the
        // user's OTHER live sessions (voice, telegram, line). Rendered
        // dimmed with the surface label — same timeline, not a reply.
        this._addMsg(
          `${ev.role === "assistant" ? "Ada" : "You"} [${ev.via || "?"}]`,
          ev.text || "", "channel");
        break;
      case "bark":
      case "notify_voice":
        this._machineVoiceLine(ev.text || "Notification.");
        break;
      case "error":
        this._systemLine(`Error: ${ev.message}`);
        break;
    }
  }

  _setComposerEnabled(on) {
    this.shadowRoot.getElementById("acc-input").disabled = !on;
    this.shadowRoot.getElementById("acc-send").disabled = !on;
  }

  _send() {
    const input = this.shadowRoot.getElementById("acc-input");
    const text = input.value.trim();
    if (!text || !this._ws) return;
    this._ws.send(JSON.stringify({ type: "text", text }));
    this._addMsg("You", text, "mine");
    input.value = "";
  }

  // -- secret drop ----------------------------------------------------------

  _openSecret() {
    this._secretError("");
    this._secretReceipt(null);
    this.shadowRoot.getElementById("acc-modal").hidden = false;
    this.shadowRoot.getElementById("acc-secret-name").focus();
  }

  _closeSecret() {
    const m = this.shadowRoot.getElementById("acc-modal");
    m.hidden = true;
    // Drop field contents when the modal closes — a cancelled drop must
    // not leave a live secret sitting in the DOM.
    this.shadowRoot.getElementById("acc-secret-value").value = "";
  }

  _secretError(msg) {
    this.shadowRoot.getElementById("acc-secret-err").textContent = msg || "";
  }

  _secretReceipt(receipt) {
    const el = this.shadowRoot.getElementById("acc-secret-receipt");
    if (!receipt) { el.textContent = ""; el.hidden = true; return; }
    el.hidden = false;
    el.textContent = `dropped → ${receipt.path}  (sha256:${receipt.sha8})`;
  }

  async _submitSecret() {
    const hostEl = this.shadowRoot.getElementById("acc-secret-host");
    const nameEl = this.shadowRoot.getElementById("acc-secret-name");
    const valueEl = this.shadowRoot.getElementById("acc-secret-value");
    const name = nameEl.value.trim();
    if (!ACC_NAME_RE.test(name)) {
      this._secretError("name must match [a-z0-9._-] (1-64 chars)");
      return;
    }
    const value = valueEl.value;
    if (!value) { this._secretError("value required"); return; }
    this._secretError("");
    const btn = this.shadowRoot.getElementById("acc-secret-submit");
    btn.disabled = true;
    try {
      const d = await this._fetch("/api/secret-drop", {
        method: "POST",
        body: JSON.stringify({ host: hostEl.value, name, value }),
      });
      valueEl.value = ""; // clear first — never re-render the value
      this._secretReceipt(d);
    } catch (e) {
      this._secretError(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: #c9fbff; font: 15px/1.45 system-ui, sans-serif; }
        .card { border: 1px solid #0d4a63; border-radius: 14px; background: #04141c;
                padding: 18px; max-width: 720px; display: flex; flex-direction: column;
                min-height: 420px; }
        .head { display: flex; align-items: center; justify-content: space-between;
                gap: 10px; margin-bottom: 10px; }
        h1 { margin: 0; color: #aef8ff; font-size: 20px; }
        .toolbar { display: flex; gap: 8px; }
        #acc-status { color: #75bdc5; font-size: 13px; min-height: 18px; }
        #acc-log { flex: 1; overflow-y: auto; min-height: 220px; max-height: 50vh;
                   padding: 10px; border-radius: 10px; background: rgba(0,12,19,.85);
                   display: flex; flex-direction: column; gap: 6px; }
        .msg { max-width: 82%; padding: 8px 12px; border-radius: 12px;
               white-space: pre-wrap; overflow-wrap: anywhere; }
        .msg .who { display: block; font-size: 11px; opacity: .65; margin-bottom: 2px; }
        .msg.mine { align-self: flex-end; background: rgba(8,126,242,.22);
                    border: 1px solid rgba(8,126,242,.5); }
        .msg.ada { align-self: flex-start; background: rgba(13,74,99,.55);
                   border: 1px solid #0d4a63; }
        .msg.system { align-self: center; background: none; color: #ff9a8a;
                      font-size: 12px; padding: 2px; }
        .msg.machine-voice { align-self: center; background: none;
                             color: #75bdc5; font-size: 12.5px; padding: 2px; }
        .msg.machine-voice .mv-tag { color: #f2a008; font-weight: 600; }
        .machine-voice-play { min-height: 26px; margin-right: 7px; padding: 1px 9px;
                              border: 1px solid #0d4a63; border-radius: 7px;
                              background: rgba(5,35,48,.9); color: #aef8ff;
                              font: 12px system-ui, sans-serif; cursor: pointer;
                              vertical-align: 1px; }
        .msg.channel { align-self: center; background: none; max-width: 92%;
                       color: #9adfe7; font-size: 12.5px; padding: 2px;
                       opacity: .75; }
        .msg.channel .who { color: #f2a008; }
        .msg.streaming { opacity: .75; }
        .composer { display: flex; gap: 8px; margin-top: 10px; }
        .composer input { flex: 1; min-height: 44px; padding: 10px 14px;
                          border: 1px solid #087ef2; border-radius: 9px;
                          background: rgba(5,35,48,.9); color: #c9fbff;
                          font: 15px system-ui, sans-serif; }
        .b { min-height: 40px; padding: 8px 14px; border: 1px solid #087ef2;
             border-radius: 8px; background: rgba(5,35,48,.9); color: #aef8ff;
             cursor: pointer; font: 600 14px system-ui, sans-serif; }
        .b:disabled { opacity: .4; }
        .acc-secret { border-color: #2a6a55; color: #8affc9; }
        #acc-modal { position: fixed; inset: 0; display: grid; place-items: center;
                     background: rgba(0,8,12,.92); z-index: 300; }
        #acc-modal[hidden] { display: none; }
        .acc-modal-card { width: min(380px, 90vw); padding: 24px; border: 1px solid #0d4a63;
                          border-radius: 14px; background: #04141c; }
        .acc-modal-card h2 { margin: 0 0 4px; color: #aef8ff; font: 700 19px system-ui, sans-serif; }
        .acc-modal-card .hint { margin: 0 0 14px; color: #75bdc5; font: 13px/1.5 system-ui, sans-serif; }
        .acc-modal-card label { display: block; color: #75bdc5; font-size: 12px;
                                font-weight: 700; text-transform: uppercase;
                                letter-spacing: .06em; margin: 10px 0 4px; }
        .acc-modal-card select, .acc-modal-card input {
          width: 100%; box-sizing: border-box; padding: 11px 12px;
          border: 1px solid #087ef2; border-radius: 9px;
          background: rgba(5,35,48,.9); color: #c9fbff;
          font: 15px system-ui, sans-serif; }
        #acc-secret-err { min-height: 20px; color: #ff9a8a; font-size: 13px; margin-top: 8px; }
        #acc-secret-receipt { color: #8affc9; font-size: 13px; margin-top: 8px;
                              overflow-wrap: anywhere; }
        .modal-actions { display: flex; gap: 8px; margin-top: 14px; }
        .modal-actions .b { flex: 1; }
        .b.cancel { border-color: #0d4a63; color: #75bdc5; }
      </style>
      <div class="card">
        <div class="head">
          <h1>Ada chat</h1>
          <div class="toolbar">
            <button class="b acc-secret" id="acc-secret-open"
                    title="Drop a secret onto a fleet host">🔐 Drop secret</button>
            <button class="b" id="acc-connect">Connect</button>
          </div>
        </div>
        <div id="acc-status" aria-live="polite">Tap Connect</div>
        <div id="acc-log"></div>
        <div class="composer">
          <input id="acc-input" type="text" placeholder="Message Ada…"
                 autocomplete="off" disabled>
          <button class="b" id="acc-send" disabled>Send</button>
        </div>
      </div>
      <div id="acc-modal" hidden>
        <div class="acc-modal-card" role="dialog" aria-label="Drop a secret">
          <h2>Drop a secret</h2>
          <p class="hint">Writes ~/.config/secrets/&lt;name&gt; (0600) on the
            target host. The value is never shown or logged — you get back
            a receipt (path + sha256).</p>
          <label for="acc-secret-host">Target host</label>
          <select id="acc-secret-host">
            ${ACC_HOSTS.map(h => `<option value="${h}">${h}</option>`).join("")}
          </select>
          <label for="acc-secret-name">Secret name</label>
          <input id="acc-secret-name" type="text" autocomplete="off"
                 placeholder="e.g. openai-key" maxlength="64">
          <label for="acc-secret-value">Value</label>
          <input id="acc-secret-value" type="password" autocomplete="off"
                 data-lpignore="true" data-1p-ignore data-form-type="other"
                 placeholder="secret value">
          <div id="acc-secret-err" role="alert"></div>
          <div id="acc-secret-receipt" hidden></div>
          <div class="modal-actions">
            <button class="b acc-secret" id="acc-secret-submit">Drop it</button>
            <button class="b cancel" id="acc-secret-cancel">Close</button>
          </div>
        </div>
      </div>`;

    this.shadowRoot.getElementById("acc-connect")
      .addEventListener("click", () => this._connect());
    this.shadowRoot.getElementById("acc-send")
      .addEventListener("click", () => this._send());
    this.shadowRoot.getElementById("acc-input")
      .addEventListener("keydown", (e) => { if (e.key === "Enter") this._send(); });
    this.shadowRoot.getElementById("acc-secret-open")
      .addEventListener("click", () => this._openSecret());
    this.shadowRoot.getElementById("acc-secret-cancel")
      .addEventListener("click", () => this._closeSecret());
    this.shadowRoot.getElementById("acc-secret-submit")
      .addEventListener("click", () => this._submitSecret());
    this.shadowRoot.getElementById("acc-modal")
      .addEventListener("click", (e) => {
        if (e.target.id === "acc-modal") this._closeSecret();
      });
    this.shadowRoot.getElementById("acc-secret-name")
      .addEventListener("keydown", (e) => { if (e.key === "Enter") this._submitSecret(); });
  }
}

customElements.define("ada-chat-card", AdaChatCard);
