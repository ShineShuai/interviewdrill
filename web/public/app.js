// drill web client: no build step, no inline script/style (strict CSP), textContent only.
const view = document.querySelector("#view");

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-Drill": "1", ...opts.headers } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error ?? res.statusText);
    err.status = res.status;
    throw err;
  }
  return data;
}

const jsonPost = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

let grader = "none"; // who grades answers: "workers-ai", "anthropic", or "none" (you do)

const errText = (e) =>
  e instanceof TypeError ? "Offline or session expired. Reload." : e.message;

async function show(page, ...args) {
  try {
    await page(...args);
  } catch (e) {
    view.replaceChildren(
      h("p", { class: "err" }, errText(e)),
      h("button", { onclick: () => location.reload() }, "Reload"),
    );
  }
}

function speak(text) {
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  speechSynthesis.speak(u);
}

// --- voice: record, resample to 16 kHz mono WAV in the browser, transcribe on the server ---
async function toWav(blob) {
  const ctx = new AudioContext();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  await ctx.close();
  const rate = 16000;
  const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const tag = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  tag(0, "RIFF");
  out.setUint32(4, 36 + pcm.length * 2, true);
  tag(8, "WAVEfmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); // PCM
  out.setUint16(22, 1, true); // mono
  out.setUint32(24, rate, true);
  out.setUint32(28, rate * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  tag(36, "data");
  out.setUint32(40, pcm.length * 2, true);
  pcm.forEach((s, i) => out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Blob([out], { type: "audio/wav" });
}

let recorder = null;
async function toggleMic(btn, box, msg) {
  if (recorder) return recorder.stop();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    const mr = new MediaRecorder(stream);
    mr.ondataavailable = (e) => chunks.push(e.data);
    mr.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      recorder = null;
      btn.textContent = "Transcribing…";
      try {
        const wav = await toWav(new Blob(chunks, { type: mr.mimeType }));
        const { text } = await api("/api/stt", {
          method: "POST",
          headers: { "Content-Type": "audio/wav" },
          body: wav,
        });
        box.value = `${box.value} ${text}`.trim();
      } catch (e) {
        msg.textContent = errText(e);
      } finally {
        btn.textContent = "Record";
      }
    };
    recorder = mr;
    mr.start();
    btn.textContent = "Stop";
    setTimeout(() => mr.state === "recording" && mr.stop(), 120_000);
  } catch (e) {
    msg.textContent = errText(e);
  }
}

// --- pages ---
async function home() {
  const s = await api("/api/status");
  grader = s.grader;
  const topic = h(
    "select",
    {},
    h("option", { value: "" }, `all (${s.due})`),
    ...s.topics.map((t) => h("option", { value: t.topic }, `${t.topic} (${t.due})`)),
  );
  const count = h("input", { type: "number", min: 1, max: 20, value: 5 });
  view.replaceChildren(
    h("h1", {}, `${s.due} due`),
    h("p", { class: "dim" }, `${s.today.n} answered today · streak ${s.streak} d`),
    h("p", { class: "dim" }, grader === "none" ? "You grade yourself" : `Graded by ${grader}`),
    h("label", {}, "Topic", topic),
    h("label", {}, "Questions", count),
    h(
      "button",
      { class: "primary", onclick: () => show(drill, topic.value, Number(count.value) || 5) },
      "Start",
    ),
  );
}

async function drill(topic, n) {
  const q = new URLSearchParams({ n });
  if (topic) q.set("topic", topic);
  const { cards } = await api(`/api/next?${q}`);
  if (!cards.length) {
    view.replaceChildren(
      h("p", {}, "Nothing due."),
      h("button", { onclick: () => show(home) }, "Back"),
    );
    return;
  }
  for (const [i, card] of cards.entries()) {
    const result = await ask(card, `${i + 1}/${cards.length}`);
    if (result) await review(result, i + 1 === cards.length);
  }
  await home();
}

function ask(card, label) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    // Exercises (bug:, problem:) have a one-line instruction followed by code or a statement.
    const [head, ...rest] = card.question.split("\n");
    const body = rest.join("\n").trim();
    const box = h("textarea", {
      rows: body ? 12 : 8,
      class: body ? "mono" : null,
      spellcheck: body ? "false" : null,
      placeholder: "Answer; Ctrl+Enter submits",
    });
    const msg = h("p", { class: "err" });
    const mic = h("button", { onclick: () => toggleMic(mic, box, msg) }, "Record");
    const send = h("button", { class: "primary" }, "Submit");
    const secs = () => (performance.now() - t0) / 1000;
    const mine = h("button", {}, "Grade myself");
    mine.addEventListener("click", async () => {
      mine.disabled = true;
      msg.textContent = "";
      try {
        resolve(await selfCheck(card, box.value, secs()));
      } catch (e) {
        msg.textContent = errText(e);
        mine.disabled = false;
      }
    });
    const problem = card.id.startsWith("problem:");
    const panel = h("div", { class: "panel" });
    const reveal = h("input", { type: "checkbox" });
    const run = h("button", {}, "Run as Python");
    run.addEventListener("click", () => runProblem(card, box, panel, run, reveal.checked));
    const skip = h("button", { onclick: () => resolve(null) }, "Next");
    const submit = async () => {
      if (grader === "none") return mine.click();
      if (!box.value.trim()) return;
      send.disabled = true;
      msg.textContent = "";
      try {
        resolve(await api("/api/attempt", jsonPost({
          card_id: card.id, answer: box.value, secs: secs(),
        })));
      } catch (e) {
        msg.textContent = `${errText(e)} You can still use "Grade myself".`;
        send.disabled = false;
      }
    };
    send.addEventListener("click", submit);
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
    });
    view.replaceChildren(
      h("p", { class: "dim" }, `${label} · ${card.topic}`),
      h("h2", {}, head),
      body ? h("pre", { class: "q" }, body) : null,
      h(
        "div",
        { class: "row" },
        h("button", { onclick: () => speak(head) }, "Read aloud"),
        mic,
      ),
      box,
      problem
        ? h("div", { class: "row" }, run, h("label", { class: "inline" }, reveal, "reveal"))
        : null,
      h("div", { class: "row" }, grader === "none" ? null : send, mine),
      problem ? skip : null,
      problem ? panel : null,
      msg,
    );
    box.focus();
  });
}

