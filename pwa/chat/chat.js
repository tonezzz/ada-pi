// Ada text chat — same Gemini Live session as the voice PWA, text turns in,
// transcript deltas + PCM audio out. Shares the device-key auth model.
const OUTPUT_RATE = 24000;

const connectButton = document.querySelector("#connect");
const disconnectButton = document.querySelector("#disconnect");
const voiceToggle = document.querySelector("#voice-toggle");
const statusElement = document.querySelector("#chat-status");
const logElement = document.querySelector("#chat-log");
const inputElement = document.querySelector("#chat-input");
const sendButton = document.querySelector("#chat-send");

let socket = null;
let playbackContext = null;
let playbackNode = null;
let voiceMuted = false;
let assistantEntry = null;   // { el, text } while a response streams
let connectionInProgress = false;

function setStatus(text) {
  if (statusElement) statusElement.textContent = text;
}

function setConnected(connected) {
  if (connectButton) connectButton.disabled = connected || (authRequired && !authed);
  if (disconnectButton) disconnectButton.disabled = !connected;
  if (inputElement) inputElement.disabled = !connected;
  if (sendButton) sendButton.disabled = !connected;
  if (connected) inputElement?.focus();
}

function addMsg(who, text, cls = "") {
  const el = document.createElement("div");
  el.className = `msg ${cls}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("span");
  body.textContent = text;
  el.append(label, body);
  logElement.append(el);
  logElement.scrollTop = logElement.scrollHeight;
  return { el, body };
}

function systemLine(text) {
  const el = document.createElement("div");
  el.className = "msg system";
  el.textContent = text;
  logElement.append(el);
  logElement.scrollTop = logElement.scrollHeight;
}

// --- PCM playback (same worklet as the voice PWA, no meter/face) ---

async function createPlayback() {
  playbackContext = new AudioContext({ sampleRate: OUTPUT_RATE, latencyHint: "interactive" });
  const workletSource = `
    class PCMPlayer extends AudioWorkletProcessor {
      constructor() {
        super();
        this.queue = [];
        this.offset = 0.0;
        this.step = 24000 / sampleRate;
        this.bufferedSamples = 0;
        this.playing = false;
        this.forceStart = false;
        this.port.onmessage = e => {
          if (e.data.type === 'clear') {
            this.queue = []; this.offset = 0; this.bufferedSamples = 0;
            this.playing = false; this.forceStart = false;
          } else if (e.data.type === 'flush') {
            this.forceStart = true;
          } else {
            const chunk = new Int16Array(e.data);
            this.queue.push(chunk); this.bufferedSamples += chunk.length;
          }
        };
      }
      process(inputs, outputs) {
        const out = outputs[0][0]; out.fill(0); let n = 0;
        if (!this.playing) {
          if (!this.bufferedSamples && !this.forceStart) return true;
          this.playing = true;
        }
        while (n < out.length && this.queue.length) {
          const chunk = this.queue[0];
          while (n < out.length && this.offset < chunk.length - 1) {
            const i = Math.floor(this.offset);
            const frac = this.offset - i;
            const s0 = chunk[i] / 32768;
            const s1 = chunk[i + 1] / 32768;
            out[n++] = s0 + (s1 - s0) * frac;
            this.bufferedSamples -= this.step;
            this.offset += this.step;
          }
          if (this.offset >= chunk.length - 1) {
            this.queue.shift();
            this.offset = Math.max(0, this.offset - (chunk.length - 1));
          }
        }
        if (!this.queue.length) { this.playing = false; this.forceStart = false; }
        return true;
      }
    }
    registerProcessor('pcm-player', PCMPlayer);`;
  const url = URL.createObjectURL(new Blob([workletSource], { type: "text/javascript" }));
  await playbackContext.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  playbackNode = new AudioWorkletNode(playbackContext, "pcm-player", { outputChannelCount: [1] });
  playbackNode.connect(playbackContext.destination);
  // AudioContext created outside a user gesture starts suspended and
  // resume() pends until the next tap — do NOT await it or connect() stalls.
  playbackContext.resume().catch(() => {});
  document.body.addEventListener("touchstart", () => playbackContext?.resume().catch(() => {}), { passive: true });
  document.body.addEventListener("click", () => playbackContext?.resume().catch(() => {}));
}

function flushAssistantEntry() {
  if (assistantEntry) {
    assistantEntry.el.classList.remove("streaming");
    assistantEntry = null;
  }
}

function handleControl(event) {
  switch (event.type) {
    case "ready":
      setConnected(true);
      setStatus("Connected — type below");
      break;
    case "speech_started":
      setStatus("Voice input in progress…");
      break;
    case "speech_stopped":
      setStatus(socket ? "Connected — type below" : "Disconnected");
      break;
    case "clear_audio":
      playbackNode?.port.postMessage({ type: "clear" });
      flushAssistantEntry();
      break;
    case "user_transcript":
      addMsg("Voice", event.text, "voice");
      break;
    case "assistant_transcript_delta":
      if (!assistantEntry) assistantEntry = addMsg("Ada", "", "ada streaming");
      assistantEntry.body.textContent += event.text;
      logElement.scrollTop = logElement.scrollHeight;
      break;
    case "response_started":
    case "response_completed":
      flushAssistantEntry();
      if (playbackContext?.state === "suspended") playbackContext.resume().catch(() => {});
      playbackNode?.port.postMessage({ type: "flush" });
      break;
    case "response_interrupted":
      playbackNode?.port.postMessage({ type: "clear" });
      flushAssistantEntry();
      break;
    case "live_reconnecting":
      setStatus("Reconnecting…");
      break;
    case "error":
      systemLine(`Error: ${event.message}`);
      break;
  }
}

async function connect() {
  if (connectionInProgress || socket) return;
  connectionInProgress = true;
  setStatus("Connecting…");
  try {
    await ensureSession();
    await createPlayback();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const key = getApiKey();
    const wsUrl = `${scheme}://${location.host}${chatBasePath()}/ws`
      + `?device_id=${encodeURIComponent(getDeviceId())}`
      + (key ? `&api_key=${encodeURIComponent(key)}` : "");
    socket = new WebSocket(wsUrl);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => setStatus("Connecting to AI…");
    socket.onmessage = (message) => {
      if (typeof message.data === "string") handleControl(JSON.parse(message.data));
      else if (!voiceMuted) playbackNode?.port.postMessage(message.data, [message.data]);
    };
    socket.onerror = () => systemLine("WebSocket error");
    socket.onclose = (event) => {
      if (event.code === 4401) {
        authRequired = true;
        localStorage.removeItem(AUTH_STORAGE_KEY);
        setLocked(true, "This device isn't authorized — enter the API key.");
      }
      disconnect(false);
    };
  } catch (error) {
    console.error(error);
    setStatus(error.message);
    await disconnect(false);
  } finally {
    connectionInProgress = false;
  }
}

