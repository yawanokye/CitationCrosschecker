# Zero-downtime CiteIntegrity upgrades

Use two independent deployments behind the stable public domain. Application
maintenance mode is for occasions when public processing must stop; it is not a
substitute for blue-green deployment.

## Deployment slots

- **Blue** is the version currently receiving public traffic.
- **Green** is the new version, available only through a protected preview URL.
- Set `RELEASE_SLOT` and `RELEASE_VERSION` differently on each service. Both are
  visible in `/health` and `/developer/access`, preventing tests against the
  wrong release.
- Protect the green URL with platform access control as well as CiteIntegrity's
  developer Basic authentication. Do not rely on an unlisted URL for security.

The two services may use the same production database only when all schema
changes are backward-compatible. Use expand-and-contract migrations: add new
tables or nullable columns first, deploy compatible code, switch traffic, and
remove old structures only in a later release. Give blue and green separate
Redis queue names or separate Redis instances so jobs are never processed by a
worker running incompatible code.

## Promotion checklist

1. Deploy the upgrade to green while the public continues using blue.
2. Confirm green's release identity in `/health`.
3. Sign in through `/developer/access` and test upload, analysis, payment
   entitlement, correction decisions, exports, deletion, and the second run.
4. Run smoke tests and verify workers, Redis, database, email, AI rewrite (when
   enabled), and payment sandbox callbacks.
5. Switch the stable domain or load-balancer target from blue to green.
6. Keep blue available for a short rollback window, but stop its workers from
   consuming new green jobs.
7. If production checks succeed, make green the new blue baseline. If they fail,
   route traffic back to the previous slot.

The traffic switch belongs to the hosting platform or reverse proxy. It should
not be performed by an unauthenticated application endpoint. If the hosting
provider supports weighted traffic, first send a small percentage to green and
increase it after monitoring errors and processing completion rates.
