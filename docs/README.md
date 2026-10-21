# Documentation index

| Folder | Contents |
| --- | --- |
| [guides/](guides/) | Feature behavior and operator guides: authentication, background processing, routing, clarification, monitoring and human review |
| [plans/](plans/) | Original requirements and implementation plans; plans describe intended work and are not proof that it was completed |
| [testing/](testing/README.md) | Semantic runbook, testing coverage and explicitly marked legacy broad runbooks |
| [history/](history/) | Dataset review notes and the historical session excerpt |

[Project completion report](project_completion_report.md) summarizes the system,
the main implementation corrections, measured test evidence and resume-ready
project wording.

Start with the project [README](../README.md). The [E2E suite plan](plans/e2e_suite_plan.md) inventories current scripts and seeders and specifies the proposed comprehensive suite. The [reports index](../reports/README.md) catalogs recorded evidence separately from plans.

Common guides: [authentication](guides/authentication.md), [background processing](guides/background_processing.md), [LLM routing](guides/llm_routing.md), [human review](guides/human_review_finalization.md), [clarification](guides/request_clarification.md), and [monitoring](guides/request_attempt_monitoring.md).

Folder placement does not certify freshness: check a guide's prerequisites against current code. Legacy testing documents carry an explicit warning; historical reports retain their original content.

The current [agent workflow](../AGENT_WORKFLOW.md) defines source ownership,
structured temporary work and final cleanup. The original submission workflow is
preserved as [historical documentation](history/ai_workflow_submission_20261007.md).
