# CiteIntegrity GHS/USD Pricing and Paystack Implementation

## Pricing display

Applicants should be able to choose either GHS or USD.

| Package | GHS | USD | Runs |
|---|---:|---:|---:|
| Article Full Review | GHS 35 | USD 2.99 | 2 |
| Research Paper Full Review | GHS 70 | USD 5.99 | 2 |
| Thesis Full Review | GHS 150 | USD 12.99 | 2 |
| PhD / Large Document Full Review | GHS 285 | USD 24.99 | 2 |

These are fixed prices, not live FX conversions.

## Payment behaviour

- If applicant chooses GHS, Paystack initializes payment with `currency: "GHS"`.
- If applicant chooses USD, Paystack initializes payment with `currency: "USD"`.
- Paystack amount is always sent in subunits.
- GHS 35.00 becomes 3500.
- USD 2.99 becomes 299.

## Paystack requirement for USD

USD checkout will work only if USD is enabled on your Paystack account and you have added a USD settlement account where required.

## Suggested UI wording

Choose your preferred payment currency.

Prices are shown in both Ghana cedis and US dollars. You may pay in either currency, depending on the payment option available to you.

## Files

- `entitlements_multi_currency.py`
- `access_control_multi_currency.py`
- `paystack_payments_multi_currency.py`
- `purchases_schema_multi_currency.sql`
- `main_paystack_multi_currency_snippets.py`

Rename the Python files before deployment:

- `entitlements_multi_currency.py` → `entitlements.py`
- `access_control_multi_currency.py` → `access_control.py`
- `paystack_payments_multi_currency.py` → `paystack_payments.py`
