/* Chaba guest page — text chat + name registration + voice clip enrollment.
 *
 * Protocol mirrors pwa/chat/chat.js but adds:
 *   ?name=<declared name>   bound server-side to the session's memory files
 *   {type:"register", name} queued for admin promotion
 *   {type:"identity_changed"} pushed when an admin promotes/revokes
 */
"use strict";

const $ = (s) => document.querySelector(s);
const logEl = $("#log");
const statusEl = $("#identity");
const inputEl = $("#chat-input");
const sendBtn = $("#chat-send");
const connectBtn = $("#connect");
const voiceBtn = $("#voice-btn");
const overlay = $("#overlay");
const nameInput = $("#name-input");
const nameError = $("#name-error");
const nameSubmit = $("#name-submit");
const keyInput = $("#key-input");

const NAME_KEY = "chaba_guest_name";
const KEY_KEY = "chaba_api_key";

let socket = null;
let assistantEntry = null;
let guestName = localStorage.getItem(NAME_KEY) || "";

function basePath() {
  return location.pathname.replace(/\/guest\/?$/, "") || "/";
}

function getApiKey() {
  const fromUrl = new URLSearchParams(location.search).get("api_key");
  if (fromUrl) {
    localStorage.setItem(KEY_KEY, fromUrl);
    history.replaceState(null, "", location.pathname);
  }
  return localStorage.getItem(KEY_KEY) || "";
}

function setStatus(t) { statusEl.textContent = t; }

function addMsg(who, text, cls) {
  const el = document.createElement("div");
  el.className = `msg ${cls}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.textContent = text;
  el.append(label, body);
  logEl.append(el);
  logEl.scrollTop = logEl.scrollHeight;
  return { el, body };
}

function systemLine(text) {
  const el = document.createElement("div");
  el.className = "msg system";
  el.textContent = text;
  logEl.append(el);
  logEl.scrollTop = logEl.scrollHeight;
}

function flushAssistant() {
  if (assistantEntry) { assistantEntry.el.classList.remove("streaming"); assistantEntry = null; }
}

function handleControl(event) {
  switch (event.type) {
    case "ready":
      inputEl.disabled = false;
      sendBtn.disabled = false;
      setStatus(`${guestName || "Guest"} — connected`);
      break;
    case "assistant_transcript_delta":
      if (!assistantEntry) assistantEntry = addMsg("Chaba", "", "ada streaming");
      assistantEntry.body.textContent += event.text;
      logEl.scrollTop = logEl.scrollHeight;
      break;
    case "response_started":
    case "response_completed":
      flushAssistant();
      break;
    case "user_transcript":
      addMsg("You (voice)", event.text, "mine");
      break;
    case "registered":
      systemLine(`Registered as "${event.name}" — awaiting admin approval for a named profile.`);
      voiceBtn.hidden = false;
      break;
    case "identity_changed":
      if (event.kind === "user") {
        guestName = event.name;
        localStorage.setItem(NAME_KEY, guestName);
        setStatus(`${guestName} — promoted user`);
        systemLine(`You're now a named user — welcome, ${event.name}!`);
      } else {
        setStatus("Guest");
        systemLine("Your session was reset to guest.");
      }
      break;
    case "speech_started":
      setStatus("Listening…");
      break;
    case "speech_stopped":
      setStatus(`${guestName || "Guest"} — connected`);
      break;
    case "error":
      systemLine(`Error: ${event.message}`);
      break;
  }
}

async function ensureSession() {
  const key = getApiKey();
  if (!key) return; // auth may be off, or cookie already set
  try {
    await fetch(`${basePath()}/api/auth/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key, path: basePath() }),
    });
  } catch { /* cookie auth optional */ }
}

async function connect() {
  if (socket) return;
  setStatus("Connecting…");
  await ensureSession();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const key = getApiKey();
  const url = `${scheme}://${location.host}${basePath()}/ws`
    + `?name=${encodeURIComponent(guestName)}`
    + (key ? `&api_key=${encodeURIComponent(key)}` : "");
  socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  socket.onmessage = (m) => {
    if (typeof m.data === "string") {
      try { handleControl(JSON.parse(m.data)); } catch { /* ignore */ }
    }
    // binary frames are audio — guest page is text-first; drop them.
  };
  socket.onclose = (e) => {
    socket = null;
    inputEl.disabled = true;
    sendBtn.disabled = true;
    setStatus(e.code === 4401 ? "Access denied — check your link/key" : "Disconnected");
    connectBtn.textContent = "Connect";
  };
  socket.onerror = () => setStatus("Connection error");
  connectBtn.textContent = "Reconnect";
}

function send() {
  const text = inputEl.value.trim();
  if (!text || !socket) return;
  socket.send(JSON.stringify({ type: "text", text }));
  addMsg("You", text, "mine");
  inputEl.value = "";
}

// --- voice clip enrollment ----------------------------------------------
// Records ~3s of mic audio, converts to PCM16 base64, and enrolls the clip
// under guest-<name> so an admin can bind it to a person on promotion.

async function recordAndEnroll() {
  if (!guestName) { systemLine("Set your name first."); return; }
  const key = getApiKey();
  voiceBtn.disabled = true;
  voiceBtn.classList.add("recording");
  voiceBtn.textContent = "🎙 Recording…";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ctx = new AudioContext({ sampleRate: 16000 });
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    proc.onaudioprocess = (e) => {
      const f = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f.length);
      for (let i = 0; i < f.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f[i] * 32768));
      chunks.push(i16);
    };
    src.connect(proc);
    proc.connect(ctx.destination);
    await new Promise((r) => setTimeout(r, 3200));
    proc.disconnect(); src.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    await ctx.close();
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const pcm = new Int16Array(total);
    let off = 0;
    for (const c of chunks) { pcm.set(c, off); off += c.length; }
    const b64 = btoa(String.fromCharCode(...new Uint8Array(pcm.buffer)));
    const slug = guestName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "guest";
    const resp = await fetch(`${basePath()}/api/speakers/enroll`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(key ? { "X-Api-Key": key } : {}) },
      body: JSON.stringify({
        name: `guest-${slug}`,
        audio: b64,
        display_name: guestName,
      }),
    });
    if (resp.ok) {
      systemLine("Voice clip saved — it will be linked to your profile on approval.");
    } else {
      systemLine(`Voice enrollment failed (${resp.status}) — you can try again.`);
    }
  } catch (e) {
    systemLine(`Microphone unavailable: ${e.message || e}`);
  } finally {
    voiceBtn.disabled = false;
    voiceBtn.classList.remove("recording");
    voiceBtn.textContent = "🎙 Voice";
  }
}

// --- startup -------------------------------------------------------------

nameSubmit.onclick = async () => {
  const name = nameInput.value.trim();
  if (!name) { nameError.textContent = "Please enter your name."; return; }
  if (keyInput.style.display !== "none" && !keyInput.value.trim()) {
    // key shown but empty — allow anyway; server will 4401 if required
  }
  if (keyInput.value.trim()) localStorage.setItem(KEY_KEY, keyInput.value.trim());
  guestName = name;
  localStorage.setItem(NAME_KEY, guestName);
  overlay.hidden = true;
  await connect();
  if (socket) socket.send(JSON.stringify({ type: "register", name }));
};
nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") nameSubmit.click(); });
connectBtn.onclick = connect;
sendBtn.onclick = send;
inputEl.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
voiceBtn.onclick = recordAndEnroll;

if (guestName) {
  overlay.hidden = true;
  setStatus(`${guestName} — tap Connect`);
} else {
  nameInput.focus();
}
