# Local E2E setup metadata

Each `<run_id>/` directory holds redacted setup plans, infrastructure preflights,
public caller manifests and setup verification. These files are prerequisites or
historical setup evidence, not test results or proof that a scenario passed.
Private credentials/resumable inputs stay under ignored `.e2e/<run_id>/`.

The initializer now writes here. Older records may reference
`reports/e2e/comprehensive/<run_id>/{manifest,preflight,setup-plan,verification}.json`;
those setup files were moved here with unchanged contents. Historical journals
and reports remain in their original comprehensive/narrow report categories.
Use a fresh run ID after configuration/code/data changes; moving a manifest does
not make its old configuration fingerprint current.
