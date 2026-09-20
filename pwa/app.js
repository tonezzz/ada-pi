const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

const connectButton = document.querySelector("#connect");
const disconnectButton = document.querySelector("#disconnect");
const statusElement = document.querySelector("#pwa-status");
const logElement = document.querySelector("#pwa-log");

let socket = null;
let stream = null;
let captureContext = null;
let playbackContext = null;
let captureNode = null;
let playbackNode = null;
let playbackAnalyser = null;
let playbackMeterFrame = null;
let assistantEntry = null;
let assistantPlaybackActive = false;
let localSpeechActive = false;
let speechAboveFrames = 0;
let speechBelowFrames = 0;
let microphoneNoiseFloor = .004;
let connectionInProgress = false;
let micMuted = false;
const micToggleButton = document.querySelector("#mic-toggle");

function setMicButton(muted) {
  if (micToggleButton) micToggleButton.textContent = muted ? "Mic: Off" : "Mic: On";
}

function setStatus(text) {
  if (statusElement) statusElement.textContent = text;
}

function setConnected(connected) {
  if (connectButton) connectButton.disabled = connected || (authRequired && !authed);
  if (disconnectButton) disconnectButton.disabled = !connected;
}

function logLine(text, className = "") {
  if (!logElement) return;
  const line = document.createElement("div");
  line.className = `entry ${className}`;
  line.textContent = text;
  logElement.append(line);
  logElement.scrollTop = logElement.scrollHeight;
}

function downsampleToPCM16(input, inputRate, outputRate) {
  const ratio = inputRate / outputRate;
  const length = Math.floor(input.length / ratio);
  const output = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end && j < input.length; j++) sum += input[j];
    const sample = Math.max(-1, Math.min(1, sum / (end - start)));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output.buffer;
}

