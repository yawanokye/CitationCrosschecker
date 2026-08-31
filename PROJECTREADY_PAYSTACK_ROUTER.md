# ProjectReady central Paystack webhook update

Keep this Paystack dashboard webhook unchanged:

`https://projectreadyai.com/api/paystack/webhook`

Update the code behind that endpoint so it performs these steps in order:

1. Verify `X-Paystack-Signature` against the exact request body using the
   existing Paystack secret.
2. Read `data.reference` and `data.metadata.source_app`.
3. Process `PRJ-` or `source_app=projectready` payments with the existing
   ProjectReady payment function.
4. Forward `CIT-` or `source_app=citeintegrity` `charge.success` events to
   CiteIntegrity.
5. Return HTTP 200 for duplicate successful notifications. Both applications
   must keep activation idempotent.

## Forwarding pattern

```python
import hashlib
import hmac
import os
import requests

confirmation_secret = os.environ["PAYSTACK_CONFIRMATION_SECRET"]

# raw_body must be the exact Paystack request body already signature-verified
# by ProjectReady. Do not rebuild or reformat the JSON before signing it.
signature = hmac.new(
    confirmation_secret.encode("utf-8"),
    raw_body,
    hashlib.sha256,
).hexdigest()

response = requests.post(
    "https://citeintegrity.org/api/paystack/payment-confirmation",
    data=raw_body,
    headers={
        "Content-Type": "application/json",
        "X-CiteIntegrity-Signature": signature,
    },
    timeout=30,
)
response.raise_for_status()
```

Add the same long random `PAYSTACK_CONFIRMATION_SECRET` to the Render
environment of both ProjectReady and CiteIntegrity. Never send the Paystack
secret itself between the applications.

CiteIntegrity will independently call Paystack to verify the transaction and
will compare its reference, amount, currency, email and metadata with the
stored pending purchase before unlocking the result.
