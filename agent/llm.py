"""Power layer: all model calls go through Meterless Nightshift (OpenAI-compatible Runtime).

Every call is logged with the model Nightshift reports serving it and its Meterless receipt
(routed_model, decision_id), which is the evidence that NVIDIA Nemotron handled the request.
"""
import hashlib
import json
import os
import re
import time

from . import cache, config, log, runctl

_client = None
_thinking_flag_ok = True
_call_no = 0


def client():
    global _client
    if _client is None:
        from openai import OpenAI  # imported here so tests and sample mode do not need the package
        _client = OpenAI(
            base_url=os.environ["METERLESS_BASE_URL"],
            api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
            default_headers={"meterless-app": os.environ.get("METERLESS_APP", "rivertruth"),
                             "meterless-scope": "user"},
            timeout=300,
            max_retries=0,  # we retry ourselves so every failure is logged
        )
    return _client


def _receipt(resp):
    extra = getattr(resp, "model_extra", None) or {}
    m = extra.get("meterless") or {}
    if not isinstance(m, dict):
        return {}
    keep = {k: m.get(k) for k in ("routed_model", "decision_id", "from_cache") if k in m}
    if isinstance(m.get("cache"), dict):
        keep["cache_status"] = m["cache"].get("status")
        keep["cache"] = {k: v for k, v in m["cache"].items() if isinstance(v, (str, int, float, bool))}
    lineage = m.get("lineage")
    if isinstance(lineage, dict):
        for k in ("routed_model", "lane", "provider", "model"):
            if k in lineage and "routed_model" not in keep:
                keep["routed_model"] = lineage[k]
    return keep


def _stream(model, messages, budget, temperature, extra, timeout=None):
    """Stream the reply. Nightshift aborts non-streamed requests after ~45 s; streaming keeps the connection
    alive while Nemotron reasons. Returns (text, served_model, receipt, usage)."""
    stream = client().chat.completions.create(model=model, messages=messages, max_tokens=budget,
                                              temperature=temperature, extra_body=extra, stream=True,
                                              timeout=timeout or config.CALL_TIMEOUT_S)
    parts, served, receipt, usage = [], None, {}, None
    for ev in stream:
        served = served or getattr(ev, "model", None)
        r = _receipt(ev)
        if r:
            receipt.update(r)
        if getattr(ev, "usage", None):
            usage = ev.usage
        if ev.choices:
            d = ev.choices[0].delta
            if getattr(d, "content", None):
                parts.append(d.content)
    if receipt.get("routed_model"):
        served = receipt["routed_model"]
    return "".join(parts).strip(), served, receipt, usage


_seq = 0


def _dump_prompt(role, model, msgs):
    """Save the exact prompt of every live call to logs/prompts/ (so any answer can be traced to what the model saw)."""
    global _seq
    try:
        _seq += 1
        d = os.path.join(config.LOG_DIR, "prompts")
        os.makedirs(d, exist_ok=True)
        body = "\n\n".join(f"### {m['role']}\n{m['content']}" for m in msgs)
        name = f"{log._run_id or 'norun'}_{_seq:03d}_{role}.txt"
        with open(os.path.join(d, name), "w") as f:
            f.write(f"model: {model}\n\n{body}")
        log.event("prompt", role=role, file=f"prompts/{name}", chars=len(body),
                  sha=hashlib.sha256(body.encode()).hexdigest()[:12])
    except Exception:  # noqa: BLE001  logging must never break a run
        pass


PROSE_ROLES = {"reporter", "river_check"}


def temperature_for(role):
    """0 for every role that writes SQL or JSON (planner, investigator, critic, fact-checker), so the same input
    gives the same output; a little for the prose roles."""
    return config.REPORT_TEMPERATURE if role.split(":")[0] in PROSE_ROLES else 0.0


