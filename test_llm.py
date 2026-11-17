import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import llm

TOOL = {
    "name": "grade",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 5},
            "tags": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["score", "tags"],
    },
}
GOOD = {"score": 4, "tags": ["x"]}


class Handler(BaseHTTPRequestHandler):
    log: list = []
    replies: list = []  # (status, body) consumed in order; the last one repeats

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Handler.log.append((self.path, {k.lower(): v for k, v in self.headers.items()},
                            json.loads(self.rfile.read(n))))
        status, body = Handler.replies.pop(0) if len(Handler.replies) > 1 else Handler.replies[0]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def anthropic_reply(obj):
    return (200, {"content": [{"type": "text", "text": "hi"},
                              {"type": "tool_use", "name": "grade", "input": obj}]})


def chat_reply(text):
    return (200, {"choices": [{"message": {"content": text}}]})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.log, Handler.replies = [], []
        drop = ("DRILL_LLM", "ANTHROPIC", "DRILL_MODEL", "DRILL_GEN_MODEL")
        keep = {k: v for k, v in os.environ.items() if not k.startswith(drop)}
        patcher = mock.patch.dict(os.environ, keep, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class HelpersTest(unittest.TestCase):
    def test_check_validates_the_schema_subset(self):
        s = TOOL["input_schema"]
        self.assertEqual(llm.check(GOOD, s), [])
        self.assertEqual(llm.check({"score": 7, "tags": []}, s), ["$.score: above 5"])
        self.assertEqual(llm.check({"score": -1, "tags": []}, s), ["$.score: below 0"])
        self.assertEqual(llm.check({"tags": []}, s), ["$.score: missing"])
        self.assertEqual(llm.check({"score": 1.5, "tags": []}, s), ["$.score: expected integer"])
        self.assertEqual(llm.check({"score": True, "tags": []}, s), ["$.score: expected integer"])
        self.assertEqual(llm.check({"score": 1, "tags": [1]}, s), ["$.tags[0]: expected string"])
        self.assertIn("must be one of", llm.check({**GOOD, "kind": "c"}, s)[0])
        self.assertEqual(llm.check([], s), ["$: expected object"])
        self.assertEqual(llm.check(2.5, {"type": "number"}), [])

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(llm.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(llm.extract_json('Sure!\n```json\n{"a": 1}\n```\nDone.'), {"a": 1})
        self.assertEqual(llm.extract_json('Here you go: {"a": {"b": 2}} hope it helps'),
                         {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            llm.extract_json("no json here")
        with self.assertRaises(ValueError):
            llm.extract_json('{"a": ')


class ProviderChoiceTest(Base):
    def test_default_is_manual_then_follows_the_configuration(self):
        self.assertEqual((llm.provider_name(), llm.automatic()), ("manual", False))
        os.environ["DRILL_LLM_URL"] = "http://x"
        self.assertEqual((llm.provider_name(), llm.automatic()), ("openai", True))
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self.assertEqual(llm.provider_name(), "anthropic")
        os.environ["DRILL_LLM"] = "none"
        self.assertEqual((llm.provider_name(), llm.automatic(), llm.explicit()),
                         ("none", False, "none"))
        os.environ["DRILL_LLM"] = "bogus"
        with self.assertRaisesRegex(llm.LLMError, "DRILL_LLM must be one of"):
            llm.provider_name()

    def test_describe(self):
        self.assertEqual(llm.describe(), "manual (claude.ai)")
        os.environ.update({"DRILL_LLM_URL": "http://x", "DRILL_LLM_MODEL": "llama3"})
        self.assertEqual(llm.describe(), "openai:llama3")
        os.environ.update({"ANTHROPIC_API_KEY": "k", "DRILL_MODEL": "claude-x"})
        self.assertEqual(llm.describe(), "anthropic:claude-x")
        os.environ["DRILL_LLM"] = "none"
        self.assertEqual(llm.describe(), "none")

    def test_none_raises(self):
        os.environ["DRILL_LLM"] = "none"
        with self.assertRaisesRegex(llm.LLMError, "no language model configured"):
            llm.call_tool("s", TOOL, "p")


class AnthropicTest(Base):
    def setUp(self):
        super().setUp()
        os.environ.update({"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": self.url,
                           "DRILL_MODEL": "claude-test"})

    def test_request_shape_and_forced_tool_call(self):
        Handler.replies = [anthropic_reply(GOOD)]
        self.assertEqual(llm.call_tool("be strict", TOOL, "the prompt", max_tokens=777), GOOD)
        (path, headers, body), = Handler.log
        self.assertEqual(path, "/v1/messages")
        self.assertEqual((headers["x-api-key"], headers["anthropic-version"]),
                         ("sk-test", "2023-06-01"))
        self.assertEqual((body["model"], body["max_tokens"], body["system"]),
                         ("claude-test", 777, "be strict"))
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "grade"})
        self.assertEqual(body["tools"], [TOOL])
        self.assertEqual(body["messages"], [{"role": "user", "content": "the prompt"}])

    def test_gen_model_overrides_the_default_model(self):
        os.environ["DRILL_GEN_MODEL"] = "claude-gen"
        Handler.replies = [anthropic_reply(GOOD)]
        llm.call_tool("s", TOOL, "p")
        self.assertEqual(Handler.log[0][2]["model"], "claude-gen")

    def test_schema_violation_is_retried_once_with_feedback(self):
        Handler.replies = [anthropic_reply({"score": 9, "tags": []}), anthropic_reply(GOOD)]
        self.assertEqual(llm.call_tool("s", TOOL, "p"), GOOD)
        second = Handler.log[1][2]["messages"][0]["content"]
        self.assertIn("previous reply was invalid: $.score: above 5", second)

    def test_two_invalid_replies_raise(self):
        Handler.replies = [anthropic_reply({"score": 9, "tags": []})]
        with self.assertRaisesRegex(llm.LLMError, "did not match the schema"):
            llm.call_tool("s", TOOL, "p")
        self.assertEqual(len(Handler.log), 2)

    def test_errors_are_explained(self):
        Handler.replies = [(401, {"error": "bad key"})]
        with self.assertRaisesRegex(llm.LLMError, "HTTP 401.*bad key") as ctx:
            llm.call_tool("s", TOOL, "p")
        self.assertEqual(ctx.exception.status, 401)
        Handler.replies = [(200, {"content": [{"type": "text", "text": "no tool"}]})]
        with self.assertRaisesRegex(llm.LLMError, "no tool call"):
            llm.call_tool("s", TOOL, "p")
        del os.environ["ANTHROPIC_API_KEY"]
        os.environ["DRILL_LLM"] = "anthropic"
        with self.assertRaisesRegex(llm.LLMError, "ANTHROPIC_API_KEY is not set"):
            llm.call_tool("s", TOOL, "p")


class OpenAITest(Base):
    def setUp(self):
        super().setUp()
        os.environ.update({"DRILL_LLM_URL": self.url + "/v1/", "DRILL_LLM_MODEL": "llama3",
                           "DRILL_LLM_KEY": "k123"})

    def test_chat_request_with_json_mode_and_schema_in_the_prompt(self):
        Handler.replies = [chat_reply('```json\n{"score": 3, "tags": []}\n```')]
        self.assertEqual(llm.call_tool("sys", TOOL, "ask", max_tokens=500),
                         {"score": 3, "tags": []})
        (path, headers, body), = Handler.log
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer k123")
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"]),
                         ("llama3", 500, 0))
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        user = body["messages"][1]["content"]
        self.assertTrue(user.startswith("ask\n\n"))
        self.assertIn(json.dumps(TOOL["input_schema"]), user)

    def test_retries_without_json_mode_when_the_server_rejects_it(self):
        Handler.replies = [(400, {"error": "response_format not supported"}),
                           chat_reply('{"score": 1, "tags": []}')]
        self.assertEqual(llm.call_tool("s", TOOL, "p"), {"score": 1, "tags": []})
        self.assertIn("response_format", Handler.log[0][2])
        self.assertNotIn("response_format", Handler.log[1][2])

    def test_other_errors_are_not_retried(self):
        Handler.replies = [(500, {"error": "boom"})]
        with self.assertRaisesRegex(llm.LLMError, "HTTP 500"):
            llm.call_tool("s", TOOL, "p")
        self.assertEqual(len(Handler.log), 1)

    def test_prose_instead_of_json_is_retried_then_fails(self):
        Handler.replies = [chat_reply("I think the answer is good."), chat_reply(json.dumps(GOOD))]
        self.assertEqual(llm.call_tool("s", TOOL, "p"), GOOD)
        self.assertIn("no JSON object found", Handler.log[1][2]["messages"][1]["content"])
        Handler.replies = [chat_reply("still prose")]
        with self.assertRaisesRegex(llm.LLMError, "no JSON object found"):
            llm.call_tool("s", TOOL, "p")

    def test_configuration_and_unexpected_shapes(self):
        Handler.replies = [(200, {"unexpected": True})]
        with self.assertRaisesRegex(llm.LLMError, "unexpected reply shape"):
            llm.call_tool("s", TOOL, "p")
        del os.environ["DRILL_LLM_MODEL"]
        with self.assertRaisesRegex(llm.LLMError, "DRILL_LLM_MODEL"):
            llm.call_tool("s", TOOL, "p")

    def test_no_key_means_no_authorization_header(self):
        del os.environ["DRILL_LLM_KEY"]
        Handler.replies = [chat_reply(json.dumps(GOOD))]
        llm.call_tool("s", TOOL, "p")
        self.assertNotIn("authorization", Handler.log[0][1])


class ManualTest(Base):
    def run_manual(self, typed: str):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(typed)), contextlib.redirect_stdout(out), \
                mock.patch.object(llm.shutil, "which", return_value=None):
            result = llm.call_tool("system text", TOOL, "the question", max_tokens=100)
        return result, out.getvalue()

    def test_prints_the_prompt_and_reads_the_pasted_json(self):
        reply = 'Here:\n```json\n{"score": 2,\n "tags": ["a"]}\n```\nEND\n'
        result, shown = self.run_manual(reply)
        self.assertEqual(result, {"score": 2, "tags": ["a"]})
        for needle in ("Paste this into claude.ai", "system text", "the question",
                       json.dumps(TOOL["input_schema"]), "then a line with only END"):
            self.assertIn(needle, shown)

    def test_stops_at_end_and_at_eof(self):
        result, _ = self.run_manual(json.dumps(GOOD) + "\nEND\nignored text\n")
        self.assertEqual(result, GOOD)
        result, _ = self.run_manual(json.dumps(GOOD))  # Ctrl-D instead of END
        self.assertEqual(result, GOOD)

    def test_copies_to_the_clipboard_when_pbcopy_exists(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps(GOOD))), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(llm.shutil, "which", return_value="/usr/bin/pbcopy"), \
                mock.patch.object(llm.subprocess, "run") as run:
            llm.call_tool("s", TOOL, "p")
        self.assertEqual(run.call_args.args[0], ["pbcopy"])
        self.assertTrue(run.call_args.kwargs["input"].startswith(b"s\n\np\n\nReply with ONLY"))

    def test_an_empty_or_invalid_paste_fails_after_the_retry(self):
        with self.assertRaisesRegex(llm.LLMError, "no JSON object found"):
            self.run_manual("")


if __name__ == "__main__":
    unittest.main()
