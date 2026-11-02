#!/usr/bin/env python3
"""Add new publications to research.html.

Finds works linked to the ORCID in .github/publications.json (via Crossref
and OpenAlex), takes every card detail from Crossref, skips works already on
the page, swaps preprint cards for their published versions, and writes a
summary for the pull request body.

Standard library only. Usage:
    python .github/scripts/update_publications.py [--page research.html]
        [--summary summary.md] [--fixtures dir] [--dry-run]
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".github" / "publications.json"
UA = "gps-lab-website-publication-updater (https://github.com/ceciliapad/web)"
NL = "\r\n"  # research.html uses CRLF line endings

PREPRINT_SERVERS = {
    "10.1101": "bioRxiv",
    "10.64898": "bioRxiv",
    "10.31235": "SocArXiv",
    "10.31219": "OSF Preprints",
    "10.32942": "EcoEvoRxiv",
    "10.31223": "EarthArXiv",
    "10.21203": "Research Square",
    "10.20944": "Preprints.org",
    "10.48550": "arXiv",
    "10.2139": "SSRN",
}
PREPRINT_LINK_HINTS = ("biorxiv", "medrxiv", "osf.io", "arxiv", "socarxiv",
                       "researchsquare", "ssrn", "preprints.org")
SKIP_TITLE = re.compile(r"^\s*(correction|corrigendum|erratum|retraction|reply|"
                        r"response to|author correction|publisher correction)\b", re.I)
KEPT_TYPES = {"journal-article", "posted-content", "proceedings-article", "book-chapter"}

FIXTURES = None  # directory of saved API responses, for offline tests


# ---------------------------------------------------------------- fetching

def get_json(url):
    if FIXTURES:
        name = re.sub(r"[^A-Za-z0-9]+", "_", url)[:200] + ".json"
        path = Path(FIXTURES) / name
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {url} ({name})")
        return json.loads(path.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def norm_doi(doi):
    doi = (doi or "").strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = urllib.parse.unquote(doi).rstrip(".")
    return re.sub(r"_v\d+$", "", doi)  # OSF versions: _v1, _v2 ...


def crossref_by_orcid(orcid, since):
    url = ("https://api.crossref.org/works?rows=200&filter="
           + urllib.parse.quote(f"orcid:{orcid},from-pub-date:{since}"))
    return [it for it in get_json(url)["message"]["items"]]


def openalex_dois(orcid, since):
    params = {"filter": f"author.orcid:https://orcid.org/{orcid},from_publication_date:{since}",
              "per-page": "200", "select": "doi,title"}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    data = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    return [norm_doi(w["doi"]) for w in data.get("results", []) if w.get("doi")]


def crossref_work(doi):
    return get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))["message"]


# ---------------------------------------------------------------- crossref helpers

def cr_title(item):
    t = (item.get("title") or [""])[0]
    return re.sub(r"\s+", " ", t).strip()


def cr_date(item):
    for key in ("published", "issued", "published-online", "published-print", "posted", "created"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            p = parts[0] + [1, 1]
            return date(p[0], p[1] or 1, p[2] or 1)
    return None


def is_preprint(item):
    return item.get("type") == "posted-content" or norm_doi(item["DOI"]).split("/")[0] in PREPRINT_SERVERS


def preprint_server(item):
    for inst in item.get("institution") or []:
        if inst.get("name"):
            return inst["name"]
    return PREPRINT_SERVERS.get(norm_doi(item["DOI"]).split("/")[0], item.get("publisher", "Preprint"))


def related_dois(item, kind):
    return {norm_doi(r.get("id")) for r in (item.get("relation") or {}).get(kind, [])
            if r.get("id-type") == "doi"}


# ---------------------------------------------------------------- card text

def esc(s):
    return html.escape(html.unescape(s or ""), quote=False)


def clean_title(t):
    """Escape a Crossref title, keeping simple italics/sub/superscripts."""
    t = html.escape(html.unescape(re.sub(r"\s+", " ", t).strip()), quote=False)
    t = re.sub(r"&lt;(/?)(i|em)&gt;", r"<\1em>", t)
    t = re.sub(r"&lt;(/?)(sub|sup)&gt;", r"<\1\2>", t)
    t = re.sub(r"&lt;/?[a-zA-Z:]+[^&]*?&gt;", "", t)  # any other markup (mml etc.)
    return t


def initials(given):
    out = []
    for part in re.split(r"\s+", given.strip()):
        bits = [b for b in part.split("-") if b]
        if bits:
            out.append("-".join(b[0].upper() + "." for b in bits))
    return "".join(out)


def author_list(item, orcid, highlight):
    names = []
    for a in item.get("author") or []:
        if a.get("family"):
            name = f"{a['family']}, {initials(a.get('given', ''))}".rstrip(", ")
        else:
            name = a.get("name", "")
        name = esc(name)
        mine = (a.get("ORCID") or "").endswith(orcid) or a.get("family") in highlight
        names.append(f'<span class="author-highlight">{name}</span>' if mine else name)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + ", &amp; " + names[-1]


def venue_line(item):
    if is_preprint(item):
        return f'<span class="journal-name">{esc(preprint_server(item))}</span>.'
    venue = esc((item.get("container-title") or [""])[0])
    bits = []
    vol, issue = item.get("volume"), item.get("issue")
    if vol:
        bits.append(esc(vol) + (f"({esc(issue)})" if issue else ""))
    num = item.get("article-number") or item.get("page")
    if num:
        bits.append(esc(num).replace("-", "–"))
    tail = (", " + ", ".join(bits)) if bits else ""
    return f'<span class="journal-name">{venue}</span>{tail}.'


def card_html(item, orcid, highlight):
    year = cr_date(item).year
    i = " " * 20
    lines = [
        f'{i}<div class="publication-card card" data-year="{year}">',
        f'{i}    <div class="card-body position-relative">',
        f'{i}        <span class="year-badge">{year}</span>',
        f'{i}        <div class="content_title">',
        f'{i}            <h4 class="card-title">{clean_title(cr_title(item))}</h4>',
        f'{i}        </div>',
        f'{i}        <p class="card-text">',
        f'{i}            {author_list(item, orcid, highlight)}',
        f'{i}            <br>',
        f'{i}            {venue_line(item)}',
        f'{i}        </p>',
        f'{i}        <a target="_blank" href="https://doi.org/{html.escape(item["DOI"])}" class="btn btn-publication">View Article</a>',
        f'{i}    </div>',
        f'{i}</div>',
    ]
    return NL.join(lines) + NL + NL


# ---------------------------------------------------------------- page parsing

def norm_title(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def similar(a, b):
    a, b = norm_title(a), norm_title(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def first_family(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text.split(",")[0].strip().lower()


CARD_START = re.compile(r'[ \t]*<div class="publication-card card"[^>]*>')


def find_cards(page):
    cards = []
    for m in CARD_START.finditer(page):
        depth, pos = 0, m.start()
        for tag in re.finditer(r"<(/?)div\b[^>]*>", page[m.start():]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = m.start() + tag.end()
                break
        else:
            raise ValueError("unbalanced publication card")
        block = page[pos:end]
        title = re.search(r'<h4 class="card-title">(.*?)</h4>', block, re.S)
        text = re.search(r'<p class="card-text">(.*?)<br', block, re.S)
        href = re.search(r'href="([^"]+)" class="btn btn-publication"', block)
        link = href.group(1) if href else ""
        dois = {norm_doi(d) for d in re.findall(r"10\.\d{4,9}/[^\s\"'&?#<>]+", block)}
        dois |= {norm_doi(re.sub(r"v\d+(\.full)?$", "", d)) for d in dois}
        cards.append({
            "start": pos, "end": end,
            "title": title.group(1).strip() if title else "",
            "first": first_family(text.group(1)) if text else "",
            "link": link, "dois": dois,
            "preprint": any(h in link.lower() for h in PREPRINT_LINK_HINTS)
                        or any(d.split("/")[0] in PREPRINT_SERVERS for d in dois),
        })
    return cards


def match_card(item, cards):
    """Return (card, how) for the existing card that is this work, or (None, None)."""
    doi = norm_doi(item["DOI"])
    linked = {doi} | related_dois(item, "has-preprint") | related_dois(item, "is-preprint-of")
    title = cr_title(item)
    first = (item.get("author") or [{}])[0].get("family", "").lower()
    for c in cards:
        if doi in c["dois"]:
            return c, "same DOI"
    for c in cards:
        if linked & c["dois"]:
            return c, "linked preprint/published DOI"
    best = max(cards, key=lambda c: similar(title, c["title"]), default=None)
    if best:
        score = similar(title, best["title"])
        if score >= 0.9:
            return best, f"title match ({score:.2f})"
        if score >= 0.7 and first and best["first"].startswith(first):
            return best, f"close title + same first author ({score:.2f})"
    return None, None


# ---------------------------------------------------------------- page editing

def insert_card(page, year, card):
    marker = re.search(rf"<!-- {year} Publications -->[^\n]*\n(?:[ \t]*\r?\n)?", page)
    if marker:
        return page[:marker.end()] + card + page[marker.end():]
    # New year: add a section and a filter option.
    years = [int(y) for y in re.findall(r"<!-- (\d{4}) Publications -->", page)]
    older = sorted([y for y in years if y < year], reverse=True)
    section = f"                    <!-- {year} Publications -->{NL}{NL}" + card
    if older:
        m = re.search(rf"[ \t]*<!-- {older[0]} Publications -->", page)
        page = page[:m.start()] + section + page[m.start():]
    else:
        m = re.search(r'<div id="publicationsContainer"[^>]*>\r?\n(?:[ \t]*\r?\n)?', page)
        page = page[:m.end()] + section + page[m.end():]
    opts = [int(y) for y in re.findall(r'class="dropdown-item[^"]*" href="#" data-year="(\d{4})"', page)]
    if year not in opts:
        after = [y for y in opts if y > year]
        anchor = (rf'[^\n]*data-year="{min(after)}"[^\n]*\n' if after
                  else r'[^\n]*data-year="all"[^\n]*\n')
        m = re.search(anchor, page)
        indent = re.match(r"[ \t]*", m.group(0)).group(0)
        li = f'{indent}<li><a class="dropdown-item" href="#" data-year="{year}">{year}</a></li>{NL}'
        page = page[:m.end()] + li + page[m.end():]
    return page


def remove_card(page, card):
    end = card["end"]
    # swallow the line ending and one blank line after the card
    m = re.match(r"\r?\n(?:[ \t]*\r?\n)?", page[end:])
    if m:
        end += m.end()
    start = page.rfind("\n", 0, card["start"]) + 1
    return page[:start] + page[end:]


# ---------------------------------------------------------------- main

def main():
    global FIXTURES
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=str(ROOT / "research.html"))
    ap.add_argument("--summary", default=str(ROOT / "publication-summary.md"))
    ap.add_argument("--fixtures")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    FIXTURES = args.fixtures

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    orcid, since = cfg["orcid"], cfg["since"]
    highlight = set(cfg.get("highlight_family_names", []))
    ignore = {norm_doi(d) for d in cfg.get("ignore_dois", [])}
    notes = []

    # 1. Collect candidate works (full records always from Crossref).
    works = {}
    try:
        for it in crossref_by_orcid(orcid, since):
            works[norm_doi(it["DOI"])] = it
    except Exception as e:  # keep going with the other source
        notes.append(f"Crossref ORCID search failed: {e}")
    try:
        for doi in openalex_dois(orcid, since):
            if doi not in works:
                try:
                    works[doi] = crossref_work(doi)
                except Exception as e:
                    notes.append(f"Could not get Crossref details for {doi}: {e}")
    except Exception as e:
        notes.append(f"OpenAlex search failed: {e}")
    if not works and notes:
        print("\n".join(notes), file=sys.stderr)
        sys.exit(1)

    # 2. Filter.
    cands = []
    for doi, it in works.items():
        title = cr_title(it)
        d = cr_date(it)
        if (doi in ignore or it.get("type") not in KEPT_TYPES or not title
                or SKIP_TITLE.search(title) or d is None or d < date.fromisoformat(since)):
            continue
        cands.append(it)
    # Published versions first, so their preprints are dropped as duplicates.
    cands.sort(key=lambda it: (is_preprint(it), cr_date(it)), reverse=False)
    picked = []
    for it in cands:
        rel = related_dois(it, "is-preprint-of") | related_dois(it, "has-preprint")
        if any(norm_doi(p["DOI"]) in rel or similar(cr_title(p), cr_title(it)) >= 0.9 for p in picked):
            continue
        picked.append(it)

    # 3. Compare with the page and edit it.
    raw = Path(args.page).read_bytes().decode("utf-8")
    page = raw
    added, replaced, already = [], [], []
    for it in sorted(picked, key=cr_date):
        cards = find_cards(page)
        card, how = match_card(it, cards)
        label = f"{cr_title(it)} ({venue_line(it)})"
        label = html.unescape(re.sub(r"<[^>]+>", "", label)).rstrip(".)") + ")"
        if card and card["preprint"] and not is_preprint(it) and how != "same DOI":
            page = remove_card(page, card)
            page = insert_card(page, cr_date(it).year, card_html(it, orcid, highlight))
            replaced.append(f"- **{label}** – https://doi.org/{it['DOI']}\n"
                            f"  replaces preprint card \"{re.sub(r'<[^>]+>', '', card['title'])}\" ({how})")
        elif card:
            already.append(f"- {label} – already on the page ({how})")
        else:
            page = insert_card(page, cr_date(it).year, card_html(it, orcid, highlight))
            added.append(f"- **{label}** – https://doi.org/{it['DOI']}")

    # 4. Report.
    lines = ["Weekly check of works linked to ORCID "
             f"[{orcid}](https://orcid.org/{orcid}) since {since} (Crossref + OpenAlex). "
             "All card details come from Crossref.", ""]
    if added:
        lines += ["### New cards", *added, ""]
    if replaced:
        lines += ["### Preprints swapped for the published version", *replaced, ""]
    if already:
        lines += ["<details><summary>Already on the page</summary>", "", *already, "", "</details>", ""]
    if notes:
        lines += ["### Warnings", *[f"- {n}" for n in notes], ""]
    lines += ["To stop a paper from being suggested again, add its DOI to `ignore_dois` in "
              "`.github/publications.json` and close this pull request."]
    summary = "\n".join(lines) + "\n"
    print(summary)
    Path(args.summary).write_text(summary, encoding="utf-8")

    changed = page != raw
    if changed and not args.dry_run:
        Path(args.page).write_bytes(page.encode("utf-8"))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
