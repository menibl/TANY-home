"""
IMPORTANT technical note (why Claude needs a different shape than the
other two): Anthropic's API does not offer a native speech-to-speech
endpoint the way OpenAI Realtime or Gemini Live do. So:

  - ClaudeEngine  -> cascaded pipeline: text in, text out (+ tool calls).
                     The orchestrator is responsible for STT before and
                     TTS after. Slightly higher latency, best reasoning.
  - OpenAIEngine / GeminiEngine -> true speech-to-speech; audio in, audio
                     out, lower latency, but you lose the option to swap
                     the LLM independently of the voice layer.

All three implement the same interface so `LLM_PROVIDER` env var is a
one-line switch in the orchestrator, nothing else changes.
"""
import os
from abc import ABC, abstractmethod

import anthropic


class ConversationEngine(ABC):
    @abstractmethod
    async def respond(self, system_prompt: str, user_text: str, tools: list[dict], history: list[dict]) -> dict:
        """
        Returns:
          {
            "text": "<assistant reply text, empty if pure tool call turn>",
            "tool_calls": [{"name": ..., "input": {...}, "id": ...}],
          }
        """
        ...


class ClaudeEngine(ConversationEngine):
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

    async def respond(self, system_prompt: str, user_text: str, tools: list[dict], history: list[dict]) -> dict:
        messages = history + [{"role": "user", "content": user_text}]
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system_prompt,
            messages=messages,
            tools=tools if tools else anthropic.NOT_GIVEN,
        )

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"name": block.name, "input": block.input, "id": block.id})

        return {"text": "".join(text_parts), "tool_calls": tool_calls, "stop_reason": resp.stop_reason}


class OpenAIRealtimeEngine(ConversationEngine):
    """Stub — OpenAI Realtime is natively speech-to-speech, so this adapter
    would need to bypass the orchestrator's own STT/TTS entirely and proxy
    audio frames straight through. Left unimplemented until/if you want to
    switch LLM_PROVIDER away from Claude."""
    async def respond(self, system_prompt, user_text, tools, history) -> dict:
        raise NotImplementedError("OpenAI Realtime needs an audio-passthrough session, not text respond()")


class GeminiLiveEngine(ConversationEngine):
    """Stub — same situation as OpenAIRealtimeEngine, see note above."""
    async def respond(self, system_prompt, user_text, tools, history) -> dict:
        raise NotImplementedError("Gemini Live needs an audio-passthrough session, not text respond()")


def build_engine(provider: str) -> ConversationEngine:
    if provider == "claude":
        return ClaudeEngine()
    if provider == "openai":
        return OpenAIRealtimeEngine()
    if provider == "gemini":
        return GeminiLiveEngine()
    raise ValueError(f"unknown LLM_PROVIDER: {provider}")