// No model needed: compare your answer with the reference and tick what it covered.
async function selfCheck(card, answer, secs) {
  const { ref, points } = await api("/api/reveal", jsonPost({ card_id: card.id }));
  return new Promise((resolve) => {
    const ticks = points.map(() => h("input", { type: "checkbox" }));
    const msg = h("p", { class: "err" });
    const save = h("button", { class: "primary" }, "Save");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const hit = ticks.flatMap((t, i) => (t.checked ? [i] : []));
        resolve(await api("/api/self-grade", jsonPost({ card_id: card.id, answer, secs, hit })));
      } catch (e) {
        msg.textContent = errText(e);
        save.disabled = false;
      }
    });
    view.replaceChildren(
      h("h2", {}, "Check yourself"),
      h("pre", {}, ref),
      h("p", { class: "dim" }, "Tick what your answer covered:"),
      ...points.map((p, i) => h("label", { class: "inline" }, ticks[i], p)),
      save,
      msg,
    );
  });
}

const detailText = (d) => `input:\n${d.input}\nexpected: ${d.expected}\ngot: ${d.got}`;

// Problems: paste a Python solution (stdin -> stdout) and run the verified tests in the browser.
async function runProblem(card, box, panel, btn, reveal) {
  if (!box.value.trim()) return;
  btn.disabled = true;
  const note = (text, cls = "dim") => panel.replaceChildren(h("p", { class: cls }, text));
  try {
    note("Loading tests and the Python runtime (first run downloads about 10 MB)…");
    const res = await fetch(`/problems/${card.id.slice("problem:".length)}.json`);
    if (!res.ok) {
      throw new Error("no tests for this problem; run `npm run problems` and redeploy");
    }
    const spec = await res.json();
    const { runTests, summarize } = await import("/runner-core.js");
    const { spawnWorker } = await import("/runner.js");
    const t0 = performance.now();
    const results = await runTests({
      code: box.value, tests: spec.tests, timeLimitS: spec.time_limit, spawn: spawnWorker,
      onProgress: (i, n) => note(`Running test ${i}/${n}…`),
    });
    const s = summarize(results, reveal);
    const saved = await api("/api/problem/result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        card_id: card.id, passed: s.passed, total: s.total, secs: (performance.now() - t0) / 1000,
      }),
    });
    const f = s.failure;
    panel.replaceChildren(
      h("h3", {}, `${s.passed}/${s.total} passed`),
      h("p", {}, s.groups.map((g) => `${g.group} ${g.ok}/${g.n}`).join("   ")),
      f
        ? h("p", { class: "err" }, `first failure: ${f.name} ${f.verdict} (${f.secs.toFixed(2)}s)`)
        : null,
      f && f.err ? h("pre", {}, f.err) : null,
      f && f.detail
        ? h("pre", {}, detailText(f.detail))
        : f
          ? h("p", { class: "dim" }, "hidden test: tick “reveal” and run again to see it")
          : null,
      h("p", { class: "dim" }, `next review in ${Math.round(saved.interval)} d`),
    );
  } catch (e) {
    note(`${errText(e)} (Python runner: did you run npm run vendor, then deploy?)`, "err");
  } finally {
    btn.disabled = false;
  }
}

