# Design: where this goes next

This covers the parts I designed but did not build, and the safeguards that would have to exist before any of it touches real tenants.

## How it becomes a service

Nothing in the checker assumes a file. Each Story is checked on its own and keyed by a content hash, which is an idempotency key. So the same code becomes a per-event worker without a redesign:

| Prototype | Production |
|---|---|
| JSON file on disk | Story created or updated event |
| `state.json` | Keyed store, one row per tenant and story |
| `ai_semantic_cache.json` | Shared cache with a TTL |
| `run_log.jsonl` | Metrics pipeline |
| CLI invocation | Queue consumer |

Replaying an event is free, because an unchanged content hash short-circuits before any work happens. That is the property that makes it safe to run continuously.

## The full loop

```text
Story created or updated
        ->
Tenant policy + deterministic QA gate
        ->
PASS     -> publish normally
FAIL     -> hold or send back to the publisher (future policy, not v1)
WARNING  -> narrow AI semantic review
        ->
High confidence            -> recommendation shown to the publisher
Low confidence / high risk -> human review queue
        ->
Reviewer decision stored as tenant-scoped labelled feedback
        ->
Evaluate rules and prompts offline before changing anything
```

That last arrow is deliberately not automatic. Feedback tells you whether a check is any good. It does not quietly change what production does.

## How feedback reduces the review queue

Human feedback should make the system better, but it should not let the system change itself. Each review decision is saved for that tenant with the finding, the Story evidence, and the rule or prompt version that created it.

This shows me where the system is working and where it is wasting people's time:

- a low-risk recommendation that reviewers consistently accept can become a clearer rule;
- an AI decision that reviewers often overturn can lead to a better prompt or evaluation;
- a finding that is usually wrong can be narrowed or removed.

Before any change goes live, I would test it against previously reviewed examples. A person approves it first, it is released as a versioned change, and it can be rolled back. That gradually reduces repeated manual work while keeping new or risky cases with a human.

## Multi-tenant safeguards

These would block a launch if they were wrong, so I decided them now instead of later.

- **Cache key.** `hash(tenant_id, policy_version, prompt_version, cta, url_path)`. A cache keyed only on content would apply one tenant's decision under another tenant's policy. Tenant is in the key in the code today, not just in this document.
- **Feedback isolation.** A reviewer decision belongs to one tenant. Any cross-tenant learning uses aggregate per-check precision numbers only. Never tenant content, and never embeddings derived from tenant content. I would not mix one customer's copy into another tenant's model, even with opt-in, because it weakens isolation and creates avoidable data-sharing risk.
- **Stored per decision:** `tenant_id`, `finding_id`, the original evidence, the decision, the reviewer, a timestamp, and the checker and prompt versions. Without the versions you cannot attribute feedback to the thing that produced it, and it is useless for measurement.
- **Third-party models.** Sending Story text to an external model makes that vendor a sub-processor. Before that ships: no-train controls, documented retention limits, sensible data residency, and per-tenant opt-out. Titles and CTAs can contain personal data, so the request carries only the fields the review actually needs.
- **Access.** Findings are visible only inside the tenant they belong to.

## Why link health is not in v1

Reachability is the check everyone asks for first, and it is the one you cannot do casually. Doing it safely means protocol allowlisting, resolving every A and AAAA record and blocking private, loopback, link-local and cloud metadata ranges, refusing to follow redirects, timeouts, per-domain rate limits so you never hammer a customer's own site, and caching the results. That is an async worker with a queue and a network policy, not a function call.

It is worth building. Pew Research found roughly a quarter of pages that existed between 2013 and 2023 are gone, and 23% of news pages have at least one broken link. It is just not a two-hour feature, and a naive version would give confident wrong answers about a customer's own content.

## What I would build first with engineering support

1. **Rolling per-tenant window** for the cross-Story checks, replacing batch scope. Everything else is already stream-shaped.
2. **Tenant policy management**, so allowed hosts and vocabulary belong to the customer instead of a JSON file in a repo.
3. **Async link-health worker** with the controls above.
4. **Human review queue** with the feedback schema, so per-check precision gets measured before anyone argues about blocking.

## What I would deliberately not build yet

- Automatic blocking. Not until per-check precision is measured on real traffic.
- Any retraining or self-modifying policy.
- Visual analysis of the image or video content.
- Registrable-domain matching using an embedded copy of the Public Suffix List. The list changes, and a stale copy fails in the direction of trusting a host it should not.
- Phishing or fraud detection. This gate checks whether a tenant published its **own** content correctly. Abuse detection is a different product with different data, different service levels and different liability, and mixing them makes both worse.

## A note on which checks I picked

I picked checks for what actually goes wrong at volume, not for what is easy to list. `missing_story_id` will basically never fire against a real CMS, and it is only in here because malformed payloads do reach ingestion. The checks that earn their place are the editorial ones: a draft title that shipped, a CTA that promises one thing and links to another, a destination on a lookalike host, an asset silently reused.
