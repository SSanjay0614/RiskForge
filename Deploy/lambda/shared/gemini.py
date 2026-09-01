"""
One Gemini call, shared by the three prompt Lambdas.

**Why Gemini and not Bedrock.** Bedrock inference is not available on this
account. Every model, in two regions, on both `Converse` and `InvokeModel`, held
by a principal with `AdministratorAccess`, returns
`ValidationException: Operation not allowed`; `get-foundation-model-availability`
reports the model AVAILABLE but `authorizationStatus: NOT_AUTHORIZED`; and
`freetier get-account-plan-state` reports `accountPlanType: FREE`. It is a plan
restriction, the same one that blocked Aurora in Phase 2. Upgrading to the paid
plan would fix it and would also expose the running RDS instance and EC2 host to
real charges, so the model host moved off AWS instead. Nothing else about the
design changed: three Lambdas, outside the VPC, same input and output contracts.

**No data leaves that would not have gone to Bedrock either.** The guard sees the
schema and the question. SQL generation sees the schema and the question. The
evaluator sees a *profile* of a result -- row count, column names, and a summary
of the columns the WHERE clause filtered on -- and never a row. That was already
the design in tools/sql_evaluator_tool.py; evaluator_action/handler.py enforces
it at the boundary instead of trusting its caller to have honoured it.

**Where the key lives: SSM Parameter Store, as a SecureString.** Not an
environment variable -- a Lambda's environment is readable by anyone holding
`lambda:GetFunctionConfiguration`, and it would sit in Terraform state, which is
a file on a laptop next to a public repo. Not Secrets Manager either: its
differentiator is managed rotation, a third-party API key does not rotate on a
schedule, and a standard Parameter Store parameter is free where a secret is
$0.40/month. Both are KMS-encrypted and both are audited in CloudTrail, so on a
fixed credit budget the free one wins on identical guarantees.

**Structured output, not prompt-and-hope.** The request carries a
`responseSchema`, so the API constrains the model to emit JSON of the declared
shape. tools/*.py needed a regex to fish a JSON object out of markdown fences
because a local model returns whatever it likes; that regex survives here only
as a backstop for a model or an API version that ignores the schema.

**The key is cached across invocations, unlike db.py's connection.** The contrast
is deliberate. An RDS IAM token expires in 15 minutes and a cached connection can
be frozen mid-transaction, so `db.connect()` builds a fresh one every time. An
API key is static, so re-reading it from SSM on each warm invocation would buy
nothing and cost a network round trip.
"""
import json
import os
import random
import re
import time
import urllib.error
import urllib.request

import boto3

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
KEY_PARAM = os.environ["GEMINI_API_KEY_PARAM"]
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"

HTTP_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "20"))
MAX_ATTEMPTS = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "3"))

# 429 is the one that actually happens: a free-tier key has a requests-per-minute
# ceiling, and the retry loop below is what stops a burst of three sequential
# workflow steps failing the whole run.
RETRY_STATUS = {429, 500, 502, 503, 504}

# Thinking models spend output tokens before they emit anything, and a budget
# that runs out returns finishReason MAX_TOKENS with empty text -- a confusing
# failure that looks like the model refusing. These three tasks are
# classification and one short statement, so thinking earns nothing here and the
# budget is zero where it can be set.
#
# `thinkingBudget` is a 2.5-family field, and the gate is narrow for a reason
# found the hard way: the 3 family replaced the token budget with a thinking
# *level*, so sending it a budget of 0 is not "do not think" but
# `400 INVALID_ARGUMENT`, and pre-2.5 models reject the field outright. Anything
# outside 2.5 therefore gets no thinkingConfig at all and keeps the model's own
# default -- which is why the token floor below exists.
THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
_SUPPORTS_THINKING = re.match(r"gemini-2\.5", MODEL) is not None

# Headroom for a model whose thinking cannot be switched off. The callers ask for
# 512-1024 tokens because their answers are one JSON object of two or three short
# fields; on a thinking model those same 512 tokens are spent reasoning and the
# response comes back empty with finishReason MAX_TOKENS. Raising the ceiling
# costs nothing when it is not used -- maxOutputTokens is a limit, not a
# reservation -- and turns a silent empty answer into a normal one.
MIN_OUTPUT_TOKENS_WHEN_THINKING = int(
    os.environ.get("GEMINI_MIN_OUTPUT_TOKENS", "4096")
)

_ssm = None
_api_key = None


class GeminiError(RuntimeError):
    """A model call that did not produce usable JSON, for any reason."""


def _key():
    global _ssm, _api_key
    if _api_key is None:
        if _ssm is None:
            _ssm = boto3.client("ssm")
        parameter = _ssm.get_parameter(Name=KEY_PARAM, WithDecryption=True)
        _api_key = parameter["Parameter"]["Value"].strip()
    return _api_key


