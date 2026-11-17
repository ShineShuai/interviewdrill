import type { Env } from "./env.ts";
import { HttpError } from "./http.ts";

export type Provider = "workers-ai" | "anthropic" | "none";

export interface JsonTool {
  name: string;
  description: string;
  input_schema: object;
}

export interface JsonRequest {
  system: string;
  tool: JsonTool;
  prompt: string;
  maxTokens: number;
}

// The first model is the stronger one; it may need the paid plan, so the smaller one follows.
const DEFAULT_MODELS =
  "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/meta/llama-3.1-8b-instruct-fast";

interface AiRunner {
  run(model: string, input: unknown): Promise<unknown>;
}

/** Which grader this deployment has. "none" means the user grades themselves. */
export function providerOf(env: Env): Provider {
  const want = (env.LLM ?? "workers-ai").trim().toLowerCase();
  if (want === "anthropic") return env.ANTHROPIC_API_KEY ? "anthropic" : "none";
  if (want === "workers-ai") return env.AI ? "workers-ai" : "none";
  return "none";
}

/** Parse the JSON object in a model reply, tolerating code fences and surrounding prose. */
export function extractJson(text: string): Record<string, unknown> {
  let body = text.trim();
  const fenced = /```(?:json)?\s*([\s\S]*?)```/.exec(body);
  if (fenced) body = fenced[1].trim();
  const parse = (s: string): unknown => JSON.parse(s);
  let value: unknown;
  try {
    value = parse(body);
  } catch {
    const start = body.indexOf("{");
    const end = body.lastIndexOf("}");
    if (start < 0 || end <= start) throw new SyntaxError("no JSON object in the reply");
    value = parse(body.slice(start, end + 1));
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new SyntaxError("the reply is not a JSON object");
  }
  return value as Record<string, unknown>;
}

const objectFrom = (v: unknown): Record<string, unknown> =>
  typeof v === "string" ? extractJson(v) : extractJson(JSON.stringify(v ?? null));

async function viaWorkersAi(env: Env, req: JsonRequest): Promise<Record<string, unknown>> {
  const models = (env.LLM_MODEL ?? DEFAULT_MODELS).split(",").map((m) => m.trim()).filter(Boolean);
  let failure: unknown;
  for (const model of models) {
    let out: unknown;
    try {
      out = await (env.AI as unknown as AiRunner).run(model, {
        messages: [
          { role: "system", content: req.system },
          { role: "user", content: req.prompt },
        ],
        response_format: { type: "json_schema", json_schema: req.tool.input_schema },
        max_tokens: req.maxTokens,
        temperature: 0,
      });
    } catch (e) {
      failure = e; // not on this plan, over the daily allowance, or the model is down
      continue;
    }
    return objectFrom((out as { response?: unknown } | null)?.response);
  }
  console.error("workers ai failed", failure);
  throw new HttpError(502, "grader unavailable");
}

async function viaAnthropic(env: Env, req: JsonRequest): Promise<Record<string, unknown>> {
  let res: Response;
  try {
    res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": env.ANTHROPIC_API_KEY as string,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: env.ANTHROPIC_MODEL ?? "claude-sonnet-5-5",
        max_tokens: req.maxTokens,
        system: req.system,
        tools: [req.tool],
        tool_choice: { type: "tool", name: req.tool.name },
        messages: [{ role: "user", content: req.prompt }],
      }),
      signal: AbortSignal.timeout(45_000),
    });
  } catch {
    throw new HttpError(502, "grader unreachable");
  }
  if (!res.ok) {
    console.error("anthropic", res.status, await res.text());
    throw new HttpError(502, "grader error");
  }
  const data = (await res.json()) as {
    content: { type: string; input?: Record<string, unknown> }[];
  };
  const block = data.content.find((b) => b.type === "tool_use");
  if (!block?.input) throw new HttpError(502, "grader returned no grade");
  return block.input;
}

/** Ask the configured model for a JSON object; an unparsable reply is retried once. */
export async function callJson(env: Env, req: JsonRequest): Promise<Record<string, unknown>> {
  const provider = providerOf(env);
  if (provider === "none") throw new HttpError(409, "no grader configured: grade yourself");
  let invalid: unknown;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      return provider === "anthropic"
        ? await viaAnthropic(env, req)
        : await viaWorkersAi(env, req);
    } catch (e) {
      if (e instanceof HttpError) throw e;
      invalid = e;
    }
  }
  console.error("grader reply was not usable", invalid);
  throw new HttpError(502, "grader returned an invalid reply");
}
