/* jev-client.js — browser-side confirm-gate classifier (Chaba Nest edge tier).
 *
 * Loads the int8-quantized jev-student distilbert via vendored
 * transformers.js + onnxruntime-web WASM. Lazy: first user_transcript
 * triggers the (~135MB, cached) model fetch; every turn after that
 * gets scored locally and the verdict is shipped back as
 * {"type":"client_noul", p, ms} so the server-side advisory probe can
 * compare client-vs-server scores — shadow posture, the server-side
 * regex+student remains the enforcer.
 *
 * Failure is silent: no model, no verdict, Ada keeps working exactly
 * as before.
 */
(function () {
  const Jev = {
    ready: false, failed: false, busy: false, clf: null,
    _load: null,
    seen: 0,                      // turns scored this session
  };

  function ensure() {
    if (Jev._load) return Jev._load;
    Jev._load = (async () => {
      const T = window.transformers;
      if (!T) throw new Error("transformers.js missing");
      T.env.allowRemoteModels = false;
      T.env.localModelPath = "/models/";
      if (T.env.backends?.onnx?.wasm) {
        T.env.backends.onnx.wasm.wasmPaths = "/vendor/transformers/wasm/";
      }
      Jev.clf = await T.pipeline(
        "text-classification", "jev-student",
        { dtype: "q8", device: "wasm" });
      Jev.ready = true;
      console.log("[jev-client] student loaded");
    })().catch((e) => {
      Jev.failed = true;
      console.log("[jev-client] load failed:", e);
    });
    return Jev._load;
  }

  /* Score a user transcript, fire-and-forget. */
  Jev.onTranscript = function (text, send) {
    if (Jev.failed || !text || typeof send !== "function") return;
    if (!Jev.ready) { ensure(); return; }
    if (Jev.busy) return;
    Jev.busy = true;
    const t0 = performance.now();
    Jev.clf(text.slice(0, 300), { truncation: true, max_length: 96 })
      .then((out) => {
        const ms = Math.round(performance.now() - t0);
        const row = (out || []).find((r) => r.label === "LABEL_1");
        if (!row) return;
        Jev.seen += 1;
        send({ type: "client_noul", p: +row.score.toFixed(4), ms });
      })
      .catch((e) => { Jev.failed = true; console.log("[jev-client] score failed:", e); })
      .finally(() => { Jev.busy = false; });
  };

  window.jevClient = Jev;
})();