def chat(role, messages, model=None, max_tokens=8000, temperature=None, attempts=3):
    """Call the model through Nightshift (streamed); returns text.

    Nemotron 3.5 reasons before answering, and the reasoning counts against max_tokens, so we keep a large
    budget, add '/no_think' (measured fastest in scripts/probe_thinking.py) and retry with a bigger budget
    if a reply comes back empty.
    """
    model = model or config.FAST_MODEL
    temperature = temperature_for(role) if temperature is None else temperature
    key = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    if config.SAMPLE_MODE:
        hit = cache.get("llm", key)
        if hit is None:
            raise RuntimeError(f"SAMPLE_MODE: no recorded model reply for this {role} step")
        if "failed" in hit["value"]:  # this call failed while recording: take the same path again
            log.event("llm_error", role=role, requested=model, error=hit["value"]["failed"][:300], source="sample")
            return _after_failure(role, messages, model, max_tokens, temperature, attempts, hit["value"]["failed"])
        log.event("llm", role=role, requested=model, served_by=hit["value"]["served_by"],
                  receipt=hit["value"].get("receipt"), source="sample")
        return hit["value"]["text"]
    global _thinking_flag_ok
    msgs = [dict(m) for m in messages]
    # Per-model "reasoning off" switches, measured with scripts/probe_thinking.py:
    #   Lightning 3.5: '/no_think' in the system prompt was fastest.
    #   Ultra 3 550B: enable_thinking=false gives clean JSON; '/no_think' makes it write its reasoning into the answer.
    if "lightning" in model:
        if msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] = "/no_think\n" + msgs[0]["content"]
        else:
            msgs.insert(0, {"role": "system", "content": "/no_think"})
    _dump_prompt(role, model, msgs)
    last_err = None
    budget = max_tokens
    for i in range(1, attempts + 1):
        runctl.check(min_seconds=3)  # cancelled, or no time left for a useful call
        left = runctl.time_left()
        timeout = config.CALL_TIMEOUT_S if left is None else max(3.0, min(config.CALL_TIMEOUT_S, left - 1))
        t0 = time.time()
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if _thinking_flag_ok else None
        try:
            text, served, receipt, usage = _stream(model, msgs, budget, temperature, extra, timeout)
            log.event("llm", role=role, attempt=i, requested=model, served_by=served, receipt=receipt,
                      seconds=round(time.time() - t0, 2), max_tokens=budget,
                      tokens=getattr(usage, "total_tokens", None))
            if not text:
                raise RuntimeError("empty reply")
            cache.put("llm", key, {"text": text, "served_by": served, "receipt": receipt})
            return text
        except runctl.RunStopped:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            err = str(e)
            log.event("llm_error", role=role, attempt=i, requested=model, max_tokens=budget, error=err[:300],
                      seconds=round(time.time() - t0, 2))
            if extra and ("chat_template_kwargs" in err or "enable_thinking" in err or "Error code: 400" in err):
                _thinking_flag_ok = False
            if "empty" in err:
                budget = min(budget * 2, 16000)
            left = runctl.time_left()
            time.sleep(min(1.5 * i, max(0.0, (left or 99) - 3)))
    cache.put("llm", key, {"failed": str(last_err)})  # recorded, so sample mode takes the same fallback
    return _after_failure(role, messages, model, max_tokens, temperature, attempts, last_err)


def _after_failure(role, messages, model, max_tokens, temperature, attempts, last_err):
    if model != config.FAST_MODEL:
        # The bigger model (critic/reporter) is optional: if it is unavailable, fall back to the fast lane.
        log.event("llm_fallback", role=role, from_model=model, to_model=config.FAST_MODEL, error=str(last_err)[:200])
        return chat(role + ":fallback", messages, model=config.FAST_MODEL, max_tokens=max_tokens,
                    temperature=temperature, attempts=2)
    raise RuntimeError(f"{role}: model call failed after {attempts} attempts: {last_err}")


def chat_json(role, messages, validate=None, **kw):
    """Call the model, parse a JSON object and validate it. One repair attempt (with the parse or validation error)
    if the reply is unusable; then raise, so the caller falls back to its safe default."""
    from .schemas import Inconsistent, SchemaError
    text = chat(role, messages, **kw)
    obj, err = _first_valid(text, validate)
    if obj is not None:
        return obj
    repair = messages + [{"role": "assistant", "content": text[:4000]},
                         {"role": "user", "content": f"That reply could not be used ({err}). Reply again with ONLY "
                                                     "the JSON object in the requested format, no prose."}]
    first = err
    obj, err = _first_valid(chat(role + ":repair", repair, **kw), validate)
    if obj is None:  # asked once to fix it; it did not, so a self-contradicting reply is used as it stands
        kept = err if isinstance(err, Inconsistent) else first if isinstance(first, Inconsistent) else None
        if kept is not None:
            log.event("model_reply_inconsistent", role=role, error=str(kept)[:300])
            return kept.value
    if obj is None:
        log.event("model_reply_invalid", role=role, error=str(err)[:300])
        raise SchemaError(f"{role}: model did not return a usable JSON reply ({err})")
    return obj


def _first_valid(text, validate):
    """The last JSON object in the reply that parses (and passes validation, if given)."""
    from .schemas import SchemaError
    cands = json_candidates(text)
    if not cands:
        return None, "no JSON object found"
    err = "invalid JSON"
    for c in reversed(cands):
        if validate is None:
            return c, None
        try:
            return validate(c), None
        except SchemaError as e:
            err = e
    return None, err


def json_candidates(text):
    """Every JSON object in a model reply, in order: fenced ```json blocks and balanced {...} spans, with braces inside
    string literals ignored. Reasoning in <think> tags is dropped."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    spans = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)]
    depth, start, in_str, esc = 0, None, False, False
    for j, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append(text[start:j + 1])
    out = []
    for c in spans:
        try:
            v = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            out.append(v)
    # fenced blocks were added first; keep document order for the rest, so "last" means the final answer
    return out


def extract_json(text):
    """The last JSON object in a model reply (the final answer, not an example quoted before it), or None."""
    c = json_candidates(text)
    return c[-1] if c else None