async function disconnect(closeSocket = true) {
  if (closeSocket && socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "user disconnect");
  socket = null;
  if (playbackContext) await playbackContext.close().catch(() => {});
  playbackContext = playbackNode = null;
  assistantEntry = null;
  setStatus("Disconnected");
  setConnected(false);
}

function sendText() {
  const text = (inputElement.value || "").trim();
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "text", text }));
  addMsg("You", text, "mine");
  inputElement.value = "";
}

connectButton?.addEventListener("click", connect);
disconnectButton?.addEventListener("click", () => disconnect(true));
sendButton?.addEventListener("click", sendText);
inputElement?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") sendText();
});

voiceToggle?.addEventListener("click", () => {
  voiceMuted = !voiceMuted;
  voiceToggle.textContent = voiceMuted ? "Voice: Off" : "Voice: On";
  voiceToggle.classList.toggle("off", voiceMuted);
  if (voiceMuted) playbackNode?.port.postMessage({ type: "clear" });
});

// --- Auth: API key storage, session cookie, lock UI (subset of app.js) ---

const AUTH_STORAGE_KEY = "ada_api_key";
const DEVICE_STORAGE_KEY = "ada_device_id";
const NAME_STORAGE_KEY = "ada_key_name";

const lockButton = document.querySelector("#lock-toggle");
const unlockOverlay = document.querySelector("#unlock-overlay");
const unlockInput = document.querySelector("#unlock-key");
const unlockError = document.querySelector("#unlock-error");
const unlockHint = document.querySelector("#unlock-hint");
const unlockSubmit = document.querySelector("#unlock-submit");
let authRequired = false;
let authed = true;

function chatBasePath() {
  return location.pathname.replace(/\/chat\/?$/, "") || "/";
}

