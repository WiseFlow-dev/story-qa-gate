#!/usr/bin/env python3
"""
Story QA Gate
=============

A small automatic content-quality gate for Story-style media items.

Design, in one line: cheap deterministic rules decide what they can prove,
they abstain when they cannot, and only the abstained subset is ever shown
to a model. Humans see only high-risk or low-confidence outcomes.

Run:
    python qa_gate.py sample_stories.json --out output

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import tracemalloc
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

CHECKER_VERSION = "1.0.0"

# Severity
FAIL = "fail"
WARNING = "warning"
PASS = "pass"

# Risk. A wrong outbound destination is user-visible and reputational.
# A metadata problem is not. This is what routes the human review queue.
RISK_HIGH = "high"
RISK_LOW = "low"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".m3u8"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def story_content_hash(story: dict) -> str:
    """Stable hash of a Story's content. This is the idempotency key.

    In production this is what lets the same code run as a per-event worker:
    an unchanged Story is never re-evaluated, and replaying an event is free.
    """
    canonical = json.dumps(story, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


# ---------------------------------------------------------------------------
# URL parsing
#
# urllib.parse does NOT validate. Python's own docs say to "code defensively"
# and verify components before trusting them, so every component is checked
# explicitly below rather than assumed.
# ---------------------------------------------------------------------------

def parse_url(raw: str):
    """Return (host, path, error) where error is None when the URL is usable.

    Deliberately narrow: https only, real hostname, no embedded credentials.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None, "url is empty"

    try:
        parts = urlsplit(raw.strip())
    except ValueError as exc:                      # e.g. unmatched brackets in netloc
        return None, None, "url could not be parsed: %s" % exc

    if parts.scheme.lower() != "https":
        return None, None, "url must use https"

    try:
        host = parts.hostname
    except ValueError as exc:                      # malformed IPv6 / IDNA
        return None, None, "url host could not be parsed: %s" % exc

    if not host:
        return None, None, "url has no hostname"

    if parts.username or parts.password:
        # https://trusted.example@attacker.example/ renders as "trusted" to a human.
        return None, None, "url must not contain embedded credentials"

    # urlsplit only validates the port when you actually read it. Without this,
    # https://host:99999/ and https://host:notaport/ both look perfectly fine
    # here and then fail in a browser.
    try:
        parts.port
    except ValueError:
        return None, None, "url has an invalid port"

    host = host.lower().rstrip(".")
    return host, parts.path or "/", None


def host_allowed(host: str, allowed_hosts) -> bool:
    """Exact hostname equality only.

    Deliberately NOT suffix matching. `endswith("example.com")` would also
    accept `evil-example.com` and `example.com.attacker.net`. Matching the
    registrable domain properly needs a maintained Public Suffix List, which
    is future work, so every permitted subdomain is listed explicitly.
    """
    return host in {h.lower().rstrip(".") for h in allowed_hosts}


def url_extension(path: str) -> str:
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return ""
    return "." + tail.rsplit(".", 1)[-1].lower()


# ---------------------------------------------------------------------------
# intent classification (rules)
# ---------------------------------------------------------------------------

def _has_term(text: str, term: str) -> bool:
    """Whole-word match. Substring matching classified "Follow us on Facebook"
    as ticketing, because "Facebook" contains "book"."""
    return re.search(r"(?<!\w)%s(?!\w)" % re.escape(term), text) is not None


def classify_cta(cta: str, buckets: dict):
    text = normalise_text(cta)
    hits = {name for name, spec in buckets.items()
            if any(_has_term(text, term) for term in spec.get("cta_terms", []))}
    return hits.pop() if len(hits) == 1 else None


def classify_path(path: str, buckets: dict):
    segments = [s for s in normalise_text(path).split("/") if s]
    if not segments:
        return None
    hits = {name for name, spec in buckets.items()
            if any(term in segments for term in spec.get("path_terms", []))}
    return hits.pop() if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# semantic review (the ONLY place a model is involved)
# ---------------------------------------------------------------------------

