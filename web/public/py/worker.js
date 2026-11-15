// Dedicated worker: loads Pyodide and the harness, then runs one program per message.
// The network is switched off before any user code runs.
importScripts("/pyodide/pyodide.js");

let runCase;

(async () => {
  try {
    const py = await loadPyodide({ indexURL: "/pyodide/" });
    const harness = await (await fetch("/py/harness.py")).text();
    py.runPython(harness);
    runCase = py.globals.get("run_case");
    const blocked = () => {
      throw new Error("the network is disabled in the runner");
    };
    self.fetch = blocked;
    self.XMLHttpRequest = blocked;
    self.WebSocket = blocked;
    self.importScripts = blocked;
    postMessage({ type: "ready" });
  } catch (e) {
    postMessage({ type: "error", message: String((e && e.message) || e) });
  }
})();

onmessage = ({ data }) => {
  if (data.type !== "run" || !runCase) return;
  const t0 = performance.now();
  let res;
  try {
    res = JSON.parse(runCase(data.code, data.input));
  } catch (e) {
    res = { rc: 1, out: "", err: String(e) };
  }
  postMessage({ type: "result", id: data.id, ...res, secs: (performance.now() - t0) / 1000 });
};
