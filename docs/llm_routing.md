# LLM routing

Edit [`config/llm_routing.yaml`](../config/llm_routing.yaml) and restart FastAPI to
change model routing. Credentials and gateway URLs remain in `.env`.

A **provider** selects a LangChain adapter and names its connection settings.
For example, `aicredits` uses the `openai_compatible` adapter with
`AI_CREDITS_API_KEY` and `AI_CREDITS_API_URL`. The provider factory maps adapter
names to `ChatOpenAI`, `ChatGoogleGenerativeAI`, `ChatGroq`, or `ChatSarvam`.
Another OpenAI-compatible gateway can be added with a new provider entry and
its own `*_API_KEY` and `*_URL` variables; no new Python adapter is needed.

A **model profile** gives a provider/model pair a stable name. Add a new model
under `models`, then reference its name in a purpose. The current purposes are
`request_parser`, `request_analyzer`, `semantic_judge`, and `review_explainer`. Each purpose has an
ordered `models` list, a fallback policy, a timeout, an output-token limit,
and separate output-attempt and provider-retry budgets. A later purpose can reuse existing profiles.

```yaml
models:
  aicredits_qwen_flash:
    provider: aicredits
    model: qwen/qwen3.8-flash
purposes:
  semantic_judge:
    models: [gemini_lite, aicredits_qwen_flash]
    fallback_policy: transient
    timeout_seconds: 20
    max_output_tokens: 6144
    max_output_attempts: 3
    max_provider_retries_per_attempt: 1
    stage_timeout_seconds: 120
```

The example above extends the existing routing file; it is not a complete
replacement for it. A model's position determines fallback order.

`max_output_attempts` includes the initial answer. Schema, grounding, or evidence
validation failures consume an output attempt and append the prior response,
field-level error JSON, and correction instructions to the accumulated messages.
The current model gets a chance to correct its first invalid response. Repeated
invalid responses can advance to the next configured model.

`max_provider_retries_per_attempt` counts extra calls after a retryable provider
failure. It resets when a validation failure starts the next output attempt.
Provider retries reuse the current messages without adding fictitious validation
feedback. Transient failures advance to the next configured profile when available;
at the final profile, retries use that profile again. Corrections continue on the
current profile, rather than restarting at the primary model. Exhausting the
provider allowance stops the stage as unavailable; it cannot start another output
attempt just to regain retries. Authentication errors, unsupported requests, and
recognized provider refusals stop immediately.

For example, 3 output attempts with 1 provider retry each permit at most 6 physical
calls. Later profiles might never be reached before the budgets are exhausted.
`stage_timeout_seconds` adds an elapsed-time deadline. No further calls are started
after it. Adapters that support request timeouts receive a timeout capped by the remaining time.
Late responses are rejected. This is a cooperative deadline: it does not forcibly
cancel an in-flight synchronous SDK request; an adapter that ignores its timeout
can overrun it. SDK retries remain disabled so they cannot silently multiply calls.
Deadline exhaustion is recorded as `stage_deadline_exceeded`, with
`budget_exhausted: stage_time` in the attempt trace. It does not trigger another
provider call. A timeout raised by the provider still records `provider_unavailable`.
A valid uncertain assessment is accepted and sent onward for human review, rather
than repeatedly asking a model to manufacture certainty.

Startup validates profiles, adapter names, retry limits, and the connection
settings selected by online purposes. Monitoring saves per-call `output_attempt`,
`provider_call` (within that output attempt), `total_provider_calls`, `error_origin`,
`failure_category`, `next_action`, and `budget_exhausted` in the existing
`request_attempt_events.parser_trace` JSON. The same trace retains full messages,
response, validation JSON, and correction text. No new audit table is required.
The adapter uses LangChain's `include_raw=True` to retain the raw response,
parsed result, and parsing error together. Confirmed output errors carry the
full raw message (including tool calls) into the trace and correction feedback,
with field-level errors extracted from the original exception. SDK/client
validation failures stop immediately with `error_origin: internal` and
`failure_category: application_error`; they do not trigger corrective prompts.
If the request parser exhausts corrections for malformed model output, the
existing parser rejection flow records the outcome in `decisions` and
`request_interpretations`, retaining attempt counts and correction feedback.
The existing `attempts` count still means total provider calls; stage evidence and
parser provenance additionally expose `output_attempts` and `provider_calls`.
The analyzer/judge `fallback_from` object keeps its existing shape and identifies
the first failed model when a switch occurred.

Embedding model IDs are also cataloged under `embeddings.models` in the same
YAML file. `embeddings.selected: gemini_current` keeps the current
`gemini-embedding-2` model active. To switch, change `embeddings.selected` to
another profile name. The AICredits profiles use `AI_CREDITS_API_URL` and `AI_CREDITS_API_KEY`
with its OpenAI-compatible embeddings endpoint. Catalog entries are available
options, not automatically tested or activated models.

Keep each Qdrant collection tied to the embedding model, dimensions, and text
format used when it was seeded. When switching profiles, choose new
`QDRANT_COLLECTION` and `SEMANTIC_V3_COLLECTION` names, seed both collections,
and recalibrate similarity thresholds. Existing vectors remain valid for the
old profile, but embedding spaces cannot be mixed even when dimensions match.
The Redis query cache includes the embedding specification in its key, so a
new profile uses new cache entries automatically.