async function createPlayback() {
  playbackContext = new AudioContext({ sampleRate: OUTPUT_RATE, latencyHint: "interactive" });
  const workletSource = `
    class PCMPlayer extends AudioWorkletProcessor {
      constructor() {
        super();
        this.queue = [];
        this.offset = 0.0;
        this.step = 24000 / sampleRate;
        this.inputRate = 24000;
        this.bufferedSamples = 0;
        this.playing = false;
        this.forceStart = false;
        this.startThreshold = 0;
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
          if (this.bufferedSamples < this.startThreshold && !(this.forceStart && this.bufferedSamples)) return true;
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
  playbackAnalyser = playbackContext.createAnalyser();
  playbackAnalyser.fftSize = 256;
  playbackAnalyser.smoothingTimeConstant = .68;
  playbackNode.connect(playbackAnalyser);
  playbackAnalyser.connect(playbackContext.destination);
  await playbackContext.resume();
  console.info("playback context", playbackContext.sampleRate, playbackContext.state);
  playbackContext.onstatechange = () => console.info("playback state", playbackContext.state);
  document.body.addEventListener("touchstart", () => playbackContext?.resume().catch(() => {}), { passive: true });
  document.body.addEventListener("click", () => playbackContext?.resume().catch(() => {}));
  startPlaybackMeter();
}

function startPlaybackMeter() {
  const samples = new Float32Array(playbackAnalyser.fftSize);
  let smoothed = 0;
  let lastUpdate = 0;

  const measure = (now) => {
    if (!playbackAnalyser) return;
    playbackAnalyser.getFloatTimeDomainData(samples);
    let power = 0;
    for (const sample of samples) power += sample * sample;
    const rms = Math.sqrt(power / samples.length);
    const level = Math.min(1, Math.max(0, (rms - .006) * 8.5));
    smoothed = level > smoothed ? smoothed * .48 + level * .52 : smoothed * .76 + level * .24;

    if (now - lastUpdate >= 33) {
      window.idleFace?.setSpeechLevel(smoothed);
      lastUpdate = now;
    }
    playbackMeterFrame = requestAnimationFrame(measure);
  };
  playbackMeterFrame = requestAnimationFrame(measure);
}

async function startMicrophone() {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false }
  });
  const track = stream.getAudioTracks()[0];
  track.enabled = !micMuted;
  if (micToggleButton) micToggleButton.disabled = false;
  const settings = track.getSettings();
  setStatus(`Mic ${micMuted ? "muted" : "on"} (EC:${settings.echoCancellation}, AGC:${settings.autoGainControl}, ${settings.sampleRate} Hz)`);

  captureContext = new AudioContext({ latencyHint: "interactive" });
  const source = captureContext.createMediaStreamSource(stream);
  captureNode = captureContext.createScriptProcessor(2048, 1, 1);
  const silent = captureContext.createGain();
  silent.gain.value = 0;
  captureNode.onaudioprocess = (event) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const samples = event.inputBuffer.getChannelData(0);
    if (micMuted) return;
    let power = 0;
    for (const sample of samples) power += sample * sample;
    const rms = Math.sqrt(power / samples.length);
    if (!localSpeechActive) microphoneNoiseFloor = microphoneNoiseFloor * .98 + rms * .02;
    const startThreshold = Math.max(.012, microphoneNoiseFloor * 2.8);
    const stopThreshold = Math.max(.008, microphoneNoiseFloor * 1.7);
    if (!localSpeechActive) {
      speechAboveFrames = rms > startThreshold ? speechAboveFrames + 1 : 0;
      if (speechAboveFrames >= 3) {
        localSpeechActive = true;
        speechBelowFrames = 0;
        socket.send(JSON.stringify({ type: "local_speech_started", rms, threshold: startThreshold }));
      }
    } else {
      speechBelowFrames = rms < stopThreshold ? speechBelowFrames + 1 : 0;
      if (speechBelowFrames >= 12) {
        localSpeechActive = false;
        speechAboveFrames = 0;
        socket.send(JSON.stringify({ type: "local_speech_stopped", rms, threshold: stopThreshold }));
      }
    }
    const pcm = downsampleToPCM16(samples, captureContext.sampleRate, INPUT_RATE);
    socket.send(pcm);
  };
  source.connect(captureNode);
  captureNode.connect(silent);
  silent.connect(captureContext.destination);
  await captureContext.resume();
}

function handleControl(event) {
  switch (event.type) {
    case "ready":
      window.idleFace?.setConnecting(false);
      setConnected(true);
      setStatus("Connected — listening");
      break;
    case "speech_started":
      setStatus("Speech detected");
      break;
    case "speech_stopped":
      setStatus("On — listening");
      break;
    case "clear_audio":
      assistantPlaybackActive = false;
      playbackNode?.port.postMessage({ type: "clear" });
      window.idleFace?.setSpeechLevel(0, true);
      assistantEntry = null;
      break;
    case "user_transcript":
      logLine(`You: ${event.text}`);
      break;
    case "assistant_transcript_delta":
      if (!assistantEntry) assistantEntry = event.text;
      else assistantEntry += event.text;
      break;
    case "response_started":
      assistantPlaybackActive = true;
      if (assistantEntry) logLine(`Ada: ${assistantEntry}`);
      assistantEntry = "";
      if (playbackContext?.state === "suspended") playbackContext.resume().catch(() => {});
      break;
    case "response_completed":
      assistantPlaybackActive = false;
      if (assistantEntry) logLine(`Ada: ${assistantEntry}`);
      assistantEntry = null;
      playbackNode?.port.postMessage({ type: "flush" });
      break;
    case "response_interrupted":
      assistantPlaybackActive = false;
      window.idleFace?.setSpeechLevel(0, true);
      assistantEntry = null;
      break;
    case "expression":
      window.idleFace?.setExpression(event.name);
      break;
    case "live_reconnecting":
      assistantPlaybackActive = false;
      window.idleFace?.setConnecting(true);
      setStatus("Reconnecting…");
      break;
    case "error":
      logLine(`Error: ${event.message}`, "system");
      break;
  }
}

async function connect() {
  if (connectionInProgress || socket) return;
  connectionInProgress = true;
  window.idleFace?.setConnecting(true);
  setStatus("Requesting microphone…");
  try {
    await ensureSession();
    await createPlayback();
    await startMicrophone();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const basePath = appBasePath();
    const key = getApiKey();
    const wsUrl = `${scheme}://${location.host}${basePath}/ws`
      + `?device_id=${encodeURIComponent(getDeviceId())}`
      + (key ? `&api_key=${encodeURIComponent(key)}` : "");
    socket = new WebSocket(wsUrl);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => setStatus("Connecting to AI…");
    socket.onmessage = (message) => {
      if (typeof message.data === "string") handleControl(JSON.parse(message.data));
      else playbackNode?.port.postMessage(message.data, [message.data]);
    };
    socket.onerror = () => logLine("WebSocket error", "system");
    socket.onclose = (event) => {
      if (event.code === 4401) {
        authRequired = true;
        localStorage.removeItem(AUTH_STORAGE_KEY);
        setLocked(true, "This device isn't authorized — enter the API key.");
      }
      disconnect(false);
    };
  } catch (error) {
    window.idleFace?.setConnecting(false, true);
    console.error(error);
    setStatus(error.message);
    await disconnect(false);
  } finally {
    connectionInProgress = false;
  }
}