function getDeviceId() {
  let id = localStorage.getItem(DEVICE_STORAGE_KEY);
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/-/g, "");
    localStorage.setItem(DEVICE_STORAGE_KEY, id);
  }
  return id;
}

function getApiKey() {
  const params = new URLSearchParams(location.search);
  const fromUrl = params.get("api_key");
  if (fromUrl) {
    localStorage.setItem(AUTH_STORAGE_KEY, fromUrl);
    params.delete("api_key");
    const clean = params.toString();
    history.replaceState(null, "", location.pathname + (clean ? `?${clean}` : "") + location.hash);
  }
  return localStorage.getItem(AUTH_STORAGE_KEY) || "";
}

function showUnlock(hint) {
  if (!unlockOverlay) return;
  if (unlockHint) unlockHint.textContent = hint || "Enter the API key to unlock chat.";
  if (unlockError) unlockError.textContent = "";
  if (unlockInput) unlockInput.value = "";
  unlockOverlay.hidden = false;
  unlockInput?.focus();
}

function hideUnlock() {
  if (unlockOverlay) unlockOverlay.hidden = true;
}

function setLocked(locked, hint) {
  authed = !locked;
  if (lockButton) {
    lockButton.hidden = !authRequired;
    lockButton.textContent = locked ? "Unlock" : "Lock";
    lockButton.classList.toggle("locked", locked);
  }
  if (locked) showUnlock(hint);
  else hideUnlock();
  setConnected(Boolean(socket));
}

