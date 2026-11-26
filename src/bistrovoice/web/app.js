// BistroVoice browser client.
//
// Mic -> AudioWorklet (16 kHz PCM16, 20 ms frames) -> energy VAD -> WebSocket.
// Replies come back as one WAV per sentence (Piper on the server) or as text
// that the browser speaks with an English system voice (small hosts), and are
// queued for playback.
// If the customer starts talking while the agent is busy or speaking, the
// client stops the audio at once and tells the server how many sentences
// were fully heard (barge-in).

const $ = (id) => document.getElementById(id);

// ----------------------------------------------------------------- VAD tuning
const FRAME_MS = 20;
const START_FRAMES = 4;        // 80 ms above threshold starts an utterance
const BARGE_FRAMES = 9;        // stricter while the agent is talking (echo)
const END_FRAMES = 32;         // 640 ms of quiet ends it
const PREROLL_FRAMES = 15;     // keep 300 ms before the start so words aren't clipped
const MIN_START = 0.012;
const MIN_BARGE = 0.035;

const s = {
  ws: null,
  micCtx: null,
  playCtx: null,
  live: false,
  userTalking: false,
  agentState: "idle",          // idle | transcribing | thinking | speaking
  floor: 0.004,
  above: 0,
  quiet: 0,
  preroll: [],
  chunks: [],                  // scheduled audio: {seq, start, end, src}
  nextStart: 0,
  decodeChain: Promise.resolve(),
  pendingAudio: null,
  current: null,               // current agent bubble
  lastUser: null,
  heard: 0,                    // sentences of the current reply played to the end
  dropAudio: false,            // after a barge-in, ignore audio still in flight
};

// ----------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (ev) => (typeof ev.data === "string" ? onJson(JSON.parse(ev.data)) : onAudio(ev.data));
  ws.onclose = () => {
    setStatus("Connection lost. Reconnecting...");
    setTimeout(connect, 1500);
  };
  s.ws = ws;
}

function send(obj) {
  if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify(obj));
}

function onJson(m) {
  switch (m.type) {
    case "status": onStatus(m); break;
    case "transcript": addUser(m.text, m.confidence); break;
    case "tool": addTool(m); break;
    case "guard": addGuard(m.events); break;
    case "reply": s.heard = 0; s.dropAudio = false; addAgent(m.text); break;
    case "audio": s.pendingAudio = m; break;
    case "speak": onSpeak(m); break;
    case "metrics": addMetrics(m); break;
    case "bookings": renderBookings(m); break;
  }
}

function onStatus(m) {
  if (m.state === "ready") {
    $("chip-stt").textContent = m.stt.includes("Groq") ? `Whisper ${m.stt}` : `faster-whisper ${m.stt}`;
    $("chip-llm").textContent = m.llm;
    $("chip-tts").textContent = m.tts === "browser" ? "browser voice (English)"
      : `Piper ${m.tts.replace(/^en_\w\w-/, "").replace(/-medium$/, "")}`;
    return;
  }
  if (m.state === "reset") {
    $("log").innerHTML = "";
    $("trace").innerHTML = '<p class="empty">New call. Nothing yet.</p>';
    return;
  }
  if (m.state === "interrupted") return;
  s.agentState = m.state === "listening" ? "idle" : m.state;
  const labels = {
    transcribing: "Transcribing...",
    thinking: "Thinking and checking the system...",
    speaking: "Speaking (just talk to interrupt)",
  };
  setStatus(labels[m.state] || "Listening...");
  setStep({ transcribing: "stt", thinking: "agent", speaking: "speak" }[m.state]);
  $("mic").classList.toggle("thinking", m.state === "thinking" || m.state === "transcribing");
}

// ----------------------------------------------------------------- playback
function onAudio(buf) {
  const meta = s.pendingAudio;
  s.pendingAudio = null;
  if (!meta) return;
  // decode in order, schedule back to back
  s.decodeChain = s.decodeChain.then(async () => {
    if (s.dropAudio) return;
    const audio = await s.playCtx.decodeAudioData(buf);
    if (s.dropAudio) return;
    const src = s.playCtx.createBufferSource();
    src.buffer = audio;
    src.connect(s.playCtx.destination);
    const start = Math.max(s.playCtx.currentTime + 0.02, s.nextStart);
    src.start(start);
    s.nextStart = start + audio.duration;
    const chunk = { seq: meta.seq, start, end: s.nextStart, src, stopped: false };
    s.chunks.push(chunk);
    $("mic").classList.add("speaking");
    src.onended = () => {
      if (!chunk.stopped) s.heard++;
      finishChunk(chunk);
    };
  }).catch(() => {});
}

