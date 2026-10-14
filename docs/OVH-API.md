# OVH catalogue, orders and permissions

The order adapter follows the official schema read on 2026-10-14:
https://eu.api.ovh.com/1.0/order.json. Prices/plan codes are fetched, not fixed.
The credentials can belong to any supported EU/CA/US account; subsidiary,
endpoint, expected VPS service and named secret references live in a profile.

## Minimal capabilities by function

Read inventory / reverse:

- GET /ip, GET /ip/*, GET /vps/*
- POST /ip/*/reverse, DELETE /ip/*/reverse/* for provisioned PTR changes

Prepare catalogue/quotes (not purchases):

- GET /order/cartServiceOption/vps/*
- POST /order/cart, POST /order/cart/*/assign
- GET /order/cart/*/vps, POST /order/cart/*/vps
- POST /order/cartServiceOption/vps/*
- GET /order/cart/*/item/*/requiredConfiguration
- GET/POST /order/cart/*/item/*/configuration
- GET /order/cart/*/checkout

Buy after human approval:

- POST /order/cart/*/checkout

Read fulfillment:

- GET /me/order/*, GET /me/order/*/status
- GET /me/order/*/details, GET /me/order/*/details/*

Use credentials restricted as narrowly as OVH's rights patterns permit.
For a technical hard boundary against buying, omit POST checkout rights as
well as setting VWAN_ENABLE_PURCHASES=false. No purchase permission is needed
for importing known infrastructure into the UI.

## Cart flow

1. POST /order/cart with the configured subsidiary, assign that cart.
2. Existing router IP option: POST /order/cartServiceOption/vps/{serviceName}
   with cartId, planCode, duration, pricingMode and quantity.
3. New router: POST /order/cart/{cartId}/vps with the selected offering.
4. Fetch requiredConfiguration, apply explicitly provided label/value items.
5. GET checkout for prices and contracts, store a 15-minute app quote.
6. The authenticated human confirms the quote, accepting contracts and
   selecting whether preferred-payment auto-pay is permitted.
7. Re-GET checkout. A change to prices/contracts/line items blocks approval.
8. Audit the claim and send POST checkout once. A transport error/crash is
   uncertain, not retryable. No background code can submit a fresh checkout.
9. Read order status/details. Only delivered products linked to the specific
   order enter the combined deployment path. The IP must also appear routed
   to this profile's service. Ambiguous fulfillment remains for review.

An order URL is not proof of payment/delivery. With auto-pay off, finish
payment at OVH. The withdrawal-waiver flag is false; if the supplier refuses
an offering under those conditions, inspect the account/cart instead of
automatically changing a legal acceptance parameter.

The first real purchase through this new app has not been executed during
development. A supplier sandbox/controlled human-approved transaction is
still required to verify offering-specific data and fulfillment details.
API access requirements may evolve; check the actual account's schema/catalogue.
