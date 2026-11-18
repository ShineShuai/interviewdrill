// Dedicated worker: loads Pyodide and the harness, then runs one program per message.
// The network is switched off before any user code runs.
importScripts("/pyodide/pyodide.js");

const NETWORK = ["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "importScripts"];
let runCase;

(async () => {
  try {
    const py = await loadPyodide({ indexURL: "/pyodide/" });
    const harness = await (await fetch("/py/harness.py")).text();
    py.runPython(harness);
    runCase = py.globals.get("run_case");
    // fetch and friends live on the prototype of the worker global: shadow them along the whole
    // chain, or the program reaches the original via Object.getPrototypeOf(self).fetch.
    const blocked = function () {
      throw new Error("the network is disabled in the runner");
    };
    for (let o = self; o; o = Object.getPrototypeOf(o)) {
      for (const name of NETWORK) {
        if (Object.getOwnPropertyDescriptor(o, name)?.configurable) {
          Object.defineProperty(o, name, { value: blocked, writable: false, configurable: false });
        }
      }
    }
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