class RecordedSemanticReviewer:
    """Replays real model responses recorded in ai_semantic_cache.json.

    The responses in the cache were produced by a real model. They are
    committed so the repository, the tests and the report are reproducible
    with no API key and no network. A live reviewer would implement the same
    review() signature and write its result back into this same cache.
    """

    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        self.entries = {}
        self.hits = 0
        self.misses = 0
        if cache_path and os.path.exists(cache_path):
            data = load_json(cache_path)
            for entry in data.get("entries", []):
                self.entries[entry["cache_key"]] = entry

    @staticmethod
    def cache_key(tenant_id, policy_version, prompt_version, cta, path) -> str:
        # Tenant is part of the key on purpose. A global cache keyed only on
        # (cta, path) would apply one tenant's decision under another tenant's
        # policy, which is a cross-tenant leak.
        raw = "|".join([tenant_id, policy_version, prompt_version,
                        normalise_text(cta), normalise_text(path)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def review(self, tenant_id, policy_version, cta, path):
        key = self.cache_key(tenant_id, policy_version, "semantic_review_v1", cta, path)
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry["output"]


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def finding(story_id, page_id, status, check_id, reason, risk,
            confidence="high", evidence=None, source="rule"):
    return {
        "story_id": story_id,
        "page_id": page_id,
        "status": status,
        "check_id": check_id,
        "confidence": confidence,
        "risk": risk,
        "reason": reason,
        "evidence": evidence or {},
        # A human is spent only on risk or doubt. A low-risk finding we are
        # confident about ("Read more" is a generic CTA) is a recommendation
        # the publisher can just act on. Routing those to review is how the
        # queue fills with things nobody needs to adjudicate.
        "needs_human_review": risk == RISK_HIGH or confidence in ("low", "medium"),
        "source": source,
    }


def check_page(story, page, page_index, policy, tenant_id, reviewer, coverage):
    """Evaluate one page. Returns (status, findings)."""
    story_id = story.get("story_id") or "<missing>"
    page_id = page.get("page_id")
    out = []

    if not page_id:
        page_id = "<index %d>" % page_index
        out.append(finding(story_id, page_id, FAIL, "missing_page_id",
                           "page_id is required", RISK_LOW))

    # --- asset -------------------------------------------------------------
    asset_url = page.get("asset_url")
    if not asset_url:
        out.append(finding(story_id, page_id, FAIL, "missing_asset_url",
                           "asset_url is required", RISK_HIGH))
    else:
        host, path, err = parse_url(asset_url)
        if err:
            out.append(finding(story_id, page_id, FAIL, "invalid_asset_url",
                               "asset_url is not usable: %s" % err, RISK_HIGH,
                               evidence={"asset_url": asset_url}))
        else:
            ext = url_extension(path)
            ptype = (page.get("type") or "").lower()
            # Only judge the extension when there is one. A signed or
            # extensionless CDN URL is not evidence of anything.
            if ext:
                if ptype == "image" and ext in VIDEO_EXTS:
                    out.append(finding(story_id, page_id, FAIL, "asset_type_mismatch",
                                       "image page points to a %s asset" % ext, RISK_LOW,
                                       evidence={"type": ptype, "asset_url": asset_url}))
                elif ptype == "video" and ext in IMAGE_EXTS:
                    out.append(finding(story_id, page_id, FAIL, "asset_type_mismatch",
                                       "video page points to a %s asset" % ext, RISK_LOW,
                                       evidence={"type": ptype, "asset_url": asset_url}))

    # --- page type coverage ------------------------------------------------
    ptype = (page.get("type") or "").lower()
    if ptype not in [t.lower() for t in policy.get("assessed_page_types", [])]:
        # Coverage metric, NOT a finding. Reporting "I do not assess embeds"
        # once per embed page forever would drown the review queue in noise.
        coverage["page_types_not_assessed"] += 1

    # --- action ------------------------------------------------------------
    action = page.get("action") or {}
    cta = action.get("cta")
    action_url = action.get("url")

    if action:
        if cta and not action_url:
            out.append(finding(story_id, page_id, FAIL, "incomplete_action",
                               "CTA has no destination URL", RISK_HIGH,
                               evidence={"cta": cta}))
        elif action_url and not cta:
            out.append(finding(story_id, page_id, FAIL, "incomplete_action",
                               "destination URL has no CTA label", RISK_HIGH,
                               evidence={"url": action_url}))

    if cta and action_url:
        host, path, err = parse_url(action_url)
        if err:
            out.append(finding(story_id, page_id, FAIL, "invalid_action_url",
                               "action URL is not usable: %s" % err, RISK_HIGH,
                               evidence={"cta": cta, "url": action_url}))
        else:
            if not host_allowed(host, policy.get("allowed_action_hosts", [])):
                out.append(finding(story_id, page_id, WARNING, "action_domain_not_allowed",
                                   "destination host '%s' is not in allowed_action_hosts" % host,
                                   RISK_HIGH, evidence={"cta": cta, "url": action_url}))

            # Generic CTA. WCAG 2.4.4 requires that a link's purpose be
            # determinable from its text plus context. A full-bleed Story card
            # supplies no surrounding sentence, so "Read more" tells the
            # reader nothing about where they are going.
            if normalise_text(cta) in {normalise_text(g) for g in policy.get("generic_ctas", [])}:
                out.append(finding(story_id, page_id, WARNING, "generic_cta",
                                   "CTA '%s' does not explain its destination" % cta,
                                   RISK_LOW, confidence="high",
                                   evidence={"cta": cta, "url": action_url}))

            # CTA versus destination intent.
            buckets = policy.get("cta_intent_buckets", {})
            cta_bucket = classify_cta(cta, buckets)
            path_bucket = classify_path(path, buckets)

            if cta_bucket and path_bucket and cta_bucket != path_bucket:
                # Both sides classified confidently and they disagree. Provable
                # by rule, so no model is needed.
                out.append(finding(
                    story_id, page_id, WARNING, "cta_destination_mismatch",
                    "CTA '%s' appears inconsistent with destination path '%s'." % (cta, path),
                    RISK_HIGH, confidence="medium",
                    evidence={"cta": cta, "url": action_url,
                              "cta_intent": cta_bucket, "path_intent": path_bucket}))
            elif (cta_bucket is None or path_bucket is None) and not any(
                    f["risk"] == RISK_HIGH for f in out):
                # Rules abstain. THIS is the only doorway to the model, and it
                # is why the model is not called on every page.
                #
                # The second condition matters at volume: if a rule already
                # found something high risk on this page, a human is looking at
                # it regardless, so paying for a model call adds nothing.
                coverage["semantic_check_not_applicable"] += 1
                verdict = reviewer.review(tenant_id, policy.get("policy_version", "0"), cta, path)
                if verdict:
                    coverage["semantic_check_ai_reviewed"] += 1
                    if verdict["decision"] == "mismatch":
                        out.append(finding(
                            story_id, page_id, WARNING, "cta_destination_mismatch",
                            verdict["reason"], RISK_HIGH,
                            confidence=verdict["confidence"],
                            evidence={"cta": cta, "url": action_url},
                            source="ai_semantic_review"))
                    elif verdict["decision"] == "uncertain":
                        out.append(finding(
                            story_id, page_id, WARNING, "semantic_review_uncertain",
                            verdict["reason"], RISK_HIGH,
                            confidence=verdict["confidence"],
                            evidence={"cta": cta, "url": action_url},
                            source="ai_semantic_review"))

    status = FAIL if any(f["status"] == FAIL for f in out) else (
        WARNING if out else PASS)
    return {"story_id": story_id, "page_id": page_id, "status": status,
            "checks": [f["check_id"] for f in out]}, out


def check_story(story, story_index, policy, tenant_id, reviewer, coverage):
    story_id = story.get("story_id")
    findings = []
    pages_out = []

    if not story_id:
        story_id = "<index %d>" % story_index
        findings.append(finding(story_id, None, FAIL, "missing_story_id",
                                "story_id is required", RISK_LOW))

    title = story.get("story_title") or ""
    low = normalise_text(title)
    for marker in policy.get("placeholder_markers", []):
        if re.search(r"\b%s\b" % re.escape(marker.lower()), low):
            findings.append(finding(story_id, None, WARNING, "placeholder_copy",
                                    "story_title contains draft text '%s'" % marker,
                                    RISK_LOW, evidence={"story_title": title}))
            break

    pages = story.get("pages") or []
    if not pages:
        findings.append(finding(story_id, None, FAIL, "empty_story",
                                "story has no pages to publish", RISK_LOW))

    for i, page in enumerate(pages):
        page_row, page_findings = check_page(story, page, i, policy, tenant_id,
                                             reviewer, coverage)
        pages_out.append(page_row)
        findings.extend(page_findings)

    return pages_out, findings


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def load_state(path):
    if path and os.path.exists(path):
        try:
            return load_json(path)
        except (json.JSONDecodeError, OSError):
            pass
    return {"state_version": 1, "entries": {}}


def state_key(tenant_id, story_id) -> str:
    return "%s|%s" % (tenant_id, story_id)


# ---------------------------------------------------------------------------
# main evaluation
# ---------------------------------------------------------------------------

def run(snapshot, policy, state, reviewer):
    tenant_id = snapshot.get("tenant_id") or "unknown_tenant"
    stories = snapshot.get("stories") or []
    policy_version = policy.get("policy_version", "0")

    coverage = {"page_types_not_assessed": 0,
                "semantic_check_not_applicable": 0,
                "semantic_check_ai_reviewed": 0}

    delta = {"new": 0, "changed": 0, "unchanged": 0, "carried_forward": 0}
    pages_all, findings_all = [], []

    for index, story in enumerate(stories):
        story_id = story.get("story_id") or "<index %d>" % index
        digest = story_content_hash(story)
        key = state_key(tenant_id, story_id)
        # A state file that is valid JSON but missing "entries" used to crash
        # here. Only a truncated/invalid file was recovered from.
        prior = state.setdefault("entries", {}).get(key)

        reusable = (
            prior is not None
            and prior.get("content_hash") == digest
            and prior.get("checker_version") == CHECKER_VERSION
            and prior.get("policy_version") == policy_version
        )

        if reusable:
            # Unchanged. Do not re-evaluate, but DO render the last known
            # result, otherwise the report would silently empty out on every
            # warm run and look like a clean bill of health.
            delta["unchanged"] += 1
            delta["carried_forward"] += 1
            for row in prior.get("pages", []):
                row = dict(row)
                row["source"] = "carried_forward"
                pages_all.append(row)
            for f in prior.get("findings", []):
                f = dict(f)
                f["source"] = f.get("source", "rule") + "+carried_forward"
                findings_all.append(f)
            continue

        delta["new" if prior is None else "changed"] += 1

        pages_out, findings_out = check_story(story, index, policy, tenant_id,
                                              reviewer, coverage)
        for row in pages_out:
            row["source"] = "rechecked"
        pages_all.extend(pages_out)
        findings_all.extend(findings_out)

        state["entries"][key] = {
            "content_hash": digest,
            "checked_at": utc_now(),
            "checker_version": CHECKER_VERSION,
            "policy_version": policy_version,
            "pages": pages_out,
            "findings": findings_out,
        }

    # Snapshot-level check. Cross-Story duplicates cannot be judged from one
    # Story in isolation, so this runs fresh over the whole snapshot every
    # time, independently of carry-forward. In production this becomes a
    # rolling per-tenant window rather than a per-batch map.
    ids_seen = {}
    for index, story in enumerate(stories):
        sid = story.get("story_id")
        if sid:
            ids_seen.setdefault(sid, []).append(index)
    for sid, positions in ids_seen.items():
        if len(positions) > 1:
            # Two Stories sharing an id collide in the state store, which
            # silently breaks the unchanged/changed accounting for both.
            findings_all.append(finding(
                sid, None, FAIL, "duplicate_story_id",
                "story_id '%s' appears %d times in this snapshot" % (sid, len(positions)),
                RISK_HIGH, evidence={"positions": positions}, source="snapshot"))

    reusable = [p.lower() for p in policy.get("reusable_asset_patterns", [])]
    seen = {}
    for story in stories:
        sid = story.get("story_id")
        for page in story.get("pages") or []:
            url = page.get("asset_url")
            # A sponsor frame, club crest or league ident is meant to be reused.
            # Without this, every branded bumper becomes a recurring warning.
            if url and not any(pat in url.lower() for pat in reusable):
                seen.setdefault(url, set()).add(sid)
    for url, story_ids in seen.items():
        if len(story_ids) > 1:
            ordered = sorted(x for x in story_ids if x)
            findings_all.append(finding(
                ordered[0], None, WARNING, "duplicate_asset_across_stories",
                "asset is reused by %s" % ", ".join(ordered), RISK_LOW,
                evidence={"asset_url": url, "stories": ordered},
                source="snapshot"))

    statuses = [row["status"] for row in pages_all]
    report = {
        "checker_version": CHECKER_VERSION,
        "policy_version": policy_version,
        "tenant_id": tenant_id,
        "generated_at": utc_now(),
        "summary": {
            "stories_checked": len(stories),
            "pages_checked": len(pages_all),
            # Page-level rollup. Named explicitly, because not every finding
            # belongs to a page: empty_story and placeholder_copy are about the
            # Story, and duplicate_asset_across_stories is about the snapshot.
            # Counting only page statuses let the headline read "0 failures"
            # while the report listed a failure.
            "pages_pass": statuses.count(PASS),
            "pages_warning": statuses.count(WARNING),
            "pages_fail": statuses.count(FAIL),
            # Finding-level totals. This is what a human means by "how many
            # problems are there", and it counts every level.
            "fail": sum(1 for f in findings_all if f["status"] == FAIL),
            "warning": sum(1 for f in findings_all if f["status"] == WARNING),
            "high_risk_findings": sum(1 for f in findings_all if f["risk"] == RISK_HIGH),
            "needs_human_review": sum(1 for f in findings_all if f["needs_human_review"]),
        },
        "delta": delta,
        "coverage": coverage,
        "ai": {"cache_hits": reviewer.hits, "cache_misses": reviewer.misses},
        "pages": pages_all,
        # Highest risk first. At volume the question is never "show me
        # everything", it is "show me the five that matter".
        "findings": sorted(findings_all,
                           key=lambda f: (f["risk"] != RISK_HIGH, f["status"] != FAIL)),
    }
    return report


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def render_markdown(report, perf=None, limit=20):
    s = report["summary"]
    d = report["delta"]
    c = report["coverage"]
    failure_label = "failure" if s["fail"] == 1 else "failures"
    warning_label = "warning" if s["warning"] == 1 else "warnings"
    review_label = "needs" if s["needs_human_review"] == 1 else "need"
    lines = [
        "# Story QA Gate Report",
        "",
        "`%s` | checker %s | policy %s | %s" % (
            report["tenant_id"], report["checker_version"],
            report["policy_version"], report["generated_at"]),
        "",
        "**%d stories | %d pages | %d %s | %d %s | %d %s human review**" % (
            s["stories_checked"], s["pages_checked"], s["fail"], failure_label,
            s["warning"], warning_label, s["needs_human_review"], review_label),
        "",
        "Delta: %d new | %d changed | %d unchanged | %d carried forward" % (
            d["new"], d["changed"], d["unchanged"], d["carried_forward"]),
        "",
    ]

    review = [f for f in report["findings"] if f["needs_human_review"]]
    if not review:
        lines += ["## Needs review", "", "Nothing. Every page passed.", ""]
    else:
        lines += ["## Needs review", ""]
        for f in review[:limit]:
            where = "%s / %s" % (f["story_id"], f["page_id"] or "story")
            lines += [
                "- **%s** `%s`" % (where, f["check_id"]),
                "  - %s: %s" % (f["status"].upper(), f["reason"]),
                "  - Risk: %s | Confidence: %s | Source: %s" % (
                    f["risk"], f["confidence"], f["source"]),
            ]
        if len(review) > limit:
            lines.append("")
            lines.append("_...and %d more in `quality_report.json`._" % (len(review) - limit))
        lines.append("")

    # Low risk and high confidence. The publisher can just fix these; there is
    # nothing for a reviewer to decide, so they stay out of the review queue.
    recs = [f for f in report["findings"] if not f["needs_human_review"]]
    if recs:
        lines += ["## Recommendations (no review needed)", ""]
        for f in recs[:limit]:
            lines.append("- %s / %s `%s`: %s" % (
                f["story_id"], f["page_id"] or "story", f["check_id"], f["reason"]))
        if len(recs) > limit:
            lines.append("- _...and %d more._" % (len(recs) - limit))
        lines.append("")

    lines += [
        "## Coverage",
        "",
        "- Pages of a type this version does not assess: %d" % c["page_types_not_assessed"],
        "- Pages where rules abstained on CTA intent: %d" % c["semantic_check_not_applicable"],
        "- Of those, reviewed by the semantic model: %d" % c["semantic_check_ai_reviewed"],
        "",
        "_Link reachability is not checked in v1. Calling a URL broken without an",
        "actual HTTP request would be inventing a result._",
        "",
    ]

    if perf:
        lines += [
            "## Performance",
            "",
            "- %d stories in %.2fs (%.0f stories/sec)" % (
                perf["stories"], perf["seconds"], perf["rate"]),
            "- Peak traced memory: %.1f MB" % perf["peak_mb"],
            "",
        ]
    return "\n".join(lines)


def append_run_log(path, report, perf):
    if not path:
        return
    entry = {
        "run_id": uuid.uuid4().hex[:12],
        "timestamp": report["generated_at"],
        "tenant_id": report["tenant_id"],
        "stories_seen": report["summary"]["stories_checked"],
        "delta": report["delta"],
        "fail": report["summary"]["fail"],
        "warning": report["summary"]["warning"],
        "high_risk_findings": report["summary"]["high_risk_findings"],
        "checker_version": report["checker_version"],
        "policy_version": report["policy_version"],
        "seconds": round(perf["seconds"], 3) if perf else None,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Story QA Gate")
    ap.add_argument("input", help="Story JSON snapshot")
    ap.add_argument("--policy", default="tenant_policy.json")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--ai-cache", default="ai_semantic_cache.json")
    ap.add_argument("--run-log", default="run_log.jsonl")
    ap.add_argument("--out", default="output")
    ap.add_argument("--expect-delta", default=None,
                    help="assert delta, e.g. new=2,changed=0,unchanged=0")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    tracemalloc.start()
    started = time.perf_counter()

    snapshot = load_json(args.input)
    policy = load_json(args.policy)
    state = load_state(args.state)
    reviewer = RecordedSemanticReviewer(args.ai_cache)

    report = run(snapshot, policy, state, reviewer)

    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    perf = {
        "stories": report["summary"]["stories_checked"],
        "seconds": elapsed,
        "rate": report["summary"]["stories_checked"] / elapsed if elapsed else 0.0,
        # tracemalloc works on Windows, macOS and Linux. `resource` is Unix only.
        "peak_mb": peak / (1024 * 1024),
    }
    report["performance"] = {"seconds": round(elapsed, 4),
                             "stories_per_second": round(perf["rate"], 1),
                             "peak_traced_memory_mb": round(perf["peak_mb"], 2)}

    write_json(os.path.join(args.out, "quality_report.json"), report)
    md = render_markdown(report, perf)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "quality_report.md"), "w", encoding="utf-8") as fh:
        fh.write(md + "\n")

    if args.state:
        write_json(args.state, state)
    append_run_log(args.run_log, report, perf)

    if not args.quiet:
        s, d = report["summary"], report["delta"]
        review_label = "needs" if s["needs_human_review"] == 1 else "need"
        print("%s | %d stories, %d pages | %d fail, %d warning | %d %s review"
              % (report["tenant_id"], s["stories_checked"], s["pages_checked"],
                  s["fail"], s["warning"], s["needs_human_review"], review_label))
        print("delta: %d new, %d changed, %d unchanged, %d carried forward"
              % (d["new"], d["changed"], d["unchanged"], d["carried_forward"]))
        print("%.0f stories/sec, peak %.1f MB -> %s"
              % (perf["rate"], perf["peak_mb"], args.out))

    if args.expect_delta:
        want = dict(kv.split("=") for kv in args.expect_delta.split(","))
        for field, value in want.items():
            actual = report["delta"].get(field.strip())
            if actual != int(value):
                print("DELTA ASSERTION FAILED: %s expected %s, got %s"
                      % (field, value, actual), file=sys.stderr)
                return 1

    # v1 warns, it never blocks publishing. Exit 0 even on findings.
    return 0


if __name__ == "__main__":
    sys.exit(main())
