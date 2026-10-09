// <ada-keys-card> — member key admin: Issued | Pending | Revoked.
//
// Vanilla web component (the ada-*-card standard, docs/kb/
// ada-member-invite.md §Component standard): no build, no framework — the
// same file serves ada-pi-pwa (/keys.html), an HA sidebar panel, and CMS
// embeds unchanged. Target matrix: iPhone 15 Safari + Add-to-Home-Screen.
//
// Auth: the caller's admin key from localStorage "ada_api_key" (shared
// with the voice PWA), a ?api_key= URL param, or the session cookie.
// api-base attribute overrides the API root (default: the mount base of
// the page hosting the card).
//
//   <ada-keys-card></ada-keys-card>
//   <ada-keys-card api-base="https://ada.example.com"></ada-keys-card>

class AdaKeysCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._poll = null;
  }

  connectedCallback() {
    this._render();
    this.refresh();
    // Light polling while pending invites exist so a member's claim shows
    // up without a manual refresh.
    this._poll = setInterval(() => this.refresh(true), 15000);
  }

  disconnectedCallback() { clearInterval(this._poll); }

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

  async refresh(quiet) {
    try {
      const d = await this._fetch("/api/auth/keys");
      this._data = d;
      this._render();
    } catch (e) {
      if (!quiet) this._error(e.message);
    }
  }

  _error(msg) {
    const el = this.shadowRoot.getElementById("err");
    if (el) el.textContent = msg || "";
  }

  async _act(fn, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    this._error("");
    try { await fn(); await this.refresh(); }
    catch (e) { this._error(e.message); }
  }

  async _createInvite() {
    const nameEl = this.shadowRoot.getElementById("new-name");
    const personEl = this.shadowRoot.getElementById("new-person");
    const name = (nameEl.value || "").trim();
    if (!name) { this._error("name required — e.g. user-kk"); return; }
    const body = { name, person: (personEl.value || "").trim() || undefined };
    this._error("");
    try {
      const d = await this._fetch("/api/auth/invites", {
        method: "POST", body: JSON.stringify(body),
      });
      nameEl.value = ""; personEl.value = "";
      const url = location.origin + this._apiBase() + d.invite_url;
      try { await navigator.clipboard.writeText(url); } catch (_) {}
      this._lastLink = url;
      await this.refresh();
    } catch (e) { this._error(e.message); }
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  _row(name, d) {
    const esc = this._esc;
    const person = d.person ? ` <span class="person">${esc(d.person)}</span>` : "";
    const hp = d.ha_person ? ` <span class="meta">${esc(d.ha_person)}</span>` : "";
    const claimed = d.claimed ? ' <span class="badge claimed">claimed</span>' : "";
    const dev = d.device && d.device !== "*"
      ? ` <span class="meta">dev:${esc(String(d.device).slice(0, 8))}</span>`
      : d.device === "*" ? ' <span class="meta">shared</span>' : "";
    const apps = d.apps ? ` <span class="meta">${esc(d.apps.join("+"))}</span>` : "";
    let actions = "";
    if (d.status === "pending") {
      actions = `<button class="b ok" data-act="approve" data-name="${esc(name)}">Approve</button>
                 <button class="b warn" data-act="reject" data-name="${esc(name)}">Reject</button>`;
    } else if (d.status === "issued") {
      actions = `<button class="b" data-act="repair" data-name="${esc(name)}">Re-pair</button>
                 <button class="b warn" data-act="revoke" data-name="${esc(name)}">Revoke</button>`;
    } else {
      actions = `<span class="meta">revoked ${esc(d.revoked_at || "")}</span>`;
    }
    return `<div class="row"><div class="id"><b>${esc(name)}</b>${person}${hp}${claimed}${dev}${apps}</div>
            <div class="acts">${actions}</div></div>`;
  }

  _section(title, cls, names, details, empty) {
    const rows = names.length
      ? names.map(n => this._row(n, details[n] || { status: "" })).join("")
      : `<div class="empty">${empty}</div>`;
    return `<div class="sec ${cls}"><h2>${title} <span class="count">${names.length}</span></h2>${rows}</div>`;
  }

  _render() {
    const d = this._data || {};
    const details = d.details || {};
    const link = this._lastLink
      ? `<div class="invite-link">invite link (copied): <code>${this._esc(link)}</code></div>` : "";
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: #c9fbff; font: 15px/1.45 system-ui, sans-serif; }
        .card { border: 1px solid #0d4a63; border-radius: 14px; background: #04141c;
                padding: 18px; max-width: 720px; }
        h1 { margin: 0 0 12px; color: #aef8ff; font-size: 20px; }
        h2 { margin: 16px 0 8px; color: #75bdc5; font-size: 13px; font-weight: 700;
             text-transform: uppercase; letter-spacing: .06em; }
        .count { color: #ffd97a; }
        .row { display: flex; align-items: center; justify-content: space-between;
               gap: 10px; padding: 8px 10px; border: 1px solid #0d3040;
               border-radius: 9px; margin: 6px 0; background: rgba(5,35,48,.5); }
        .id { min-width: 0; overflow-wrap: anywhere; }
        .person { color: #ffd97a; }
        .meta { color: #5d92a0; font-size: 12.5px; }
        .badge.claimed { color: #04141c; background: #ffd97a; border-radius: 6px;
                         padding: 1px 7px; font-size: 11.5px; font-weight: 700; }
        .acts { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
        .b { padding: 8px 13px; border: 1px solid #087ef2; border-radius: 8px;
             background: rgba(5,35,48,.9); color: #aef8ff; cursor: pointer;
             font: 600 14px system-ui, sans-serif; }
        .b.ok { border-color: #2a6a55; color: #8affc9; }
        .b.warn { border-color: #6a2a2a; color: #ff9a8a; }
        .empty { color: #3d6a78; font-size: 13.5px; padding: 4px 2px; }
        .new { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
        .new input { flex: 1; min-width: 130px; padding: 9px 11px;
                     border: 1px solid #087ef2; border-radius: 8px;
                     background: rgba(5,35,48,.9); color: #c9fbff;
                     font: 15px system-ui, sans-serif; }
        #err { min-height: 20px; color: #ff9a8a; font-size: 13.5px; margin-top: 8px; }
        .invite-link { color: #8affc9; font-size: 13px; margin-top: 8px;
                       overflow-wrap: anywhere; }
        .invite-link code { color: #aef8ff; }
      </style>
      <div class="card">
        <h1>Ada keys</h1>
        ${this._section("Pending", "pending", d.pending || [], details, "no invites waiting")}
        ${this._section("Issued", "issued", d.issued || [], details, "no live member keys")}
        ${this._section("Revoked", "revoked", d.revoked || [], details, "nothing revoked")}
        <div class="new">
          <input id="new-name" placeholder="key name (user-kk)" autocomplete="off">
          <input id="new-person" placeholder="person label (KK)" autocomplete="off">
          <button class="b" id="mint">Mint invite</button>
        </div>
        ${link}
        <div id="err" role="alert"></div>
      </div>`;

    this.shadowRoot.getElementById("mint")
      .addEventListener("click", () => this._createInvite());
    this.shadowRoot.querySelectorAll("[data-act]").forEach(b =>
      b.addEventListener("click", () => {
        const name = b.dataset.name;
        switch (b.dataset.act) {
          case "approve":
            this._act(() => this._fetch(`/api/auth/keys/${name}/approve`, { method: "POST" }));
            break;
          case "reject":
            this._act(() => this._fetch(`/api/auth/keys/${name}/reject`, { method: "POST" }),
              `Reject invite for ${name}? The link dies.`);
            break;
          case "revoke":
            this._act(() => this._fetch(`/api/auth/keys/${name}`, { method: "DELETE" }),
              `Revoke ${name}? Their sessions die immediately.`);
            break;
          case "repair":
            this._act(async () => {
              const d = await this._fetch(`/api/auth/invites/${name}`, {
                method: "POST", body: "{}",
              });
              const url = d.redeem_url
                ? location.origin + this._apiBase() + d.redeem_url
                : d.invite_url ? location.origin + this._apiBase() + d.invite_url : "";
              if (url) {
                try { await navigator.clipboard.writeText(url); } catch (_) {}
                this._lastLink = url;
              }
            });
            break;
        }
      }));
  }
}

customElements.define("ada-keys-card", AdaKeysCard);
