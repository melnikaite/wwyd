# Evaluating wwyd

**Status:** the eval script (`wwyd eval`, SPEC §13) is planned and not built yet; there
are no results yet.

## Method

The question is whether someone who knows the owner can tell a `predict` answer from the
owner's real reply.

1. The owner prepares, locally, a JSONL file with one line per real past exchange:
   `{"situation": "...", "audience": "...", "real_reply": "..."}`. Use exchanges that were
   **not** ingested, so the persona has not seen the answer.
2. `wwyd eval pairs.jsonl` calls `predict` for each situation, then shows the real reply
   and the prediction in random order. The owner guesses which is real.
3. The script prints the **detection rate**: 50% means indistinguishable, 100% means every
   prediction was spotted. It also reports how often `predict` escalated instead of
   answering. Nothing but the score is stored; the pairs file never
   leaves the owner's machine except as `predict` situations sent to the configured LLM.

Report the number of pairs, the audiences covered and the model. Small samples are noisy:
with 40 pairs, a 95% interval around 50% is roughly ±15 points.

## Results

| Model | Backend | Pairs | Detection rate | Escalated | Date |
|---|---|---|---|---|---|
| `qwen3-vl-8b-instruct` | local (LocalAI) | TODO | TODO | TODO | TODO |
| TODO (OpenRouter model, SPEC D12) | OpenRouter | TODO | TODO | TODO | TODO |
