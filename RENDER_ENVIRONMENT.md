# CiteIntegrity Render environment

Add these values to the CiteIntegrity **web service** in Render. Use **Secret**
for every key or password. Do not commit live values to the repository.

## Central Paystack routing

| Render key | Value |
|---|---|
| `APP_BASE_URL` | `https://citeintegrity.org` |
| `PAYSTACK_PUBLIC_KEY` | Same existing Paystack public key used by ProjectReady |
| `PAYSTACK_GH_SECRET_KEY` | Same existing Ghana Paystack secret key used by ProjectReady |
| `PAYSTACK_NG_SECRET_KEY` | Use the same secret if your Paystack account is enabled for NGN, otherwise use the separate Nigeria-market key |
| `PAYSTACK_CALLBACK_URL` | `https://citeintegrity.org/payment/callback` |
| `PAYSTACK_REFERENCE_PREFIX` | `CIT` |
| `PAYSTACK_SOURCE_APP` | `citeintegrity` |
| `PAYSTACK_CONFIRMATION_SECRET` | A new long random secret shared only with ProjectReady |

Generate the confirmation secret locally with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the same generated value in ProjectReady. ProjectReady uses it to sign the
exact Paystack webhook body forwarded to CiteIntegrity in the
`X-CiteIntegrity-Signature` header.

## International Stripe routing

| Render key | Value |
|---|---|
| `STRIPE_SECRET_KEY` | Stripe live secret key |
| `STRIPE_WEBHOOK_SECRET` | Stripe signing secret for CiteIntegrity's webhook |

## Required commercial service values

| Render key | Recommended value |
|---|---|
| `GLOBAL_ACCESS_MODE` | `payment_required` |
| `PAYMENT_COOKIE_SECURE` | `true` |
| `DEMO_UNLOCK_ALL_FEATURES` | `false` |
| `DEVELOPER_USERNAME` | Private administrator username |
| `DEVELOPER_PASSWORD` | Long unique password |
| `DEVELOPER_SESSION_SECRET` | Long random secret |
| `DATABASE_URL` | CiteIntegrity PostgreSQL connection string |
| `REDIS_URL` | CiteIntegrity Redis connection string |

The existing Paystack dashboard webhook remains:

`https://projectreadyai.com/api/paystack/webhook`

Do not replace it with a CiteIntegrity webhook URL. ProjectReady must route
`CIT-` events to `https://citeintegrity.org/api/paystack/payment-confirmation`.
The server-side forwarding pattern is documented in
`PROJECTREADY_PAYSTACK_ROUTER.md`.
