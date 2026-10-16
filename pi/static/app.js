// Protogen web UI logic.
// Polls /api/status every 200ms and reflects state into the DOM.
// User actions are debounced and tagged via `touched` so sync doesn't override
// freshly-changed values for ~2s after interaction.

const $ = id => document.getElementById(id);
const fmtT = s => s < 60
  ? s.toFixed(0) + "s"
  : s < 3600
    ? Math.floor(s/60) + "m " + Math.floor(s%60) + "s"
    : Math.floor(s/3600) + "h";

const touched = {};
const touch = k => { touched[k] = Date.now(); };
const notTouching = k => !touched[k] || (Date.now() - touched[k] > 2000);

// Spectrum bars
for (let i = 0; i < 16; i++) {
  const b = document.createElement("div");
  b.className = "bar";
  b.style.height = "0%";
  $("bars").appendChild(b);
}
const bars = document.querySelectorAll(".bar");

// --- API helpers ---
async function apiSet(patch){
  try {
    await fetch("/api/set", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(patch),
    });
  } catch(e){ console.error("apiSet", e); }
}
async function sendCmd(c, v){
  await fetch("/api/cmd", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({cmd: c, value: v}),
  });
}
async function cmd(c){ await sendCmd(c, 0); }
window.cmd = cmd;

const debounce = (fn, ms) => {
  let h;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
};
const sendM  = debounce(v => sendCmd("M", v), 100);
const sendG  = debounce(v => sendCmd("G", v), 100);
const sendA  = debounce(v => sendCmd("A", v), 100);
const sendSm = debounce(v => sendCmd("S", v), 100);

// --- Mode switching ---
function showSection(m){
  ["emotion", "image", "eq"].forEach(x =>
    $("sec-" + x).classList.toggle("hidden", x !== m));
  document.querySelectorAll(".mode").forEach(b =>
    b.classList.toggle("active", b.dataset.m === m));
}
function switchMode(m){
  touch("mode");
  showSection(m);
  apiSet({mode: m});
}
document.querySelectorAll(".mode").forEach(b =>
  b.onclick = () => switchMode(b.dataset.m));

// --- Effect buttons ---
document.querySelectorAll("[data-eff]").forEach(b => b.onclick = e => {
  touch("effect");
  document.querySelectorAll("[data-eff]").forEach(x =>
    x.classList.toggle("active", x === e.currentTarget));
  apiSet({effect: e.currentTarget.dataset.eff});
});

// --- Color picker ---
function applyColor(hex){
  touch("color");
  $("colorV").textContent = hex.toUpperCase();
  const c = [
    parseInt(hex.slice(1,3), 16),
    parseInt(hex.slice(3,5), 16),
    parseInt(hex.slice(5,7), 16),
  ];
  apiSet({color: c, effect: "solid"});
  document.querySelectorAll("[data-eff]").forEach(x =>
    x.classList.toggle("active", x.dataset.eff === "solid"));
}
$("color").oninput  = e => applyColor(e.target.value);
$("color").onchange = e => applyColor(e.target.value);

// --- Display sliders ---
$("bright").oninput = e => {
  touch("bright");
  $("brightV").textContent = e.target.value + "%";
  apiSet({brightness: +e.target.value});
};
$("rot").onclick = () => {
  touch("rot");
  $("rot").classList.toggle("on");
  apiSet({rotate_180: $("rot").classList.contains("on")});
};
$("mir").onclick = () => {
  touch("mir");
  $("mir").classList.toggle("on");
  apiSet({mirror: $("mir").classList.contains("on")});
};

// --- EQ sliders ---
$("margin").oninput = e => { touch("margin"); $("marginV").textContent = e.target.value + " dB"; sendM(+e.target.value); };
$("gain").oninput   = e => { touch("gain");   $("gainV").textContent   = e.target.value;        sendG(+e.target.value); };
$("amp").oninput    = e => { touch("amp");    $("ampV").textContent    = e.target.value + "×";  sendA(+e.target.value); };
$("smooth").oninput = e => { touch("smooth"); $("smoothV").textContent = e.target.value + "%";  sendSm(+e.target.value); };

