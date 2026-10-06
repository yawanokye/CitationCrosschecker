# CiteIntegrity 2.0.16: Dashboard approval and control fixes

Prepared: 6 October 2026. This complete archive includes the previous fixes through 2.0.15. Deploy it to update the running service.

## Confirmed causes

The same source was displayed in several sections with the same radio-button group name. Selecting a candidate in one section could clear the selection in another. Result refreshes rebuilt the cards and discarded source selections and checked review confirmations. Candidate cards also used nested HTML labels.

Correction errors appeared in the Evidence Resolution card and top status line, which could be outside the section where the user clicked. Manual-decision buttons were locked and labelled successful before the server confirmed the save. The feature chips described functions but did not navigate to them. Fix next issue could scroll to a card while its section was hidden.

The supplied access logs confirm mixed HTTP 422 and HTTP 200 decision responses. They do not contain the rejection bodies, so they cannot establish which production approval guard rejected each request. This release preserves those guards and displays their messages beside the affected controls.

## Changes

1. Give source choices separate radio groups in Evidence Resolution, Manual Verification and the Claim Support renderer. Use valid, separate labels for each radio and confirmation.
2. Preserve selected candidates, opened-source state and fit, author and publication-year confirmations through result refreshes. Identify each reviewed candidate by its bibliographic metadata rather than its position in the list. Candidate reordering preserves the choice. Changed metadata requires fresh confirmation.
3. Display saving, success and rejection messages beside each visible copy of the correction. Keep rejected decisions pending and restore retry controls. Do not claim success when an HTTP 200 response omits the updated correction plan.
4. Prevent duplicate correction requests across sections. Preserve busy states when the cards refresh during a save.
5. Save manual decisions before applying successful labels or locking other choices. Show failures on the manual-review card and restore its choices. Follow the reference when a server refresh reorders rows. Clarify that a recorded manual assessment and approval of a tracked Word replacement are separate actions.
6. Make the eight feature chips native buttons that open the appropriate section or control. Fix next issue opens Evidence Resolution before scrolling to a pending item.

Author completeness, online versus final publication-year review, source identity, claim support and exact Word-placement checks remain required. HTTP 422 can still be an appropriate rejection. The previously fixed direct downloads and both download/delete choices are retained. This release does not change verification matching, publication-status data, payment pricing or payment routing. The verification cache namespace remains `v8-reference-metadata-safety`.

## Files to replace relative to the application root

| File | Change relative to 2.0.15 |
| --- | --- |
| `templates/new_results.html` | Approval state, visible feedback, retry and duplicate guards, manual decisions and navigation. |
| `new_results.html` | Identical dashboard copy for deployments using the root template. |
| `main.py` | Release identity only. |
| `worker.py` | Release identity only. |
| `verify.py` | Release identity only. |
| `.env.example` | Release-setting example. Preserve live credentials and settings. |

Also included: this `RELEASE_2_0_16.md`, the new `test_dashboard_controls.cjs`, and updated `test_approval_error_dashboard.cjs`, `test_reference_review_dashboard.cjs`, `test_reference_metadata_dashboard.cjs`, and `test_product_release.py`.

For an installation older than 2.0.15, deploy the complete application archive so the preceding report-package and source-review fixes are also included.

## Deployment and checks

1. Deploy the application files together and set `RELEASE_VERSION=2.0.16-dashboard-controls`. Preserve payment secrets, scholarly API keys, access settings and existing environment values.
2. Restart the web service and active workers. Confirm the release at `/health` and reload the results page to load the new script.
3. In Manual Verification, select and open a candidate, complete the required confirmations, then refresh. The choice and confirmations should remain. A changed candidate or edited metadata requires review again.
4. Approve a source. During the save, its correction controls should be busy in every section. A successful server response shows approval. A rejection shows its exact reason beside the card and permits retry after the underlying issue is corrected.
5. Check a manual decision, Reject, Ignore, the feature buttons, Fix next issue and Download only. Open the tracked Word document to inspect approved changes. Manual assessments alone do not replace references.

## Validation

- Python regression suite: 231 tests and 4 subtests passed.
- All seven JavaScript checks passed. The new DOM test executes the full production dashboard script with its real event handlers and simulated server responses. It covers duplicate-section radios, refresh persistence, candidate reordering, changed-metadata resets, local HTTP 422 errors, malformed success responses, duplicate clicks, refresh during saving, successful source approval, claim approval, Reject/Ignore, manual-decision failures and success, reordered manual rows, and feature navigation.
- Modified Python modules compile. Both dashboard copies are identical. The release ZIP passes checksum validation.

The DOM test uses jsdom 27 as a development dependency. Install it in a development environment and run `node test_dashboard_controls.cjs`. It is not needed by the deployed application.

These checks use simulated server responses for UI behavior and controlled fixtures for the Python routes. The running production service and the reported job have not been deployed or independently rerun by this work.
