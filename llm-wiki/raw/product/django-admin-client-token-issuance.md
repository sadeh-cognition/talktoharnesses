# Django admin client token issuance

- **Approved:** 2026-08-21
- **Source:** User-requested product change

## Product request

Add a Django admin form for generating a JWT token per client so remote clients
can be provisioned without writing a custom in-process script.

## Requirements

- A client is represented by an active host-owned Django user; TTH does not add
  a separate client identity model.
- An authorized Django administrator can select that user and issue its JWT from
  the standard Django admin.
- Admin issuance uses the existing trusted token issuance operation and its
  one-active-token-per-user rule.
- The raw token is shown only in the immediate issuance response and is never
  persisted by TTH.
- The response containing the token must not be cached.
- This capability does not add an unauthenticated HTTP token endpoint or
  package-owned login and user-management flows.
