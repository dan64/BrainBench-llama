"""LLM-based answer judge for the AI Brainteaser Benchmark.

The judge can run on any provider: Anthropic, OpenAI, or any
OpenAI-compatible endpoint (e.g. the same local model served by
llama-server). Configure it in config.yaml under the `judge:` key:

    judge:
      provider: openai_compatible        # openai_compatible | openai | anthropic
      model_id: local-model              # ignored by llama-server, use anything
      base_url: http://127.0.0.1:8080/v1 # only for openai_compatible
      api_key_env: MY_API_KEY            # optional, for remote endpoints

Note: using the model under test as its own judge introduces self-bias
(it tends to agree with its own phrasing), but it is a reasonable
approximation when no external judge API is available.
"""

import json
import logging
import re

import anthropic
import openai

logger = logging.getLogger(__name__)

JUDGE_PROMPT = """\
You are a strict but fair judge evaluating whether an AI model correctly answered a brainteaser question.

## Question
{question}

## Ground-Truth Answer
{ground_truth}

## Model's Response
{response}

## Instructions
Determine if the model's response arrives at the CORRECT answer. Be strict on correctness but lenient on phrasing:
- The model must clearly commit to the correct answer (not just mention it among options)
- The model may phrase the answer differently — that's fine as long as the core conclusion matches
- If the model hedges with "it depends" and never commits, mark it INCORRECT
- If the model gives the right answer but with wrong reasoning, still mark it CORRECT (we're testing the answer, not the explanation)
- If the model gives no answer (empty, error, refusal), mark it INCORRECT

Respond with ONLY a JSON object, no other text:
{{"correct": true or false, "reasoning": "one sentence explaining your judgment"}}"""


def _extract_json(text: str) -> dict | None:
    """Robustly extract a JSON object from a model response.

    Handles: clean JSON, markdown code fences, leading/trailing prose.
    Small local models frequently wrap or prefix their output, so we
    fall back to bracket matching and finally a regex for the boolean.
    """
    text = text.strip()
    if not text:
        return None

    # 1) Direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2) Strip markdown fences
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1).strip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

    # 3) First balanced { ... } block
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    # 4) Last resort: recover the boolean flag from the text
    m = re.search(r'"?correct"?\s*[:=]\s*(true|false)', text, re.IGNORECASE)
    if m:
        return {"correct": m.group(1).lower() == "true", "reasoning": "(recovered from malformed output)"}
    m = re.search(r"\b(correct|incorrect)\b", text, re.IGNORECASE)
    if m:
        return {"correct": m.group(1).lower() == "correct", "reasoning": "(recovered from malformed output)"}

    return None


async def _judge_anthropic(question: str, ground_truth: str, response: str, judge_model: str) -> dict:
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=judge_model,
        max_tokens=256,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            question=question, ground_truth=ground_truth, response=response)}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_judge_text(text)


async def _judge_openai(
    question: str, ground_truth: str, response: str,
    judge_model: str, base_url: str | None, api_key_env: str | None,
    reasoning_budget_tokens: int | None = None,
    reasoning_budget_message: str | None = None,
) -> dict:
    if api_key_env:
        import os
        api_key = os.environ.get(api_key_env, "")
    else:
        api_key = ""
    if not api_key:
        api_key = "local-no-key"
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    client = openai.AsyncOpenAI(api_key=api_key, **kwargs)
    resp = await client.chat.completions.create(
        model=judge_model,
        # 8192: a local reasoning judge (e.g. Qwen3 with xhigh effort) can
        # spend most of its budget thinking before emitting the JSON verdict;
        # 512 truncated the thinking and left content empty.
        max_tokens=8192,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            question=question, ground_truth=ground_truth, response=response)}],
    )
    # Per-request thinking budget (llama-server b9982+): judging is a simple
    # comparison, so a small cap keeps the local xhigh judge fast.
    # extra_body because the openai client rejects unknown kwargs.
    extra_body = {}
    if reasoning_budget_tokens is not None:
        extra_body["reasoning_budget_tokens"] = reasoning_budget_tokens
    if reasoning_budget_message is not None:
        extra_body["reasoning_budget_message"] = reasoning_budget_message
    create_kwargs = {"extra_body": extra_body} if extra_body else {}
    resp = await client.chat.completions.create(
        model=judge_model,
        # 8192: a local reasoning judge (e.g. Qwen3 with xhigh effort) can
        # spend most of its budget thinking before emitting the JSON verdict;
        # 512 truncated the thinking and left content empty.
        max_tokens=8192,
        messages=[{"role": "user", "content": JUDGE_PROMPT.format(
            question=question, ground_truth=ground_truth, response=response)}],
        **create_kwargs,
    )
    text = resp.choices[0].message.content or ""
    return _parse_judge_text(text)


def _parse_judge_text(text: str) -> dict:
    obj = _extract_json(text)
    if obj is None:
        logger.error(f"Judge returned non-parseable output: {text[:200]!r}")
        return {"correct": False, "reasoning": "", "error": f"JSON parse error: unparseable judge output"}
    correct = obj.get("correct", False)
    if isinstance(correct, str):
        correct = correct.strip().lower() == "true"
    return {
        "correct": bool(correct),
        "reasoning": str(obj.get("reasoning", "")),
        "error": None,
    }


async def judge_response(
    question: str,
    ground_truth: str,
    response: str,
    judge_model: str = "claude-sonnet-4-20250514",
    judge_provider: str | None = None,
    judge_base_url: str | None = None,
    api_key_env: str | None = None,
    reasoning_budget_tokens: int | None = None,
    reasoning_budget_message: str | None = None,
) -> dict:
    """Judge whether a model response is correct.

    Args:
        judge_model: model id to use for judging.
        judge_provider: "anthropic", "openai", or "openai_compatible".
            Defaults to "anthropic" unless judge_base_url is given
            (in which case "openai_compatible").
        judge_base_url: base URL for OpenAI-compatible endpoints,
            e.g. "http://127.0.0.1:8080/v1" for llama-server.
        api_key_env: name of the env var holding the API key (optional).

    Returns:
        {"correct": bool, "reasoning": str, "error": str|None}
    """
    if not response or not response.strip():
        return {"correct": False, "reasoning": "Empty response", "error": None}

    provider = judge_provider or ("openai_compatible" if judge_base_url else "anthropic")

    try:
        if provider in ("anthropic",):
            return await _judge_anthropic(question, ground_truth, response, judge_model)
        elif provider in ("openai", "openai_compatible"):
            return await _judge_openai(question, ground_truth, response, judge_model,
                                       judge_base_url, api_key_env,
                                       reasoning_budget_tokens, reasoning_budget_message)
        else:
            return {"correct": False, "reasoning": "", "error": f"Unknown judge provider: {provider}"}
    except Exception as e:
        logger.error(f"Judge error: {e}")
        return {"correct": False, "reasoning": "", "error": str(e)}