function finishChunk(chunk) {
  s.chunks = s.chunks.filter((c) => c !== chunk);
  if (s.chunks.length) return;
  $("mic").classList.remove("speaking");
  if (s.agentState === "speaking") {
    s.agentState = "idle";
    setStatus(s.live ? "Listening..." : "Type or tap the orb to talk");
    setStep(null);
  }
}

// ----------------------------------------------------------------- browser voice
// Used when the server is too small to run Piper quickly. Always an English
// voice, Indian English if the browser has one, whatever the system language is.
const synth = window.speechSynthesis;
let englishVoice = null;

function pickVoice() {
  const voices = synth ? synth.getVoices() : [];
  const en = voices.filter((v) => /^en[-_]/i.test(v.lang));
  const female = /female|libby|sonia|hazel|serena|kate|zira|aria|jenny|samantha|ava|allison/i;
  englishVoice = en.find((v) => /en[-_]IN/i.test(v.lang) && /heera|neerja|female/i.test(v.name))
    || en.find((v) => /en[-_]IN/i.test(v.lang))
    || en.find((v) => /en[-_]GB/i.test(v.lang) && female.test(v.name))
    || en.find((v) => /en[-_]GB/i.test(v.lang))
    || en.find((v) => /en[-_]US/i.test(v.lang) && female.test(v.name))
    || en.find((v) => /en[-_]US/i.test(v.lang))
    || en[0] || null;
}
if (synth) {
  pickVoice();
  synth.onvoiceschanged = pickVoice;
}

function onSpeak(m) {
  if (s.dropAudio || !synth) return;
  const u = new SpeechSynthesisUtterance(m.say || m.text);
  if (englishVoice) u.voice = englishVoice;
  u.lang = englishVoice ? englishVoice.lang : "en-IN";
  const chunk = { seq: m.seq, stopped: false, speech: true };
  s.chunks.push(chunk);
  $("mic").classList.add("speaking");
  u.onend = () => {
    if (!chunk.stopped) s.heard++;
    finishChunk(chunk);
  };
  u.onerror = () => {
    chunk.stopped = true;
    finishChunk(chunk);
  };
  synth.speak(u);
}

function isPlaying() {
  return s.chunks.length > 0;
}

// Stops the agent mid-sentence. Returns how many sentences were heard in full;
// a half-heard sentence doesn't count (it might have been a booking read-back).
function stopPlayback() {
  s.dropAudio = true;
  s.chunks.forEach((c) => { c.stopped = true; try { if (c.src) c.src.stop(); } catch (_) {} });
  if (synth) synth.cancel();
  s.chunks = [];
  s.nextStart = 0;
  $("mic").classList.remove("speaking");
  return s.heard;
}

// ----------------------------------------------------------------- microphone
async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  s.stream = stream;
  s.micCtx = new AudioContext();
  if (!s.playCtx) s.playCtx = new AudioContext();
  await s.micCtx.audioWorklet.addModule("/static/mic-worklet.js");
  const node = new AudioWorkletNode(s.micCtx, "mic-processor");
  s.micCtx.createMediaStreamSource(stream).connect(node);
  node.port.onmessage = (ev) => onFrame(ev.data.pcm, ev.data.rms);
  s.live = true;
  $("mic").classList.add("live");
  $("stop").hidden = false;
  setStatus("Listening... say hello");
}

// End the call: stop the agent talking, release the microphone.
function stopCall() {
  const heard = stopPlayback();
  send({ type: "stop", heard });
  if (s.current && s.agentState !== "idle") s.current.classList.add("cut");
  if (s.stream) s.stream.getTracks().forEach((t) => t.stop());
  if (s.micCtx) s.micCtx.close();
  s.stream = null;
  s.micCtx = null;
  s.live = false;
  s.userTalking = false;
  s.preroll = [];
  s.agentState = "idle";
  $("mic").classList.remove("live", "speaking", "thinking");
  $("mic").style.setProperty("--level", "0");
  $("stop").hidden = true;
  setStep(null);
  setStatus("Call stopped. Tap the orb to talk again.");
}

