#!/usr/bin/env python3
"""Generate a large synthetic Story snapshot to measure throughput.

Manual review is the thing this project exists to replace, so "it scales"
should be a measured number rather than a claim. Roughly 5% of generated
Stories carry a deliberate defect so the run is not trivially clean.

    python scripts/generate_synthetic_data.py 5000 synthetic_stories.json
"""

import json
import random
import sys

CLUBS = ["Penguin FC", "Seals United", "Orca City", "Krill Rovers", "Petrel Town"]
GOOD = [
    ("Watch highlights", "https://antarcticfootballleague.com/highlights"),
    ("View lineup", "https://antarcticfootballleague.com/lineup"),
    ("Buy tickets", "https://antarcticfootballleague.com/tickets"),
    ("Live match centre", "https://antarcticfootballleague.com/live"),
]
BAD = [
    ("Buy tickets", "https://antarcticfootballleague.com/highlights"),   # rule mismatch
    ("Read more", "https://antarcticfootballleague.com/lineup"),        # generic CTA
    ("View lineup", "https://evil-antarcticfootballleague.com/lineup"), # lookalike host
    ("Buy tickets", "http://antarcticfootballleague.com/tickets"),      # not https
]


def build(count, seed=7):
    rng = random.Random(seed)
    stories = []
    for i in range(count):
        defective = rng.random() < 0.05
        cta, url = rng.choice(BAD if defective else GOOD)
        home, away = rng.sample(CLUBS, 2)
        title = "TODO preview" if defective and rng.random() < 0.3 else \
                "%s vs %s: matchday" % (home, away)
        stories.append({
            "story_id": "story_%06d" % i,
            "story_title": title,
            "pages": [
                {
                    "page_id": "page_1",
                    "type": "image",
                    "asset_url": "https://cdn.storyteller.com/assets/s%06d/p1.jpg" % i,
                    "action": {"cta": cta, "url": url},
                },
                {
                    "page_id": "page_2",
                    "type": "video",
                    "asset_url": "https://cdn.storyteller.com/assets/s%06d/p2.mp4" % i,
                    "action": {"cta": "Watch highlights",
                               "url": "https://antarcticfootballleague.com/highlights"},
                },
            ],
            "context": {"categories": [home, away], "publish_date": "2026-02-14"},
        })
    return {
        "tenant_id": "tenant_antarctic_league_001",
        "tenant_name": "Antarctic Football League",
        "stories": stories,
    }


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    path = sys.argv[2] if len(sys.argv) > 2 else "synthetic_stories.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(build(count), fh)
    print("wrote %d stories to %s" % (count, path))