async function ensureSession() {
  const key = getApiKey();
  if (!key) return false;
  try {
    const resp = await fetch(`${chatBasePath()}/api/auth/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Device-Id": getDeviceId() },
      body: JSON.stringify({ api_key: key, path: chatBasePath(), device_id: getDeviceId() }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.name) localStorage.setItem(NAME_STORAGE_KEY, data.name);
    return resp.ok;
  } catch {
    return false;
  }
}

async function submitUnlock() {
  const key = unlockInput?.value.trim();
  if (!key) return;
  localStorage.setItem(AUTH_STORAGE_KEY, key);
  if (unlockSubmit) unlockSubmit.disabled = true;
  if (unlockError) unlockError.textContent = "";
  try {
    if (await ensureSession()) {
      setLocked(false);
      connect();
    } else {
      localStorage.removeItem(AUTH_STORAGE_KEY);
      if (unlockError) unlockError.textContent = "That key was rejected.";
    }
  } finally {
    if (unlockSubmit) unlockSubmit.disabled = false;
  }
}

async function initAuth() {
  let status = null;
  try {
    const resp = await fetch(`${chatBasePath()}/api/auth/status`, { headers: { "X-Device-Id": getDeviceId() } });
    status = await resp.json();
  } catch {
    return;
  }
  authRequired = !!status.auth_configured;
  if (!authRequired) { connect(); return; }

  let linkRejected = "";
  const urlKey = new URLSearchParams(location.search).get("api_key");
  if (urlKey) {
    // A redeem link just landed — verify it names THIS device before trusting it.
    const who = await fetch(`${chatBasePath()}/api/auth/status`, {
      headers: { "X-Api-Key": urlKey },
    }).then(r => r.json()).catch(() => null);
    const storedName = localStorage.getItem(NAME_STORAGE_KEY);
    if (who?.name && storedName && who.name !== storedName) {
      history.replaceState(null, "", location.pathname + location.hash);
      linkRejected = `That link was issued for "${who.name}" — this device is "${storedName}".`;
    } else if (who?.name) {
      localStorage.setItem(NAME_STORAGE_KEY, who.name);
    }
  }

  if (status.name) localStorage.setItem(NAME_STORAGE_KEY, status.name);
  if (status.authenticated) { setLocked(false); if (linkRejected) setStatus(linkRejected); connect(); return; }
  if (getApiKey()) {
    if (await ensureSession()) { setLocked(false); if (linkRejected) setStatus(linkRejected); connect(); return; }
    localStorage.removeItem(AUTH_STORAGE_KEY);
    setLocked(true, linkRejected || "Saved key was rejected — enter a new one.");
    return;
  }
  setLocked(true, linkRejected || undefined);
}

unlockSubmit?.addEventListener("click", submitUnlock);
unlockInput?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") submitUnlock();
});

// --- Decision check: upload/paste a listing, get a verified verdict ---

const checkToggle = document.querySelector("#check-toggle");
const checkPanel = document.querySelector("#check-panel");
const checkText = document.querySelector("#check-text");
const checkFileBtn = document.querySelector("#check-file");
const checkFileInput = document.querySelector("#check-file-input");
const checkFileName = document.querySelector("#check-file-name");
const checkMode = document.querySelector("#check-mode");
const checkRun = document.querySelector("#check-run");
let checkFile = null;
let checking = false;

checkToggle?.addEventListener("click", () => checkPanel?.classList.toggle("open"));
checkFileBtn?.addEventListener("click", () => checkFileInput?.click());
checkFileInput?.addEventListener("change", () => {
  checkFile = checkFileInput.files?.[0] || null;
  if (checkFileName) checkFileName.textContent = checkFile ? checkFile.name : "no file";
});

function readFileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function checkLine(parent, label, items) {
  if (!items || !items.length) return;
  const section = document.createElement("div");
  section.className = "check-section";
  const head = document.createElement("b");
  head.textContent = label;
  const list = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.append(li);
  }
  section.append(head, list);
  parent.append(section);
}

function renderCheckResult(data) {
  const card = document.createElement("div");
  card.className = "msg check";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = "Check";
  const product = data.product || {};
  const title = document.createElement("div");
  const price = product.price ? ` — ${product.price} ${product.currency || ""}` : "";
  title.textContent = `${product.name || "Item"}${price}`;
  const badge = document.createElement("span");
  badge.className = `check-verdict ${data.verdict || "caution"}`;
  badge.textContent = data.verdict || "unknown";
  const summary = document.createElement("div");
  summary.className = "check-section";
  summary.textContent = data.summary || "";
  card.append(who, title, badge, summary);
  if (data.price_assessment) {
    checkLine(card, "Price", [`${data.price_assessment} — ${data.price_reference || ""}`]);
  }
  checkLine(card, "Why", data.reasons);
  checkLine(card, "Flags", data.flags);
  const criteriaFails = (data.criteria_results || [])
    .map(c => `${c.pass ? "✓" : "✗"} ${c.criterion}${c.note ? ` — ${c.note}` : ""}`);
  checkLine(card, "Your criteria", criteriaFails);
  checkLine(card, "Alternatives", (data.alternatives || []).map(
    a => `${a.name || "?"} — ${a.source || ""} ${a.price || ""}: ${a.why || ""}`
  ));
  const meta = document.createElement("div");
  meta.className = "check-meta";
  const ms = data.durations_ms || {};
  meta.textContent = `${data.mode} · ${data.adapter} · ${Math.round((ms.total_ms || 0) / 1000)}s` +
    (data.persisted ? " · saved" : " · not saved");
  card.append(meta);
  logElement.append(card);
  logElement.scrollTop = logElement.scrollHeight;
}

checkRun?.addEventListener("click", async () => {
  const text = (checkText?.value || "").trim();
  if (!text && !checkFile) { systemLine("Add a screenshot or product text first"); return; }
  if (checking) return;
  checking = true;
  if (checkRun) checkRun.disabled = true;
  systemLine(`Checking (${checkMode?.value || "quick"}) — this can take a moment…`);
  try {
    const body = { mode: checkMode?.value || "quick" };
    if (text) {
      if (/^https?:\/\/\S+$/.test(text)) body.url = text;
      else body.text = text;
    }
    if (checkFile) {
      if (checkFile.type.startsWith("image/")) {
        const dataUrl = await readFileDataUrl(checkFile);
        body.image_b64 = String(dataUrl).split(",")[1];
        body.image_mime = checkFile.type;
      } else {
        const fileText = await checkFile.text();
        body.text = [body.text, fileText].filter(Boolean).join("\n\n");
      }
    }
    const headers = { "Content-Type": "application/json", "X-Device-Id": getDeviceId() };
    const key = getApiKey();
    if (key) headers["X-Api-Key"] = key;
    const resp = await fetch(`${chatBasePath()}/api/decision/check`, {
      method: "POST", headers, body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      systemLine(`Check failed: ${data.detail || resp.status}`);
      return;
    }
    renderCheckResult(data);
  } catch (error) {
    systemLine(`Check error: ${error.message}`);
  } finally {
    checking = false;
    if (checkRun) checkRun.disabled = false;
  }
});

// --- Documents: upload a scan/photo, get classify+rendered print preview ---
const docToggle = document.querySelector("#doc-toggle");
const docPanel = document.querySelector("#doc-panel");
const docFileBtn = document.querySelector("#doc-file");
const docFileInput = document.querySelector("#doc-file-input");
const docFileName = document.querySelector("#doc-file-name");
const docRun = document.querySelector("#doc-run");
let docFile = null;
let docRunning = false;

docToggle?.addEventListener("click", () => docPanel?.classList.toggle("open"));
docFileBtn?.addEventListener("click", () => docFileInput?.click());
docFileInput?.addEventListener("change", () => {
  docFile = docFileInput.files?.[0] || null;
  if (docFileName) docFileName.textContent = docFile ? docFile.name : "no file";
});

async function authedBlob(url) {
  const headers = { "X-Device-Id": getDeviceId() };
  const key = getApiKey();
  if (key) headers["X-Api-Key"] = key;
  const resp = await fetch(`${chatBasePath()}${url}`, { headers });
  if (!resp.ok) throw new Error(`${resp.status}`);
  return URL.createObjectURL(await resp.blob());
}

function renderDocResult(data) {
  const card = document.createElement("div");
  card.className = "msg check";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = "Docs";
  const title = document.createElement("div");
  const conf = Math.round((data.confidence || 0) * 100);
  title.textContent = `${data.doc_type || "document"} — ${conf}%`;
  const summary = document.createElement("div");
  summary.className = "check-section";
  summary.textContent = data.summary || "";
  card.append(who, title, summary);
  const plan = data.plan || {};
  const meas = data.measured || {};
  checkLine(card, "Plan", [
    `${plan.output || "?"} → ${(plan.placed_mm || []).join("×")}mm on A4 @300dpi`,
    `source ${meas.width}×${meas.height}px` +
      (meas.src_dpi ? ` (${meas.src_dpi}dpi)` : "") +
      (meas.spread ? " — two-page spread, left page only" : ""),
  ]);
  checkLine(card, "Warnings", data.warnings);
  if (data.preview_url) {
    authedBlob(data.preview_url).then(u => {
      const img = document.createElement("img");
      img.src = u;
      img.style.cssText = "max-width:100%;border-radius:8px;margin-top:6px";
      card.append(img);
    }).catch(() => {});
  }
  if (data.pdf_url) {
    authedBlob(data.pdf_url).then(u => {
      const a = document.createElement("a");
      a.href = u;
      a.download = `${data.key?.split("/").pop() || "document"}.pdf`;
      a.className = "pwa-button";
      a.style.cssText = "display:inline-block;margin-top:6px;text-decoration:none";
      a.textContent = "Download PDF";
      card.append(a);
    }).catch(() => {});
  }
  const meta = document.createElement("div");
  meta.className = "check-meta";
  const ms = data.durations_ms || {};
  meta.textContent = `${Math.round((ms.total_ms || 0) / 1000)}s · held in RAM (print/archive need confirmation)`;
  card.append(meta);
  logElement.append(card);
  logElement.scrollTop = logElement.scrollHeight;
}

docRun?.addEventListener("click", async () => {
  if (!docFile) { systemLine("Choose a document image first"); return; }
  if (docRunning) return;
  docRunning = true;
  if (docRun) docRun.disabled = true;
  systemLine("Assessing document — classify + render…");
  try {
    const dataUrl = await readFileDataUrl(docFile);
    const headers = { "Content-Type": "application/json", "X-Device-Id": getDeviceId() };
    const key = getApiKey();
    if (key) headers["X-Api-Key"] = key;
    const resp = await fetch(`${chatBasePath()}/api/documents/intake`, {
      method: "POST", headers,
      body: JSON.stringify({
        image_b64: dataUrl, image_mime: docFile.type,
        filename: docFile.name,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      systemLine(`Document intake failed: ${data.detail || resp.status}`);
      return;
    }
    renderDocResult(data);
  } catch (error) {
    systemLine(`Document intake error: ${error.message}`);
  } finally {
    docRunning = false;
    if (docRun) docRun.disabled = false;
  }
});

lockButton?.addEventListener("click", async () => {
  if (!authed) { showUnlock(); return; }
  if (socket) await disconnect(true);
  try {
    await fetch(`${chatBasePath()}/api/auth/logout?path=${encodeURIComponent(chatBasePath())}`, { method: "POST", headers: { "X-Device-Id": getDeviceId() } });
  } catch {}
  localStorage.removeItem(AUTH_STORAGE_KEY);
  setLocked(true, "Locked.");
});

initAuth();