function onFrame(pcm, rms) {
  $("mic").style.setProperty("--level", Math.min(1, rms * 12).toFixed(3));
  if (!s.userTalking) {
    s.preroll.push(pcm);
    if (s.preroll.length > PREROLL_FRAMES) s.preroll.shift();
    const playing = isPlaying();
    const threshold = playing ? Math.max(MIN_BARGE, s.floor * 7) : Math.max(MIN_START, s.floor * 3.5);
    if (rms > threshold) {
      s.above++;
    } else {
      s.above = 0;
      if (rms < s.floor * 2.5 || rms < MIN_START) s.floor = Math.max(0.002, 0.97 * s.floor + 0.03 * rms);
    }
    if (s.above >= (playing ? BARGE_FRAMES : START_FRAMES)) beginUtterance();
    return;
  }
  s.ws.send(pcm);
  const endThreshold = Math.max(MIN_START * 0.8, s.floor * 2.5);
  s.quiet = rms < endThreshold ? s.quiet + 1 : 0;
  if (s.quiet >= END_FRAMES) endUtterance();
}

function beginUtterance() {
  s.userTalking = true;
  s.above = 0;
  s.quiet = 0;
  if (isPlaying() || s.agentState !== "idle") bargeIn();
  for (const f of s.preroll) s.ws.send(f);
  s.preroll = [];
  setStatus("Hearing you...");
  setStep("listen");
}

function endUtterance() {
  s.userTalking = false;
  send({ type: "end_utterance" });
}

function bargeIn() {
  const heard = stopPlayback();
  send({ type: "interrupt", heard });
  if (s.current) s.current.classList.add("cut");
  s.agentState = "idle";
}

// ----------------------------------------------------------------- UI
function setStatus(text) { $("status").textContent = text; }

function setStep(step) {
  const order = ["listen", "stt", "agent", "guard", "speak"];
  const idx = order.indexOf(step);
  document.querySelectorAll("#pipeline li").forEach((li, i) => {
    li.classList.toggle("active", i === idx || (step === "agent" && li.dataset.step === "guard"));
    li.classList.toggle("done", idx > -1 && i < idx);
  });
}

function bubble(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  $("log").appendChild(div);
  $("log").scrollTop = $("log").scrollHeight;
  return div;
}

function addUser(text, confidence) {
  s.lastUser = bubble("user", text);
  if (confidence !== null && confidence !== undefined) {
    const c = document.createElement("div");
    c.className = "conf" + (confidence < 0.55 ? " low" : "");
    c.textContent = `heard with ${(confidence * 100).toFixed(0)}% confidence`;
    s.lastUser.appendChild(c);
  }
  traceDivider();
}

function addAgent(text) {
  s.current = bubble("agent", text);
}

function addMetrics(m) {
  $("ms-stt").textContent = m.stt_ms ? `${m.stt_ms} ms` : "typed";
  $("ms-agent").textContent = `${m.llm_ms + m.tool_ms} ms`;
  $("ms-tts").textContent = m.tts_first_ms ? `${m.tts_first_ms} ms first` : "browser voice";
  $("first-audio").textContent = m.first_audio_ms ? `first audio ${m.first_audio_ms} ms after you stopped` : "";
  if (!s.current) return;
  const meta = document.createElement("div");
  meta.className = "meta";
  const parts = [
    m.stt_ms ? ["STT", m.stt_ms] : null,
    ["LLM", m.llm_ms, `${m.llm_calls}x`],
    m.tool_ms ? ["tools", m.tool_ms] : null,
    m.tts_first_ms ? ["TTS 1st", m.tts_first_ms] : null,
  ].filter(Boolean);
  for (const [k, v, extra] of parts) {
    const span = document.createElement("span");
    span.textContent = `${k} ${v} ms${extra ? " · " + extra : ""}`;
    meta.appendChild(span);
  }
  const fa = document.createElement("span");
  fa.className = "hl";
  fa.textContent = `first audio ${m.first_audio_ms} ms`;
  meta.appendChild(fa);
  s.current.appendChild(meta);
}

