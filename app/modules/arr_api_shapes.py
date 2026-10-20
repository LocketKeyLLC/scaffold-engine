r"""§17.1694 — the *arr API shapes a drafted read leans on, shown to the drafter and checked in the draft.

Live (job e5e22b92 T4 "Compare Prowlarr indexer categories", 2026-10-20): the in-guest program read
`idx.get("categories")` off each object of Prowlarr's `/api/v1/indexer` -- a field Prowlarr does not have. An indexer's
categories are under `capabilities.categories` (job c2518102 M2 read them there, live, the day before: 22 of 50 with TV),
so the step would have printed "TV-capable enabled indexers: 0" and every later step (the sync diagnosis, the category
change) would have started from a false zero. The engine had the shape in its own record and nothing carried it.

Each entry is what the engine has read off these services (job c2518102 M1-M3, e5e22b92 T2) or what their API
reference defines, scoped to the fields a read needs and the ones that hold credentials. An entry is shown when the
step names its service and its resource; `wrong` is a draft pattern the shape rules out, refused with the shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Shape:
    service: str            # the word in the step text: prowlarr | sonarr
    resource: re.Pattern    # the resource words in the step text
    endpoint: str           # the path a draft that reads this resource calls
    text: str               # what the drafter is shown
    wrong: Optional[re.Pattern] = None   # a draft pattern the shape rules out
    why: str = ""
    alone: Optional[re.Pattern] = None   # step wording that names this resource without naming the service


SHAPES: tuple[Shape, ...] = (
    Shape("prowlarr", re.compile(r"\bindexers?\b|\bcategor", re.I), "/api/v1/indexer",
          "Prowlarr `GET /api/v1/indexer` -> a list; per indexer: `id`, `name`, `enable`, `protocol`, `privacy`, "
          "`appProfileId`, `tags`, and `capabilities.categories` = `[{id, name, subCategories: [{id, name}]}]` (TV is "
          "5000 with subcategories 5010-5090; Anime is 5070). There is NO top-level `categories` field. `fields` hold the "
          "indexer's settings AND credentials: never print them. `GET /api/v1/indexerstatus` -> the failing ones "
          "(`indexerId`, `disabledTill`, `mostRecentFailure`).",
          re.compile(r"""\.get\(\s*['"]categories['"]|\[\s*['"]categories['"]\s*\]"""),
          "an indexer object from Prowlarr's `/api/v1/indexer` has no top-level `categories`: they are in "
          "`capabilities.categories` (`[{id, name, subCategories: [{id, name}]}]`), so this read finds none on any "
          "indexer and reports a false zero"),
    Shape("prowlarr", re.compile(r"\bsync|\bapplications?\b|\bsonarr\b", re.I), "/api/v1/applications",
          "Prowlarr `GET /api/v1/applications` -> per application (Sonarr, Radarr): `id`, `name`, `syncLevel` "
          "(`disabled` | `addOnly` | `fullSync`), `tags`, and `fields` as `[{name, value}]` -- the sync categories are "
          "the fields named `syncCategories` and `animeSyncCategories` (lists of category ids); the same `fields` hold "
          "`apiKey`, `baseUrl` and `prowlarrUrl`: print only the category fields. An application with tags syncs only "
          "indexers that share one of its tags. `GET /api/v1/appprofile` -> `id`, `name`, `enableRss`, "
          "`enableAutomaticSearch`, `enableInteractiveSearch`, `minimumSeeders` (each indexer's `appProfileId` names one). "
          "`POST /api/v1/applications/action/syncall` asks Prowlarr to sync every application now (a change)."),
    Shape("sonarr", re.compile(r"\bquality\s+profiles?\b|\bprofiles?\b", re.I), "/api/v3/qualityprofile",
          "Sonarr `GET /api/v3/qualityprofile` -> per profile: `id`, `name`, `upgradeAllowed`, `cutoff` (a quality id), "
          "`items` = `[{quality: {id, name, source, resolution}, allowed}]` or groups `{name, allowed, items: [...]}` -- "
          "long: print the profile's name, cutoff and the ALLOWED quality names only. Which series use a profile is "
          "`qualityProfileId` on each series of `GET /api/v3/series`."),
    Shape("sonarr", re.compile(r"\bseries\b|\bseasons?\b|\btitles?\b|\bscene\b", re.I), "/api/v3/series",
          "Sonarr `GET /api/v3/series` -> per series: `id`, `title`, `seriesType` (`standard` | `daily` | `anime`), "
          "`qualityProfileId`, `monitored`, `tvdbId`, `alternateTitles` = `[{title, seasonNumber, sceneSeasonNumber}]` "
          "(these come from Sonarr's scene-mapping service, not from the series record), and `seasons` = "
          "`[{seasonNumber, monitored, statistics: {episodeFileCount, episodeCount, totalEpisodeCount}}]`. "
          "`GET /api/v3/series/{id}` is one series."),
    Shape("sonarr", re.compile(r"\binteractive\s+search\b|\bsearch\b|\breleases?\b", re.I), "/api/v3/release",
          "Sonarr `GET /api/v3/release?seriesId=N&seasonNumber=S` (or `?episodeId=E`) runs an interactive search across "
          "Sonarr's indexers and returns the releases WITHOUT grabbing any (a grab is `POST /api/v3/release`); per "
          "release: `title`, `indexer`, `size`, `seeders`, `quality.quality.name`, `rejected`, `rejections` (why Sonarr "
          "would not take it). It can take a minute or more: give urllib a long timeout. Print counts and one short line "
          "per release, never the raw list.",
          alone=re.compile(r"\binteractive\s+search\b[^.]*?(?:\bS\d{1,2}\b|\bseasons?\b|\bepisodes?\b)|"
                           r"\bsearch\s+\S.{0,60}?\bS\d{1,2}(?:E\d{1,3})?\b", re.I)),
    Shape("sonarr", re.compile(r"\bindexers?\b", re.I), "/api/v3/indexer",
          "Sonarr `GET /api/v3/indexer` -> the indexers Sonarr itself holds (Prowlarr's synced ones end in "
          "\"(Prowlarr)\"): `id`, `name`, `enableRss`, `enableAutomaticSearch`, `enableInteractiveSearch`, `priority`, "
          "`tags`; `categories` and `animeCategories` are in `fields` by name, and `fields` also hold `apiKey` and "
          "`baseUrl`: print only the category fields."),
    Shape("sonarr", re.compile(r"\bqueue\b|\bmissing\b|\bwanted\b", re.I), "/api/v3/queue",
          "Sonarr's lists are PAGED: `GET /api/v3/queue` and `GET /api/v3/wanted/missing` take `page` and `pageSize` and "
          "answer `{page, pageSize, totalRecords, records}` -- read every page until `totalRecords`. A queue record: "
          "`seriesId`, `episodeId`, `seasonNumber`, `title`, `status`, `trackedDownloadState`, `sizeleft`, `indexer`. "
          "A missing record is an episode: `seriesId`, `seasonNumber`, `episodeNumber`, `airDateUtc`, `monitored` "
          "(`includeSeries=true` adds the series)."),
)