# A 429 body says how long to wait -- "Please retry in 6.302382372s", and a
# google.rpc.RetryInfo in details[] saying the same thing structurally. Honouring
# it matters here rather than being a nicety: the free tier meters 20 requests per
# minute, so the wait it asks for is ~6s while the backoff below would have slept
# 2s and then 4s and failed all three attempts against a quota that had not
# cleared yet.
_RETRY_AFTER_RE = re.compile("retry in ([0-9.]+)s", re.I)

# Ceiling on any single wait. A Lambda that sleeps out its whole timeout budget
# has turned a rate limit into an invocation charge and no answer.
MAX_SLEEP_S = float(os.environ.get("GEMINI_MAX_SLEEP_S", "30"))


def _retry_after(detail):
    """Seconds the provider asked us to wait, or None."""
    try:
        error = json.loads(detail).get("error") or {}
    except ValueError:
        # detail is truncated to 1000 characters, so a long body may not parse.
        # The regex below reads the same number out of the message clause, which
        # sits near the front of the body and survives the cut.
        error = {}

    for item in error.get("details") or []:
        if str(item.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(item.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                pass

    match = _RETRY_AFTER_RE.search(detail or "")
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


def _post(body, api_key):
    request = urllib.request.Request(
        ENDPOINT % MODEL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # A header, not `?key=` on the URL. A query string is a credential in
            # a place that gets logged -- access logs, proxy logs, exception
            # traces that echo the request line.
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_text(payload):
    """The model's text, or a GeminiError naming why there is none."""
    blocked = (payload.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise GeminiError("prompt blocked by the provider: %s" % blocked)

    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiError("no candidates in the response")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()

    if not text:
        # Reported rather than swallowed: MAX_TOKENS here means the schema or the
        # thinking budget needs raising, and SAFETY means the prompt does.
        raise GeminiError(
            "empty response (finishReason=%s)" % candidate.get("finishReason")
        )
    return text


def _loads(text):
    """Backstop parse. With responseSchema set this is a plain json.loads; the
    fence-stripping is here for the case where the schema was ignored."""
    try:
        return json.loads(text)
    except ValueError:
        pass

    stripped = re.sub(r"^```(?:json)?", "", text).strip()
    stripped = re.sub(r"```$", "", stripped).strip()
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        raise GeminiError("no JSON object in the response: %s" % text[:200])
    try:
        return json.loads(match.group())
    except ValueError as exc:
        raise GeminiError("unparseable JSON: %s (%s)" % (text[:200], exc))


def generate_json(prompt, response_schema, max_output_tokens=2048):
    """
    One prompt in, one dict matching `response_schema` out.

    temperature=0 because all three callers are classifiers or a single
    statement generator: the same question should produce the same answer twice,
    and an evaluator that flips its verdict between runs is not an evaluator.
    """
    if not _SUPPORTS_THINKING:
        max_output_tokens = max(max_output_tokens, MIN_OUTPUT_TOKENS_WHEN_THINKING)

    generation_config = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseSchema": response_schema,
        "maxOutputTokens": max_output_tokens,
    }
    if _SUPPORTS_THINKING:
        generation_config["thinkingConfig"] = {"thinkingBudget": THINKING_BUDGET}

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }

    api_key = _key()
    last = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        asked = None
        try:
            payload = _post(body, api_key)
            return _loads(_extract_text(payload))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                # 1000, not 300: a 429 body names *which* quota was exhausted
                # ("per minute" is waited out, "per day" is not) and 300
                # characters cut off exactly that clause.
                detail = exc.read().decode("utf-8", "replace")[:1000]
            except Exception:  # noqa: BLE001 -- the status is what matters
                pass
            # The provider's own message is the useful part of a 400: "API key not
            # valid" and "model not found" are both 400s and need different fixes.
            last = GeminiError("HTTP %s from the model API: %s" % (exc.code, detail))
            if exc.code not in RETRY_STATUS or attempt == MAX_ATTEMPTS:
                raise last
            asked = _retry_after(detail)
        except (urllib.error.URLError, TimeoutError) as exc:
            last = GeminiError("model API unreachable: %s" % exc)
            if attempt == MAX_ATTEMPTS:
                raise last

        # Jittered, so three Lambdas that hit a rate limit in the same second do
        # not all wake up in the same second either. Never shorter than the wait
        # the provider asked for -- retrying early against a per-minute quota just
        # spends an attempt to be told the same thing again.
        backoff = min(2 ** attempt, 8) + random.uniform(0, 0.75)
        time.sleep(min(max(backoff, asked or 0), MAX_SLEEP_S))

    raise last

