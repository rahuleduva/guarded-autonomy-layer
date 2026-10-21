# Testing documentation

- [Current E2E runbook](E2E_RUNBOOK.md): shared initialization, scenario selection, reports and explicit coverage gaps.
- [E2E suite implementation plan](../plans/e2e_suite_plan.md): proposed shared initializer, scenario registry, focused runners, coverage matrix and reporting.
- [Semantic pipeline runbook](PHASE12_RUNBOOK.md): semantic-specific configuration, probes and diagnostics; it does not cover the entire application.
- [Recorded E2E evidence](../../reports/e2e/README.md): historical broad runs and recent focused runs, with their limits.
- [Script inventory](../../scripts/README.md): runnable tools and their locations.

`legacy/` contains the older [interactive plan](legacy/ONLINE_E2E_PLAN.md), [granular curl runbook](legacy/ONLINE_E2E_GRANULAR_RUNBOOK.md), and [human-review checklist](legacy/HUMAN_REVIEW_E2E.md). They preserve useful scenarios but must be reconciled with current contracts before execution. Moving these files does not refresh their old expectations or constitute a new broad test run.
