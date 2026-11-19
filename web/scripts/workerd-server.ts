// Boot the Worker in workerd (see workerd.ts), print "ready" and run until SIGTERM or until
// stdin closes (the parent is gone). Used by the browser tests in test_web_ui.py.
// Usage: node scripts/workerd-server.ts PORT
import { boot } from "./workerd.ts";

const stop = await boot(Number(process.argv[2] ?? 8798));
const quit = (): void => {
  stop();
  process.exit(0);
};
process.on("SIGTERM", quit).on("SIGINT", quit);
process.stdin.on("end", quit).resume();
console.log("ready");
