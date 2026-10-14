// <ada-webchat-card> — LINE-style member webchat for the boards WP site.
//
// Vanilla web component (the ada-*-card standard, docs/kb/
// ada-member-invite.md §Component standard): no build, no framework —
// served from the lane's PWA static root and embedded cross-origin in a
// private WP page under /chat/ (card webchat-lane-boards):
//
//   <script src="https://LANE/cards/ada-webchat-card.js"></script>
//   <ada-webchat-card lane="https://LANE" member="kk"></ada-webchat-card>
//
// Transport: ws(s)://LANE/ws?api_key=<member key>&channel=web
//            &chat_id=web-<member>&mode=text — the REST text provider
//            (same contract as the relays): {type:"text"} out,
//            assistant_transcript_delta / tool_result / chat_push in.
// WebSockets need no CORS; the one cross-origin fetch (invite redeem)
// is gated by ADA_WEBCHAT_ORIGINS on the lane.
//
// Member auth reuses the invite flow: paste the /i/<token> link and the
// card redeems it against the lane, storing the member key + a device id
// in localStorage on the boards origin. A raw key paste also works.
// Turns journal channel=web into the unified voice+chat history.

class AdaWebchatCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._ws = null;
    this._stream = null;      // in-flight assistant bubble {el, body}
    this._retry = 0;
    this._retryTimer = null;
    this._denied = false;
  }

  connectedCallback() { this._render(); this._boot(); }

  disconnectedCallback() {
    this._close();
    if (this._retryTimer) clearTimeout(this._retryTimer);
  }

  // -- config / storage ----------------------------------------------------

  _lane() {
    return (this.getAttribute("lane") || "").replace(/\/+$/, "");
  }

  _member() {
    const m = (this.getAttribute("member") || "web")
      .toLowerCase().replace(/[^a-z0-9-]/g, "");
    return m || "web";
  }

  _chatId() { return `web-${this._member()}`; }

  _store() {
    try {
      const d = JSON.parse(localStorage.getItem("ada_webchat") || "{}");
      return (d && d.lane === this._lane()) ? d : {};
    } catch (_) { return {}; }
  }

  _save(patch) {
    const d = { ...this._store(), lane: this._lane(), ...patch };
    localStorage.setItem("ada_webchat", JSON.stringify(d));
    return d;
  }

  _deviceId() {
    let d = this._store();
    if (!d.device_id) d = this._save({
      device_id: (crypto.randomUUID ? crypto.randomUUID()
                  : Date.now() + "-" + Math.random()).replace(/-/g, ""),
    });
    return d.device_id;
  }

  _apiKey() { return this._store().key || ""; }

  // -- boot / setup gate ---------------------------------------------------

  _boot() {
    // ?key= / #key= hand-off (e.g. a link the operator sends).
    const p = new URLSearchParams(location.search).get("key")
      || new URLSearchParams(location.hash.slice(1)).get("key");
    if (p) this._save({ key: p });
    if (this._apiKey()) { this._connect(); return; }
    this._showSetup();
  }

  _showSetup(msg) {
    this.shadowRoot.getElementById("wc-setup").hidden = false;
    this.shadowRoot.getElementById("wc-main").hidden = true;
    if (msg) this._setupMsg(msg);
  }

  _setupMsg(t) {
    this.shadowRoot.getElementById("wc-setup-msg").textContent = t || "";
  }

  async _setupSubmit() {
    const raw = this.shadowRoot.getElementById("wc-invite").value.trim();
    if (!raw) { this._setupMsg("Paste your invite link or key."); return; }
    this._setupMsg("");
    // Raw member key (ada-...) pasted directly.
    if (/^ada-[A-Za-z0-9_-]{10,}$/.test(raw)) {
      this._save({ key: raw });
      this._enter();
      return;
    }
    // Invite link: …/i/<token> — redeem against the lane for the key.
    const m = raw.match(/\/i\/([A-Za-z0-9_-]{8,})/) || raw.match(/^([A-Za-z0-9_-]{20,})$/);
    if (!m) { this._setupMsg("That doesn't look like an invite link or key."); return; }
    const btn = this.shadowRoot.getElementById("wc-setup-go");
    btn.disabled = true;
    this._setupMsg("Redeeming invite…");
    try {
      const r = await fetch(`${this._lane()}/api/invite/${m[1]}/redeem`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: this._deviceId() }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.status === "pending") {
        this._setupMsg("Invite received — waiting for the owner to approve. Try again after they do.");
      } else if (r.ok && d.api_key) {
        this._save({ key: d.api_key });
        this._enter();
      } else {
        this._setupMsg(d.detail || `Redeem failed (${r.status})`);
      }
    } catch (e) {
      this._setupMsg(`Redeem failed: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  }

  _enter() {
    this.shadowRoot.getElementById("wc-setup").hidden = true;
    this.shadowRoot.getElementById("wc-main").hidden = false;
    this._connect();
  }

  // -- ws ------------------------------------------------------------------

  _close() {
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
  }

  _connect() {
    this._close();
    const lane = this._lane();
    const key = this._apiKey();
    if (!lane || !key) { this._showSetup(); return; }
    this._status("connecting…");
    const scheme = lane.startsWith("https:") ? "wss" : "ws";
    const host = lane.replace(/^https?:\/\//, "");
    const url = `${scheme}://${host}/ws`
      + `?api_key=${encodeURIComponent(key)}`
      + `&channel=web&chat_id=${encodeURIComponent(this._chatId())}`
      + `&mode=text&device_id=${encodeURIComponent(this._deviceId())}`;
    const ws = new WebSocket(url);
    this._ws = ws;
    ws.onmessage = (m) => {
      if (typeof m.data !== "string") return;
      try { this._onEvent(JSON.parse(m.data)); } catch (_) {}
    };
    ws.onclose = (e) => {
      if (this._ws !== ws) return;
      this._ws = null;
      this._finishStream();
      this._setComposer(false);
      if (e.code === 4401 || e.code === 4403) {
        this._denied = true;
        this._status("access denied — check your invite/key");
        this._showSetup("Access denied — the key was rejected. Re-paste your invite link or key.");
        return;
      }
      this._status("disconnected — retrying…");
      this._retryTimer = setTimeout(() => this._connect(),
        Math.min(30000, 2000 * ++this._retry));
    };
    ws.onerror = () => this._status("connection error");
  }

  _onEvent(ev) {
    switch (ev.type) {
      case "ready":
        this._retry = 0;
        this._status("");
        this._setComposer(true);
        this.shadowRoot.getElementById("wc-input").focus();
        break;
      case "response_started":
        this._finishStream();
        this._typing(true);
        break;
      case "assistant_transcript_delta":
        this._typing(false);
        if (!this._stream)
          this._stream = this._bubble("ada", "", { streaming: true });
        this._stream.body.textContent += ev.text || "";
        this._scroll();
        break;
      case "response_completed":
      case "response_interrupted":
        this._typing(false);
        this._finishStream();
        break;
      case "tool_call":
        this._system(`⚙ ${ev.name || "tool"}`);
        break;
      case "tool_result":
        this._mediaFrom(ev.result).forEach(u => this._media(u));
        break;
      case "chat_push":
        // Outbound push (chat_send channel=web) — a parked or live
        // proactive message, rendered as an Ada bubble.
        this._pushBubble(ev);
        break;
      case "channel_activity":
        // A turn on one of the member's OTHER surfaces (voice, LINE, TG).
        this._channelLine(ev);
        break;
      case "notify_voice":
      case "bark":
        this._system(ev.text || "notification");
        break;
      case "error":
        this._system(`error: ${ev.message || "unknown"}`, true);
        break;
    }
  }

  // -- rendering -----------------------------------------------------------

  _log() { return this.shadowRoot.getElementById("wc-log"); }
  _scroll() { const l = this._log(); l.scrollTop = l.scrollHeight; }

  _bubble(kind, text, opts = {}) {
    const row = document.createElement("div");
    row.className = `row ${kind}${opts.streaming ? " streaming" : ""}`;
    if (kind === "ada") {
      const av = document.createElement("div");
      av.className = "avatar";
      av.textContent = "A";
      row.append(av);
    }
    const b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;
    row.append(b);
    this._log().append(row);
    this._scroll();
    return { el: row, body: b };
  }

  _finishStream() {
    if (this._stream) {
      this._stream.el.classList.remove("streaming");
      this._stream = null;
    }
  }

  _system(text, isErr) {
    const el = document.createElement("div");
    el.className = `sys${isErr ? " err" : ""}`;
    el.textContent = text;
    this._log().append(el);
    this._scroll();
  }

  _typing(on) {
    this.shadowRoot.getElementById("wc-typing").textContent =
      on ? "Ada is typing…" : "";
    if (on) this._scroll();
  }

  _channelLine(ev) {
    const el = document.createElement("div");
    el.className = "sys chan";
    el.textContent = `[${ev.via || "?"}] ${ev.role === "assistant" ? "Ada" : "You"}: ${ev.text || ""}`;
    this._log().append(el);
    this._scroll();
  }

  // Recursively find image/video URLs in a tool result (cast_url,
  // image_url, video_url, items[].baseUrl, …) — shallow, capped.
  _mediaFrom(obj, depth = 0, out = []) {
    if (!obj || depth > 4 || out.length >= 6 || typeof obj !== "object") return out;
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === "string" && /^https?:\/\//.test(v)
          && /image_url|video_url|cast_url|media_url|baseUrl|preview/i.test(k)) {
        out.push({ url: v, video: /video|\.mp4|baseUrl.*dv/i.test(k + v) });
      } else if (v && typeof v === "object") {
        this._mediaFrom(v, depth + 1, out);
      }
    }
    return out;
  }

  _media(m) {
    const row = this._bubble("ada", "");
    const tag = document.createElement(m.video ? "video" : "img");
    tag.className = "media";
    tag.src = m.url;
    if (m.video) { tag.controls = true; tag.muted = true; tag.playsInline = true; }
    else { tag.loading = "lazy"; tag.alt = ""; }
    row.body.append(tag);
    this._scroll();
  }

  _pushBubble(ev) {
    const { el, body } = this._bubble("ada", ev.text || "");
    for (const u of [{ url: ev.image_url }, { url: ev.video_url, video: true }]) {
      if (!u.url) continue;
      const tag = document.createElement(u.video ? "video" : "img");
      tag.className = "media";
      tag.src = u.url;
      if (u.video) { tag.controls = true; tag.muted = true; tag.playsInline = true; }
      body.append(tag);
    }
    if (!body.textContent && !body.children.length) el.remove();
    this._scroll();
  }

  _status(t) {
    this.shadowRoot.getElementById("wc-status").textContent = t;
  }

  _setComposer(on) {
    this.shadowRoot.getElementById("wc-input").disabled = !on;
    this.shadowRoot.getElementById("wc-send").disabled = !on;
  }

  _send() {
    const input = this.shadowRoot.getElementById("wc-input");
    const text = input.value.trim();
    if (!text || !this._ws) return;
    this._ws.send(JSON.stringify({ type: "text", text }));
    this._bubble("mine", text);
    input.value = "";
  }

  _render() {
    const member = this._member();
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; height: 100%; color: #1a1a1a;
                font: 15px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif; }
        .wrap { display: flex; flex-direction: column; height: 100%;
                min-height: 320px; background: #8ca3b8;
                border-radius: 12px; overflow: hidden; }
        .head { display: flex; align-items: center; gap: 8px;
                padding: 10px 14px; background: #2c3e50; color: #fff; }
        .head .name { font-weight: 700; }
        #wc-status { margin-left: auto; font-size: 12px; opacity: .75; }
        #wc-log { flex: 1; overflow-y: auto; padding: 14px 12px;
                  display: flex; flex-direction: column; gap: 6px; }
        .row { display: flex; align-items: flex-end; gap: 7px; max-width: 100%; }
        .row.mine { justify-content: flex-end; }
        .avatar { flex: none; width: 30px; height: 30px; border-radius: 50%;
                  background: #2c3e50; color: #fff; display: grid;
                  place-items: center; font-size: 14px; font-weight: 700; }
        .bubble { max-width: 78%; padding: 8px 12px; border-radius: 14px;
                  background: #fff; white-space: pre-wrap;
                  overflow-wrap: anywhere; box-shadow: 0 1px 1px rgba(0,0,0,.12); }
        .row.mine .bubble { background: #90e05a; border-bottom-right-radius: 4px; }
        .row.ada .bubble { border-bottom-left-radius: 4px; }
        .row.streaming .bubble { opacity: .8; }
        .media { display: block; max-width: min(320px, 70vw); max-height: 320px;
                 margin-top: 6px; border-radius: 10px; }
        .bubble > .media:first-child { margin-top: 0; }
        .sys { align-self: center; max-width: 92%; color: #4a6076;
               font-size: 12px; text-align: center; }
        .sys.err { color: #b03030; }
        .sys.chan { opacity: .7; font-style: italic; }
        #wc-typing { color: #4a6076; font-size: 12px; font-style: italic;
                     padding: 2px 14px 6px; min-height: 16px; }
        .composer { display: flex; gap: 8px; padding: 10px;
                    background: #f4f6f8; }
        .composer input { flex: 1; min-height: 40px; padding: 8px 12px;
                          border: 1px solid #c4ccd4; border-radius: 20px;
                          font: 15px system-ui, sans-serif; }
        .composer input:focus { outline: 2px solid #4a90d9; }
        #wc-send { min-height: 40px; padding: 8px 18px; border: 0;
                   border-radius: 20px; background: #06c755; color: #fff;
                   font: 700 14px system-ui, sans-serif; cursor: pointer; }
        #wc-send:disabled { background: #a9c7b4; }
        /* setup gate */
        .setup { display: grid; place-items: center; height: 100%;
                 min-height: 320px; background: #8ca3b8; border-radius: 12px; }
        .setup-card { width: min(380px, 88%); padding: 22px;
                      background: #fff; border-radius: 14px;
                      box-shadow: 0 2px 10px rgba(0,0,0,.2); }
        .setup-card h2 { margin: 0 0 6px; font-size: 18px; }
        .setup-card p { margin: 0 0 12px; color: #556; font-size: 13px; }
        .setup-card input { width: 100%; box-sizing: border-box;
                            min-height: 42px; padding: 9px 12px;
                            border: 1px solid #c4ccd4; border-radius: 9px;
                            font: 14px system-ui, sans-serif; }
        #wc-setup-go { width: 100%; min-height: 42px; margin-top: 10px;
                       border: 0; border-radius: 9px; background: #06c755;
                       color: #fff; font: 700 15px system-ui, sans-serif;
                       cursor: pointer; }
        #wc-setup-msg { min-height: 18px; margin-top: 8px; color: #b03030;
                        font-size: 13px; }
        [hidden] { display: none !important; }
      </style>
      <div class="setup" id="wc-setup">
        <div class="setup-card">
          <h2>Ada chat</h2>
          <p>Paste your Ada invite link (…/i/&lt;token&gt;) or member key
            to connect this page to your lane.</p>
          <input id="wc-invite" type="text" autocomplete="off"
                 placeholder="https://…/i/…  or  ada-…">
          <button id="wc-setup-go">Connect</button>
          <div id="wc-setup-msg" role="alert"></div>
        </div>
      </div>
      <div class="wrap" id="wc-main" hidden>
        <div class="head">
          <div class="avatar">A</div>
          <div class="name">Ada · ${member}</div>
          <div id="wc-status"></div>
        </div>
        <div id="wc-log"></div>
        <div id="wc-typing"></div>
        <div class="composer">
          <input id="wc-input" type="text" placeholder="Message…"
                 autocomplete="off" disabled>
          <button id="wc-send" disabled>Send</button>
        </div>
      </div>`;
    this.shadowRoot.getElementById("wc-send")
      .addEventListener("click", () => this._send());
    this.shadowRoot.getElementById("wc-input")
      .addEventListener("keydown", (e) => { if (e.key === "Enter") this._send(); });
    this.shadowRoot.getElementById("wc-setup-go")
      .addEventListener("click", () => this._setupSubmit());
    this.shadowRoot.getElementById("wc-invite")
      .addEventListener("keydown", (e) => { if (e.key === "Enter") this._setupSubmit(); });
  }
}

customElements.define("ada-webchat-card", AdaWebchatCard);