def _text(node: Optional[dict]) -> str:
    return " ".join(str((node or {}).get(k) or "") for k in ("title", "description", "prompt_template"))


def for_step(node: Optional[dict]) -> list[Shape]:
    """The shapes a step needs: its text names the service and the resource."""
    text = _text(node)
    # §17.1694b — "run interactive search for Robot Chicken Season 11" names Sonarr's release search without "Sonarr"
    return [s for s in SHAPES if (re.search(rf"\b{s.service}\b", text, re.I) and s.resource.search(text))
            or (s.alone is not None and s.alone.search(text))]


def drafter_block(node: Optional[dict]) -> str:
    shapes = for_step(node)
    if not shapes:
        return ""
    lines = ["## The API shapes this step reads (the engine's record of these services)",
             "Use these field names; do not guess others.", ""] + [f"- {s.text}" for s in shapes]
    # §17.1694c — a read is drafted in the shape the engine accepts as its own evidence, from the first draft: live,
    # job e5e22b92 T5's draft asked the operator for <SONARR_SERIES_ID> (Sonarr holds it) and checked with `"id"`
    from app.modules.supervised_runs import only_reads
    if only_reads(node):                                             # §17.1695
        lines += ["", "This step only READS. Write its in-guest block as ONE `python3 - <<'PY'` program and nothing else: "
                  "read the API key from the service's config.xml, look up every id the step needs by name inside the "
                  "program (e.g. the series by `title` from `GET /api/v3/series`) -- never ask for an id the service "
                  "holds -- make only GET calls with urllib, and print a SUMMARY (totals, then one short line per item). "
                  "It needs no separate check: its own output is judged against the step's goal."]
    return "\n".join(lines)


def wrong_fields(commands: list[str], files: Optional[list[dict]]) -> list[dict]:
    """``[{command, why}]`` for a draft that reads a field the shape of the endpoint it calls rules out."""
    texts = [str(c) for c in commands or []] + [str((f or {}).get("content") or "") for f in files or []]
    out: list[dict] = []
    for s in SHAPES:
        if s.wrong is None:
            continue
        for t in texts:
            if s.endpoint not in t or "capabilities" in t:
                continue
            m = s.wrong.search(t)
            if m:
                line = next((ln for ln in t.splitlines() if s.wrong.search(ln)), m.group(0))
                out.append({"command": line.strip()[:200],
                            "why": f"this reads a field this endpoint's objects do not have: {s.why} (§17.1694). {s.text}"})
                break
    return out
