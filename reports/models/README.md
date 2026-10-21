# Model and provider experiments

This folder contains dated LLM prompt benchmarks, structured-output format probes, and Sarvam stage smoke runs. They compare narrow requests against unreviewed synthetic expectations or check whether a provider can return a small structured response.

These files are useful for debugging provider compatibility and inspecting past attempts. They are not a ranking of model quality or a production reliability claim. The expected labels were not sent to the model, but the case labels themselves were not independently reviewed.

The active provider and model routes are configured in [`../../config/llm_routing.yaml`](../../config/llm_routing.yaml). Use the reports as dated evidence and rerun a benchmark against the current routes before making a model-selection claim.