function review({ grade: g, score, interval, ref }, last) {
  return new Promise((resolve) => {
    const bar = (name, v) => h("label", {}, `${name} ${v}/5`, h("progress", { max: 5, value: v }));
    const list = (title, items) =>
      items.length
        ? h("div", {}, h("h3", {}, title), h("ul", {}, ...items.map((t) => h("li", {}, t))))
        : null;
    view.replaceChildren(
      h("h2", {}, `${score.toFixed(1)} / 5`),
      bar("correct", g.correct),
      bar("depth", g.depth),
      bar("clarity", g.clarity),
      list("Missed", g.missed),
      list("Errors", g.errors.map((e) => `"${e.quote}" — ${e.why}`)),
      g.followup ? h("p", {}, h("b", {}, "Follow-up: "), g.followup) : null,
      h("details", {}, h("summary", {}, "Reference"), h("pre", {}, ref)),
      h("p", { class: "dim" }, `next review in ${Math.round(interval)} d`),
      h("button", { class: "primary", onclick: () => resolve() }, last ? "Finish" : "Next"),
    );
  });
}

async function report() {
  const r = await api("/api/report?days=14");
  const f = (x) => Number(x).toFixed(1);
  const head = ["topic", "n", "score", "corr", "depth", "clar"];
  const table = h(
    "table",
    {},
    h("tr", {}, ...head.map((c) => h("th", {}, c))),
    ...r.topics.map((t) =>
      h(
        "tr",
        {},
        ...[t.topic, t.n, f(t.score), f(t.correct), f(t.depth), f(t.clarity)].map((v) =>
          h("td", {}, String(v)),
        ),
      ),
    ),
  );
  view.replaceChildren(
    h("h1", {}, `Last ${r.days} days`),
    r.topics.length ? table : h("p", {}, "No attempts yet."),
    h("h3", {}, "Weakest cards"),
    h(
      "ul",
      {},
      ...r.weakest.map((w) => h("li", {}, `${f(w.score)} · ${w.question.split("\n")[0]}`)),
    ),
    h("h3", {}, "Daily score"),
    ...r.trend.map((d) =>
      h("label", {}, `${d.day.slice(5)} (${d.n})`, h("progress", { max: 5, value: d.score })),
    ),
    h("p", {}, h("a", { href: "/api/export", download: "drill-export.json" }, "Export data")),
  );
}

for (const btn of document.querySelectorAll("nav button")) {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b === btn));
    show(btn.dataset.tab === "report" ? report : home);
  });
}
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
show(home);