async function disconnect(closeSocket = true) {
  window.idleFace?.setConnecting(false, true);
  if (closeSocket && socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "user disconnect");
  socket = null;
  if (captureNode) captureNode.disconnect();
  if (playbackMeterFrame) cancelAnimationFrame(playbackMeterFrame);
  playbackMeterFrame = null;
  playbackAnalyser = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  if (captureContext) await captureContext.close().catch(() => {});
  if (playbackContext) await playbackContext.close().catch(() => {});
  stream = captureContext = playbackContext = captureNode = playbackNode = null;
  localSpeechActive = false;
  speechAboveFrames = speechBelowFrames = 0;
  microphoneNoiseFloor = .004;
  assistantEntry = null;
  setStatus("Disconnected");
  setConnected(false);
  if (micToggleButton) micToggleButton.disabled = true;
  window.idleFace?.setSpeechLevel(0, true);
}

connectButton?.addEventListener("click", connect);
disconnectButton?.addEventListener("click", () => disconnect(true));

micToggleButton?.addEventListener("click", () => {
  micMuted = !micMuted;
  setMicButton(micMuted);
  const track = stream?.getAudioTracks()[0];
  if (track) track.enabled = !micMuted;
  setStatus(micMuted ? "Mic muted" : "Mic on");
});

// --- Auth: API key storage, session cookie, lock UI ---

const AUTH_STORAGE_KEY = "ada_api_key";
const DEVICE_STORAGE_KEY = "ada_device_id";
const NAME_STORAGE_KEY = "ada_key_name";

function systemLabel() {
  const path = appBasePath();
  if (path.includes("ada-michael")) return "michael-ha";
  if (path.includes("ada-tony")) return "tony-ha";
  return path;
}

function getDeviceId() {
  let id = localStorage.getItem(DEVICE_STORAGE_KEY);
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/-/g, "");
    localStorage.setItem(DEVICE_STORAGE_KEY, id);
  }
  return id;
}
const lockButton = document.querySelector("#lock-toggle");
const unlockOverlay = document.querySelector("#unlock-overlay");
const unlockInput = document.querySelector("#unlock-key");
const unlockError = document.querySelector("#unlock-error");
const unlockHint = document.querySelector("#unlock-hint");
const unlockSubmit = document.querySelector("#unlock-submit");
let authRequired = false;
let authed = true;

