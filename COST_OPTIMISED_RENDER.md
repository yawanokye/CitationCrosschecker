# Cost-optimised Render worker setup

The launch architecture needs four queues, not four always-on worker services.
An RQ worker can listen to several queues in priority order.

## Recommended launch setup

Keep two background services:

| Render service | Start command | Queues | Keep running? |
|---|---|---|---|
| `citeintegrity-worker-1` | `python core_worker.py` | document, verification, large-document fallback | Yes |
| `citeintegrity-deep-enrichment-worker` | `python deep_worker.py` | paid deep enrichment | Yes |
| `citeintegrity-worker-2` | existing command | duplicate core capacity | Suspend |
| `citeintegrity-worker-3` | existing command | duplicate core capacity | Suspend |

This reduces four always-on workers to two without removing an application
feature. One core worker processes jobs serially, which is appropriate for low
launch traffic. Resume worker 2 when normal uploads regularly wait, and worker 3
only after worker 2 is consistently busy.

## Large documents

The safe default keeps `large_document_processing` on the core worker so a PhD
document can never sit in an unserved queue. It can block smaller work while it
runs. For lower idle cost plus better isolation, configure the existing Render
one-off launcher and test it before changing:

`CORE_WORKER_QUEUES=document_processing,verification`

Required one-off variables are `LARGE_WORKER_AUTOSTART_ENABLED=true`,
`RENDER_API_KEY`, `RENDER_LARGE_WORKER_BASE_SERVICE_ID`, and the applicable plan
and command variables documented in `render_large_worker_autostart.py`.

## One-worker emergency mode

The absolute cheapest configuration is one worker with:

`WORKER_QUEUES=document_processing,verification,deep_enrichment,large_document_processing`

That is not recommended for paid launch: one long enrichment or thesis job can
delay every new upload and verification. Use it only temporarily when traffic is
near zero and monitor `/queue/status`.

## Scale triggers

- Resume worker 2 when a core queue has waiting jobs for more than five minutes
  or the core worker is busy most of the hour.
- Resume worker 3 only when two core workers cannot clear the queue promptly.
- Keep the dedicated deep worker because enrichment may run for a long time and
  should not block the paid preview-to-review path.
- `/queue/status` reports queue depths, active workers, listened queues, and an
  explicit warning when a waiting queue has no listener.
