Status: accepted
Owner: technical owner + product owner
Last verified: 2026-11-27
Applies to: generation and extraction

# ADR-0019: Provider and model naming, and what the defaults mean

## Context

The predecessor's architecture notes referred to a model string that does not exist
(`Codex-sonnet-4-6`) — an artefact of a rename that was never reconciled with the
code. This repository defaults to `llm_provider=fake` and
`llm_model=claude-sonnet-4-6`. The fake default is right for local work and tests,
but "the production provider is whatever the code happens to construct" is not a
decision anyone can configure against, and a wrong model string in documentation is
how an operator ends up debugging a 404 from a provider.

## Decision

**Provider is `anthropic`; model is `claude-sonnet-4-6`.** That exact string appears
in `.env.example`, in the deployment page, and in `Settings.llm_model`, and it is
what extraction and chat record as model lineage.

- `llm_provider=fake` is the **local and test** default. It is a real provider
  implementation (deterministic, offline) rather than a stub, which is why the
  offline suite is meaningful — and `production_blockers()` refuses it in `server`
  mode, so it cannot reach a deployment by inertia.
- The model string is **configuration, not code**. `AnthropicProvider.sampling_params`
  decides per target model which sampling parameters are legal, so `LAB_LLM_MODEL`
  can move between model generations without a code change. *Amended 2026-11-27:*
  it now sends none on any model. API version `2026-10-21`, which the `anthropic`
  2.x SDK sends, rejects `temperature`, `top_p` and `top_k` on every model, and the
  SDK no longer accepts them; an unpinned image picked up 2.x and every answer
  failed. Answers and extractions use the model's default sampling, and the SDK is
  pinned to `>=2,<3`.
- Every generated answer and every extraction records the provider, the model
  revision, and the settings used. Model lineage is per record, not per deployment,
  so a corpus built across a model change stays attributable.
- A local model is a supported fallback under the model-transfer policy
  (ADR: see `docs/security/model-transfer.md`): when a transfer is not permitted,
  the order is approved external provider → local model → abstain.

## Consequences

- An operator configures the intended provider from `.env.example` without reading
  code.
- Documentation that names a model can be checked against `Settings` — and the
  predecessor's phantom model string is not carried forward.
- Changing the production model is an `.env` change plus a re-run of the generation
  benchmark, because citation validity is model-sensitive and is a release gate.

## Alternatives considered

**Pin the model in code.** Makes a model upgrade a release, and makes the
sampling-parameter gate unnecessary. Rejected: model availability changes faster
than this codebase does.

**Default to the real provider.** Every clone would then need a credential to run
the test suite, and the suite would stop being offline.

## References

- `packages/python/lab_assistant/config.py`
- `packages/python/lab_assistant/adapters/models/anthropic_provider.py`
- Remaining-tasks audit MIT-012
