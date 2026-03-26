# bim_agent/llm.py
from __future__ import annotations

import json
import os
import time
import hashlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from openai import OpenAI

ToolHandler = Callable[[str, Dict[str, Any]], Any]


def _to_string(x):
    if isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False, indent=2, default=str)


def _ensure_json_word(prompt: str) -> str:
    # JSON mode requires "json" to appear somewhere in the prompt.
    if "json" in prompt.lower():
        return prompt
    return "Return JSON only.\n" + prompt


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _truncate(s: str, max_chars: int = 20000) -> str:
    if s is None:
        return s
    if len(s) <= max_chars:
        return s
    head = s[: max_chars // 2]
    tail = s[-max_chars // 2 :]
    return head + "\n...[TRUNCATED]...\n" + tail


def _trace_append(path: Optional[str], rec: Dict[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class LLMRunner:
    """
    Chat Completions tool loop:
      - call chat.completions.create(...)
      - if tool_calls -> execute tools -> append tool results -> call again
      - stop when no tool_calls
    """

    def __init__(
        self,
        model: str = "gpt-5",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key or os.getenv("OPENAI_API_KEY")}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    def run_with_tools(
        self,
        *,
        instructions: str,
        user_input: Any = None,
        input_items: Any = None,  # backward-compat alias
        tools: List[Dict[str, Any]],
        tool_handler: ToolHandler,
        response_format: Optional[Dict[str, Any]] = None,
        max_turns: int = 30,
        trace_path: Optional[str] = None,
        trace_tag: Optional[str] = None,
    ) -> Tuple[str, Any]:
        if user_input is None and input_items is not None:
            user_input = input_items
        if user_input is None:
            raise TypeError("Provide user_input (or input_items).")

        # Build initial messages
        user_msg = _to_string(user_input)

        # If caller wants JSON mode, ensure prompt mentions JSON (API requirement)
        if isinstance(response_format, dict) and response_format.get("type") == "json_object":
            user_msg = _ensure_json_word(user_msg)

        messages = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_msg},
        ]

        chat_tools = tools or []

        run_id = f"run_{int(time.time()*1000)}"
        _trace_append(
            trace_path,
            {
                "event": "start",
                "ts": time.time(),
                "run_id": run_id,
                "tag": trace_tag,
                "model": self.model,
                "max_turns": max_turns,
                "response_format": response_format,
                "instructions_sha256": _sha256_text(instructions),
                "user_input_preview": _truncate(user_msg, 4000),
                "tool_names": [t.get("function", {}).get("name") for t in chat_tools if isinstance(t, dict)],
            },
        )

        last = None
        for turn_idx in range(max_turns):
            req: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
            }
            if chat_tools:
                req["tools"] = chat_tools
                req["tool_choice"] = "auto"
            if response_format is not None:
                req["response_format"] = response_format

            last = self.client.chat.completions.create(**req)
            msg = last.choices[0].message

            assistant_content = msg.content or ""
            _trace_append(
                trace_path,
                {
                    "event": "assistant",
                    "ts": time.time(),
                    "run_id": run_id,
                    "tag": trace_tag,
                    "turn": turn_idx,
                    "content": _truncate(assistant_content, 20000),
                },
            )

            # If no tool calls, we're done
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                _trace_append(
                    trace_path,
                    {"event": "end", "ts": time.time(), "run_id": run_id, "tag": trace_tag, "turn": turn_idx},
                )
                return (assistant_content, last)

            # Record assistant message + tool_calls in conversation
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                name = tc.function.name
                args_raw = tc.function.arguments or "{}"

                _trace_append(
                    trace_path,
                    {
                        "event": "tool_call",
                        "ts": time.time(),
                        "run_id": run_id,
                        "tag": trace_tag,
                        "turn": turn_idx,
                        "tool_call_id": tc.id,
                        "name": name,
                        "arguments_raw": _truncate(args_raw, 20000),
                    },
                )

                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}

                try:
                    result = tool_handler(name, args)
                except Exception as exc:
                    result = {"ok": False, "error": f"tool_handler exception: {exc}", "tool": name, "args": args}

                result_str = _to_string(result)
                _trace_append(
                    trace_path,
                    {
                        "event": "tool_result",
                        "ts": time.time(),
                        "run_id": run_id,
                        "tag": trace_tag,
                        "turn": turn_idx,
                        "tool_call_id": tc.id,
                        "name": name,
                        "result": _truncate(result_str, 20000),
                    },
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    }
                )

        _trace_append(
            trace_path,
            {"event": "end_max_turns", "ts": time.time(), "run_id": run_id, "tag": trace_tag, "max_turns": max_turns},
        )
        return ((last.choices[0].message.content or "") if last else "", last)