function appBasePath() {
  return location.pathname.replace(/\/[^\/]*$/, "") || "/";
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
  if (unlockHint) unlockHint.textContent = hint || "Enter the API key to unlock voice and control.";
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
    const resp = await fetch(`${appBasePath()}/api/auth/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Device-Id": getDeviceId() },
      body: JSON.stringify({ api_key: key, path: appBasePath(), device_id: getDeviceId() }),
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
      setStatus("Unlocked — tap Connect");
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
    const resp = await fetch(`${appBasePath()}/api/auth/status`, { headers: { "X-Device-Id": getDeviceId() } });
    status = await resp.json();
  } catch {
    return;
  }
  authRequired = !!status.auth_configured;
  if (!authRequired) return;

  let linkRejected = "";
  const urlKey = new URLSearchParams(location.search).get("api_key");
  if (urlKey) {
    // A redeem link just landed — verify it names THIS device before trusting it.
    // No X-Device-Id: reading the name must not bind an unbound key to us.
    const who = await fetch(`${appBasePath()}/api/auth/status`, {
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
  if (status.authenticated) { setLocked(false); if (linkRejected) setStatus(linkRejected); return; }
  if (getApiKey()) {
    if (await ensureSession()) { setLocked(false); if (linkRejected) setStatus(linkRejected); return; }
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

lockButton?.addEventListener("click", async () => {
  if (!authed) { showUnlock(); return; }
  if (socket) await disconnect(true);
  try {
    await fetch(`${appBasePath()}/api/auth/logout?path=${encodeURIComponent(appBasePath())}`, { method: "POST", headers: { "X-Device-Id": getDeviceId() } });
  } catch {}
  localStorage.removeItem(AUTH_STORAGE_KEY);
  setLocked(true, "Locked.");
});

// --- Re-pair: scan a same-name redeem QR inside the PWA ---

const repairButton = document.querySelector("#repair");
const repairOpen2 = document.querySelector("#unlock-repair");
const repairOverlay = document.querySelector("#repair-overlay");
const repairTitle = document.querySelector("#repair-title");
const repairSystem = document.querySelector("#repair-system");
const repairHint = document.querySelector("#repair-hint");
const repairVideo = document.querySelector("#repair-video");
const repairLink = document.querySelector("#repair-link");
const repairError = document.querySelector("#repair-error");
const repairCancel = document.querySelector("#repair-cancel");
let repairStream = null;
let repairScanTimer = null;
let repairBusy = false;

async function openRepair() {
  if (!repairOverlay) return;
  const name = localStorage.getItem(NAME_STORAGE_KEY) || "";
  repairTitle.textContent = `Re-pair ${name || "this device"}`;
  repairSystem.textContent = `system: ${systemLabel()}`;
  repairHint.textContent = name
    ? `Only a QR/link minted for "${name}" will be accepted.`
    : "No stored device name — paste the redeem link to pair this device.";
  repairError.textContent = "";
  repairLink.value = "";
  repairOverlay.hidden = false;
  try {
    repairStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
    repairVideo.srcObject = repairStream;
    await repairVideo.play();
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    repairScanTimer = setInterval(() => {
      if (repairBusy || !repairVideo.videoWidth) return;
      canvas.width = repairVideo.videoWidth;
      canvas.height = repairVideo.videoHeight;
      ctx.drawImage(repairVideo, 0, 0);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const code = jsQR(img.data, img.width, img.height);
      if (code?.data) redeemScanned(code.data);
    }, 350);
  } catch {
    repairError.textContent = "Camera unavailable — paste the redeem link below.";
  }
}

function closeRepair() {
  if (repairScanTimer) clearInterval(repairScanTimer);
  repairScanTimer = null;
  repairBusy = false;
  if (repairStream) repairStream.getTracks().forEach(t => t.stop());
  repairStream = null;
  if (repairVideo) repairVideo.srcObject = null;
  if (repairOverlay) repairOverlay.hidden = true;
}

function redeemTokenFromText(text) {
  text = (text || "").trim();
  try {
    const u = new URL(text, location.origin);
    const m = u.pathname.match(/\/redeem\/([A-Za-z0-9_-]+)/);
    if (m) return m[1];
  } catch {}
  return /^[A-Za-z0-9_-]{10,}$/.test(text) ? text : null;
}

async function redeemScanned(text) {
  const token = redeemTokenFromText(text);
  if (!token) { repairError.textContent = "Not a redeem QR/link."; return; }
  if (repairBusy) return;
  repairBusy = true;
  repairError.textContent = "Redeeming…";
  try {
    const resp = await fetch(`${appBasePath()}/api/auth/redeem`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Key": getApiKey(), "X-Device-Id": getDeviceId() },
      body: JSON.stringify({
        token,
        expect_name: localStorage.getItem(NAME_STORAGE_KEY) || "",
        device_id: getDeviceId(),
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      repairBusy = false;
      repairError.textContent = data.detail || `Re-pair failed (${resp.status})`;
      return;
    }
    localStorage.setItem(AUTH_STORAGE_KEY, data.api_key);
    localStorage.setItem(NAME_STORAGE_KEY, data.name);
    await ensureSession();
    closeRepair();
    setLocked(false);
    setStatus(`Re-paired as ${data.name} · ${systemLabel()} — tap Connect`);
  } catch {
    repairBusy = false;
    repairError.textContent = "Re-pair request failed.";
  }
}

repairButton?.addEventListener("click", openRepair);
repairOpen2?.addEventListener("click", openRepair);
repairCancel?.addEventListener("click", closeRepair);
repairLink?.addEventListener("keydown", e => { if (e.key === "Enter") redeemScanned(repairLink.value); });

initAuth();