function traceDivider() {
  const t = $("trace");
  const empty = t.querySelector(".empty");
  if (empty) empty.remove();
}

function short(obj, n = 160) {
  const txt = JSON.stringify(obj);
  return txt.length > n ? txt.slice(0, n - 1) + "…" : txt;
}

function addTool(m) {
  traceDivider();
  const r = m.result || {};
  const div = document.createElement("div");
  div.className = "tool";
  const ok = r.ok !== false;
  let summary = r.say || r.message || (r.results ? `${r.results.length} dish(es): ` +
    r.results.slice(0, 4).map((d) => d.dish).join(", ") : short(r));
  if (r.status === "booked") summary = `BOOKED · reference ${r.reference} · ${summary}`;
  div.innerHTML = `
    <div class="head"><span class="name ${m.forced ? "code" : ""}"></span><span class="ms">${m.ms} ms</span></div>
    <div class="args"></div><div class="res ${ok ? "ok" : "bad"}"></div>`;
  div.querySelector(".name").textContent = (m.forced ? "code → " : "llm → ") + m.name;
  div.querySelector(".args").textContent = short(m.args);
  div.querySelector(".res").textContent = (ok ? "→ " : "✕ ") + summary;
  $("trace").appendChild(div);
  $("trace").scrollTop = $("trace").scrollHeight;
}

function addGuard(events) {
  for (const e of events) {
    const div = document.createElement("div");
    div.className = "guard";
    const [name, ...rest] = e.split(":");
    div.innerHTML = "<b></b> <span></span>";
    div.querySelector("b").textContent = "guardrail · " + name.replaceAll("_", " ");
    div.querySelector("span").textContent = rest.join(":").trim();
    $("trace").appendChild(div);
  }
  $("trace").scrollTop = $("trace").scrollHeight;
}

function renderBookings(m) {
  const days = {};
  for (const r of m.reservations) (days[r.date] ||= []).push(r);
  const fmt = (d) => new Date(d + "T12:00").toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "short" });
  let html = '<div class="bookings-scroll"><table><tr><th>Time</th><th>Name</th><th>Guests</th><th>Table</th><th>Ref</th></tr>';
  for (const [d, rows] of Object.entries(days).slice(0, 7)) {
    html += `<tr class="day"><td colspan="5">${fmt(d)}${d === m.today ? " (today)" : ""}</td></tr>`;
    for (const r of rows) {
      const isNew = String(r.reservation_id) === String(m.highlight);
      html += `<tr class="${isNew ? "new" : ""}"><td class="num">${r.time}</td><td>${esc(r.name)}</td>` +
        `<td class="num">${r.party_size}</td><td>${r.table_id}</td><td class="num">${r.reservation_id}</td></tr>`;
    }
  }
  html += "</table></div>";
  if (m.handoffs.length) {
    html += m.handoffs.map((h) => `<div class="handoff">Staff handoff #${h.handoff_id}: ${esc(h.reason)}</div>`).join("");
  }
  $("bookings").innerHTML = html;
  const row = $("bookings").querySelector("tr.new");
  if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
}

function esc(t) {
  return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ----------------------------------------------------------------- wiring
$("mic").addEventListener("click", async () => {
  if (s.live) {
    stopCall();
    return;
  }
  try {
    await startMic();
  } catch (err) {
    setStatus("Microphone blocked. You can still type below.");
  }
});

function sendTyped(text) {
  if (!text.trim()) return;
  if (!s.playCtx) s.playCtx = new AudioContext();
  s.playCtx.resume();
  if (isPlaying() || s.agentState !== "idle") bargeIn();
  $("stop").hidden = false;
  send({ type: "text", text });
}

$("type-form").addEventListener("submit", (e) => {
  e.preventDefault();
  sendTyped($("type-input").value);
  $("type-input").value = "";
});

document.querySelectorAll(".try").forEach((b) => b.addEventListener("click", () => sendTyped(b.textContent)));

$("stop").addEventListener("click", stopCall);

$("reset").addEventListener("click", () => {
  if (s.playCtx) stopPlayback();
  send({ type: "reset" });
});

connect();
