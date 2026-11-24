import { PipecatClient } from "https://cdn.jsdelivr.net/npm/@pipecat-ai/client-js@1.16.0/+esm";
import { SmallWebRTCTransport } from "https://cdn.jsdelivr.net/npm/@pipecat-ai/small-webrtc-transport@1.11.2/+esm";

// ---------- elements ----------
const callBtn = document.getElementById("callBtn");
const overlay = document.getElementById("phoneOverlay");
const avatar = document.getElementById("avatar");
const wave = document.getElementById("wave");
const callStatus = document.getElementById("callStatus");
const callTimer = document.getElementById("callTimer");
const transcriptEl = document.getElementById("transcript");
const muteBtn = document.getElementById("muteBtn");
const endBtn = document.getElementById("endBtn");
const doctorsEl = document.getElementById("doctors");
const appointmentsEl = document.getElementById("appointments");
const noAppointments = document.getElementById("noAppointments");

let pc = null;
let timerId = null;
let startedAt = null;
let muted = false;

// ---------- voice client ----------
function setCallState(state) {
  if (state === "connecting") {
    callStatus.textContent = "Calling…";
    avatar.classList.add("ringing");
    wave.classList.remove("live");
  } else if (state === "connected") {
    callStatus.textContent = "Connected";
    avatar.classList.remove("ringing");
    startedAt = Date.now();
    timerId = setInterval(tick, 1000);
  } else if (state === "ended") {
    callStatus.textContent = "Call ended";
    avatar.classList.remove("ringing");
    wave.classList.remove("live");
    clearInterval(timerId);
    setTimeout(() => overlay.classList.add("hidden"), 1200);
  }
}

function tick() {
  const s = Math.floor((Date.now() - startedAt) / 1000);
  callTimer.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

async function startCall() {
  transcriptEl.innerHTML = "";
  callTimer.textContent = "00:00";
  overlay.classList.remove("hidden");
  setCallState("connecting");

  pc = new PipecatClient({
    transport: new SmallWebRTCTransport({ webrtcUrl: "/api/offer" }),
    enableMic: true,
    callbacks: {
      onConnected: () => setCallState("connected"),
      onDisconnected: () => setCallState("ended"),
      onBotStartedSpeaking: () => wave.classList.add("live"),
      onBotStoppedSpeaking: () => wave.classList.remove("live"),
      onUserStartedSpeaking: () => wave.classList.add("live"),
      onUserStoppedSpeaking: () => wave.classList.remove("live"),
      onTrackStarted: (track, participant) => {
        if (!participant?.local && track.kind === "audio") {
          const audio = document.createElement("audio");
          audio.autoplay = true;
          audio.srcObject = new MediaStream([track]);
          document.body.appendChild(audio);
        }
      },
    },
  });

  try {
    await pc.connect();
  } catch (err) {
    console.error("connect failed", err);
    callStatus.textContent = "Could not connect. Is the server running?";
    setTimeout(() => overlay.classList.add("hidden"), 2000);
  }
}

async function endCall() {
  if (pc) {
    try { await pc.disconnect(); } catch {}
    pc = null;
  }
  setCallState("ended");
}

muteBtn.addEventListener("click", () => {
  if (!pc) return;
  muted = !muted;
  pc.enableMic(!muted);
  muteBtn.classList.toggle("muted", muted);
});
endBtn.addEventListener("click", endCall);
callBtn.addEventListener("click", startCall);

// ---------- live events from the server (transcript + booking updates) ----------
function addBubble(role, content) {
  const div = document.createElement("div");
  div.className = `bubble ${role === "user" ? "user" : "assistant"}`;
  div.textContent = content;
  transcriptEl.appendChild(div);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "transcript") addBubble(msg.role, msg.content);
    if (msg.type === "appointments") renderAppointments(msg.appointments, true);
  };
  ws.onclose = () => setTimeout(connectWS, 2000); // reconnect
}

// ---------- dashboard data ----------
async function loadDoctors() {
  const { doctors } = await (await fetch("/api/doctors")).json();
  doctorsEl.innerHTML = "";
  for (const d of doctors) {
    const card = document.createElement("div");
    card.className = "card";
    const days = d.upcoming
      .map(
        (u) => `
        <div class="day">
          <span class="when">${u.weekday.slice(0, 3)} ${u.date.slice(5)}</span>
          <span class="chips">${u.free_slots.map((t) => `<span class="chip">${t}</span>`).join("")}</span>
        </div>`
      )
      .join("");
    card.innerHTML = `
      <div class="doc-name">${d.name}</div>
      <div class="doc-spec">${d.speciality}</div>
      <div class="days">${days || '<div class="day"><span class="when">No free slots this week</span></div>'}</div>`;
    doctorsEl.appendChild(card);
  }
}

function renderAppointments(list, flash = false) {
  noAppointments.style.display = list.length ? "none" : "block";
  appointmentsEl.innerHTML = "";
  for (const a of list) {
    const tr = document.createElement("tr");
    if (flash) tr.className = "flash";
    tr.innerHTML = `
      <td>${a.doctor}<br><span style="color:var(--muted);font-size:12px">${a.speciality}</span></td>
      <td>${a.date}</td>
      <td>${a.time}</td>
      <td>${a.phone}</td>
      <td><span class="status ${a.status}">${a.status}</span></td>`;
    appointmentsEl.appendChild(tr);
  }
}

async function loadAppointments() {
  const { appointments } = await (await fetch("/api/appointments")).json();
  renderAppointments(appointments);
}

loadDoctors();
loadAppointments();
connectWS();
setInterval(loadDoctors, 60000); // keep slots fresh