// --- Emotion list ---
async function loadEmotions(){
  const list = await (await fetch("/api/emotions")).json();
  $("emos").innerHTML = list.map(e =>
    `<button class="btn big" data-em="${e.id}" title="${e.label}">${e.emoji}</button>`
  ).join("");
  $("emos").querySelectorAll("[data-em]").forEach(b => b.onclick = e => {
    touch("emotion");
    document.querySelectorAll("[data-em]").forEach(x =>
      x.classList.toggle("active", x === e.currentTarget));
    apiSet({emotion: e.currentTarget.dataset.em});
  });
}

// --- File upload ---
const drop = $("dropzone");
const fileInp = $("fileInp");
async function uploadFile(file){
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  $("dropLab").innerHTML = "Uploading " + file.name + "...";
  try {
    const r = await fetch("/api/upload", {method: "POST", body: fd});
    const j = await r.json();
    if (j.ok) {
      $("dropLab").innerHTML = "✓ " + file.name + "<br>Click to replace";
      $("preview").style.display = "block";
      $("previewImg").src = "/api/image?t=" + Date.now();
      touch("mode");
      showSection("image");
    } else {
      $("dropLab").innerHTML = "❌ " + (j.error || "upload failed");
    }
  } catch(e) {
    $("dropLab").innerHTML = "❌ " + e.message;
  }
}
fileInp.addEventListener("change", e => uploadFile(e.target.files[0]));
drop.addEventListener("dragover",  e => { e.preventDefault(); drop.classList.add("drag"); });
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop",      e => {
  e.preventDefault();
  drop.classList.remove("drag");
  if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
});

// --- Poll loop ---
async function tick(){
  try {
    const s = await (await fetch("/api/status")).json();
    $("dot").className = s.connected ? "dot on" : "dot";

    if (notTouching("mode"))    showSection(s.mode);
    if (notTouching("emotion"))
      document.querySelectorAll("[data-em]").forEach(b =>
        b.classList.toggle("active", b.dataset.em === s.emotion));
    if (notTouching("effect"))
      document.querySelectorAll("[data-eff]").forEach(b =>
        b.classList.toggle("active", b.dataset.eff === s.effect));
    if (notTouching("color")) {
      const hex = "#" + s.color.map(c => c.toString(16).padStart(2, "0")).join("");
      $("color").value = hex;
      $("colorV").textContent = hex.toUpperCase();
    }
    if (notTouching("bright")) { $("bright").value = s.brightness; $("brightV").textContent = s.brightness + "%"; }
    if (notTouching("rot"))    $("rot").classList.toggle("on", s.rotate_180);
    if (notTouching("mir"))    $("mir").classList.toggle("on", s.mirror);

    $("eqStatus").textContent = s.connected ? "ONLINE" : "OFFLINE";
    $("eqPort").textContent   = s.port || "—";
    $("eqRate").textContent   = s.eq_rate_hz + " Hz";
    $("eqUp").textContent     = s.esp_uptime_s ? fmtT(s.esp_uptime_s) : "—";
    for (let i = 0; i < 16; i++) bars[i].style.height = (s.bars[i] / 255 * 100) + "%";

    if (notTouching("margin")) { $("margin").value = s.noise_margin; $("marginV").textContent = s.noise_margin + " dB"; }
    if (notTouching("gain"))   { $("gain").value   = s.db_gain;       $("gainV").textContent   = s.db_gain; }
    if (notTouching("amp"))    { $("amp").value    = s.amp_mult;      $("ampV").textContent    = s.amp_mult + "×"; }
    if (notTouching("smooth")) { $("smooth").value = s.smoothing;     $("smoothV").textContent = s.smoothing + "%"; }

    if (s.has_image && !$("previewImg").src.includes("/api/image")) {
      $("preview").style.display = "block";
      $("previewImg").src = "/api/image?t=" + Date.now();
    }
  } catch(e) {}
}
(async () => {
  await loadEmotions();
  setInterval(tick, 200);
  tick();
})();
