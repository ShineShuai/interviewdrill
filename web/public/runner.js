// Browser adapter: one dedicated Worker per Python runtime, wrapped as { run, kill }.
export function spawnWorker() {
  return new Promise((resolve, reject) => {
    const worker = new Worker("/py/worker.js");
    const pending = new Map();
    let nextId = 0;
    const fail = (err) => {
      worker.terminate(); // do not leave a half-initialized runtime running
      reject(err);
    };
    worker.onmessage = ({ data }) => {
      if (data.type === "ready") resolve({ run, kill });
      else if (data.type === "error") fail(new Error(data.message));
      else if (data.type === "result") pending.get(data.id)?.(data);
    };
    worker.onerror = (e) => fail(new Error(e.message || "worker failed to load"));
    const run = (code, input) =>
      new Promise((res) => {
        const id = nextId++;
        pending.set(id, res);
        worker.postMessage({ type: "run", id, code, input });
      });
    const kill = () => {
      worker.terminate();
      pending.clear();
    };
  });
}
