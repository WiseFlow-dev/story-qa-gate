# Story QA Gate Report

`tenant_antarctic_league_001` | checker 1.0.0 | policy 1.0.0 | 2026-08-04T18:12:18Z

**2 stories | 5 pages | 0 failures | 3 warnings | 3 need human review**

Delta: 2 new | 0 changed | 0 unchanged | 0 carried forward

## Needs review

- **story_amb_rule / page_1** `cta_destination_mismatch`
  - WARNING: CTA 'Buy tickets' appears inconsistent with destination path '/highlights'.
  - Risk: high | Confidence: medium | Source: rule
- **story_amb_ai / page_1** `semantic_review_uncertain`
  - WARNING: The destination path is an opaque numeric identifier that carries no readable intent, so the CTA cannot be confirmed or contradicted from the URL alone.
  - Risk: high | Confidence: low | Source: ai_semantic_review
- **story_amb_ai / page_3** `cta_destination_mismatch`
  - WARNING: The CTA promises editorial squad content but the destination is a season ticket sales page, so the reader would not land on what was offered.
  - Risk: high | Confidence: high | Source: ai_semantic_review

## Coverage

- Pages of a type this version does not assess: 0
- Pages where rules abstained on CTA intent: 4
- Of those, reviewed by the semantic model: 4

_Link reachability is not checked in v1. Calling a URL broken without an
actual HTTP request would be inventing a result._

## Performance

- 2 stories in 0.00s (427 stories/sec)
- Peak traced memory: 0.1 MB

