"""One entry point for language-model calls, with several providers.

DRILL_LLM selects the provider. Default: anthropic if ANTHROPIC_API_KEY is set, openai if
DRILL_LLM_URL is set, otherwise manual.
  anthropic  Anthropic API: ANTHROPIC_API_KEY, model DRILL_MODEL (default claude-sonnet-5-5),
             ANTHROPIC_BASE_URL overrides the endpoint.
  openai     Any OpenAI-compatible chat endpoint: DRILL_LLM_URL (for Ollama:
             http://localhost:11434/v1), DRILL_LLM_MODEL, optional DRILL_LLM_KEY (Groq,
             OpenRouter, Google AI Studio, ...).
  manual     No API: the prompt is printed (and copied with pbcopy on macOS); paste it into
             claude.ai, then paste the JSON reply back and end with a line saying END.
  none       No model: callers fall back to self-grading or stop with a message.
Every reply is checked against the tool's JSON schema; an invalid reply is retried once.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
JSON_ONLY = "Reply with ONLY a JSON object that matches this JSON Schema (no prose, no fences):\n"


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- helpers ---
def check(value, schema: dict, path: str = "$") -> list[str]:
    """Validate against the JSON Schema subset our tools use; returns error messages."""
    t = schema.get("type")
    simple = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = t not in simple or isinstance(value, simple[t])
    if not ok:
        return [f"{path}: expected {t}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if t == "object":
        errors += [f"{path}.{k}: missing" for k in schema.get("required", []) if k not in value]
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += check(value[key], sub, f"{path}.{key}")
    if t == "array":
        for i, item in enumerate(value):
            errors += check(item, schema.get("items", {}), f"{path}[{i}]")
    return errors


def extract_json(text: str):
    """Parse the JSON object in a model reply, tolerating code fences and surrounding prose."""
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found in the reply") from None
        return json.loads(body[start:end + 1])


def _post(url: str, headers: dict, body: dict, timeout: float = 180):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise LLMError(f"HTTP {exc.code}: {detail}", exc.code) from exc
    except OSError as exc:
        raise LLMError(f"cannot reach {url}: {exc}") from exc
    except ValueError as exc:
        raise LLMError("the endpoint did not return JSON") from exc


# --- providers: (system, tool, prompt, max_tokens) -> dict ---
def anthropic_model() -> str:
    return (os.environ.get("DRILL_GEN_MODEL") or os.environ.get("DRILL_MODEL")
            or DEFAULT_ANTHROPIC_MODEL)


def _anthropic(system, tool, prompt, max_tokens):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    data = _post(f"{base}/v1/messages",
                 {"x-api-key": key, "anthropic-version": "2023-06-01"},
                 {"model": anthropic_model(), "max_tokens": max_tokens, "system": system,
                  "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]},
                  "messages": [{"role": "user", "content": prompt}]})
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if block is None:
        raise LLMError("the reply had no tool call")
    return block["input"]


def _openai(system, tool, prompt, max_tokens):
    url = os.environ.get("DRILL_LLM_URL", "").rstrip("/")
    model = os.environ.get("DRILL_LLM_MODEL", "")
    if not (url and model):
        raise LLMError("set DRILL_LLM_URL and DRILL_LLM_MODEL (and DRILL_LLM_KEY if needed)")
    key = os.environ.get("DRILL_LLM_KEY", "")
    ask = f"{prompt}\n\n{JSON_ONLY}{json.dumps(tool['input_schema'])}"
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": ask}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        data = _post(f"{url}/chat/completions", headers, body)
    except LLMError as exc:
        if exc.status != 400:
            raise
        del body["response_format"]  # some servers do not know JSON mode
        data = _post(f"{url}/chat/completions", headers, body)
    try:
        return extract_json(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("unexpected reply shape from the endpoint") from exc


def _manual(system, tool, prompt, max_tokens):
    text = f"{system}\n\n{prompt}\n\n{JSON_ONLY}{json.dumps(tool['input_schema'])}"
    bar = "=" * 72
    print(f"\n{bar}\nPaste this into claude.ai (a new chat):\n{bar}\n{text}\n{bar}")
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        print("(copied to the clipboard)")
    print("Paste Claude's reply here, then a line with only END (or press Ctrl-D):")
    lines = []
    for line in sys.stdin:
        if line.strip() == "END":
            break
        lines.append(line)
    return extract_json("".join(lines))


def _none(system, tool, prompt, max_tokens):
    raise LLMError("no language model configured (see llm.py: DRILL_LLM, DRILL_LLM_URL, ...)")


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "manual": _manual, "none": _none}


# --- public API ---
def explicit() -> str:
    """The provider the user asked for with DRILL_LLM, or ''."""
    name = os.environ.get("DRILL_LLM", "").strip().lower()
    if name and name not in PROVIDERS:
        raise LLMError(f"DRILL_LLM must be one of {', '.join(sorted(PROVIDERS))}")
    return name


def provider_name() -> str:
    if explicit():
        return explicit()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "openai" if os.environ.get("DRILL_LLM_URL") else "manual"


def automatic() -> bool:
    """True if a model answers without a human in the loop."""
    return provider_name() in ("anthropic", "openai")


def describe() -> str:
    name = provider_name()
    if name == "anthropic":
        return f"anthropic:{anthropic_model()}"
    if name == "openai":
        return f"openai:{os.environ.get('DRILL_LLM_MODEL', '?')}"
    return "manual (claude.ai)" if name == "manual" else "none"


def call_tool(system: str, tool: dict, prompt: str, max_tokens: int = 2000) -> dict:
    """Ask the configured model for a JSON object matching tool['input_schema']."""
    provider = PROVIDERS[provider_name()]
    problem = ""
    for _ in range(2):
        text = prompt if not problem else (
            f"{prompt}\n\nYour previous reply was invalid: {problem}\n"
            "Return a corrected JSON object.")
        try:
            out = provider(system, tool, text, max_tokens)
        except ValueError as exc:  # unparsable reply
            problem = str(exc)
            continue
        errors = check(out, tool["input_schema"])
        if not errors:
            return out
        problem = "; ".join(errors[:5])
    raise LLMError(f"the model's reply did not match the schema: {problem}")
