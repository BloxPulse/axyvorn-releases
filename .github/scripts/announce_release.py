#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Annonce automatique des releases GitHub d'Axyvorn sur Discord (stdlib only)."""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "BloxPulse/axyvorn-releases")
API = "https://api.github.com"
UA = "Axyvorn-Release-Announcer/1.0"
NL = chr(10)
COLOR = 0x8B5CF6
TITLE = "🟣 NOUVELLE MISE À JOUR D'AXYVORN !"
FOOTER = "💜 Merci de soutenir Axyvorn !"
EMPTY_FEATURES = "_Aucune nouveauté détaillée dans la release GitHub._"
EMPTY_FIXES = "_Aucune correction explicitement listée dans la release GitHub._"
FIELD_LIMIT, TOTAL_LIMIT, MAX_FIELDS = 1000, 5800, 25


def log(msg):
    print(msg, flush=True)


def mask(text):
    return re.sub(r"(webhooks/\d+/)[A-Za-z0-9_\-]+", r"\1***", str(text))


class AnnounceError(Exception):
    pass


def http_request(url, method="GET", payload=None, headers=None, timeout=30):
    hdrs = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if headers:
        hdrs.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def gh_get(path, token=None, attempts=4):
    url = path if path.startswith("http") else API + path
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    last = ""
    for i in range(1, attempts + 1):
        try:
            status, _, body = http_request(url, headers=headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "réseau : %s" % e
            time.sleep(min(2 ** i, 20))
            continue
        if status == 200:
            return json.loads(body.decode("utf-8"))
        if status == 404:
            raise AnnounceError("GitHub 404 sur %s (release inexistante ou dépôt privé)." % path)
        last = "HTTP %s : %s" % (status, body[:200].decode("utf-8", "replace"))
        if status < 500 and status != 403:
            break
        time.sleep(min(2 ** i, 20))
    raise AnnounceError("Lecture API GitHub impossible (%s) : %s" % (path, last))


def release_from_event():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("release")


def release_from_api(tag=None, token=None):
    if tag:
        return gh_get("/repos/%s/releases/tags/%s" % (REPO, urllib.parse.quote(tag)), token)
    return gh_get("/repos/%s/releases/latest" % REPO, token)


def list_releases(token=None):
    return gh_get("/repos/%s/releases?per_page=100" % REPO, token)


def eligibility(rel, include_prereleases):
    if rel.get("draft"):
        return False, "brouillon (draft) : jamais annoncé"
    if not rel.get("published_at"):
        return False, "release non publiée"
    if rel.get("prerelease") and not include_prereleases:
        return False, "préversion (ANNOUNCE_PRERELEASES=false)"
    return True, ""


def version_label(rel):
    core = re.sub(r"^[vV]", "", (rel.get("tag_name") or "").strip())
    return "V" + core if core else (rel.get("name") or "version inconnue")


HEADINGS = {
    "features": ["nouveautes", "nouveaute", "nouvelles fonctionnalites", "fonctionnalites",
                 "ajouts", "added", "features", "new"],
    "fixes": ["corrections", "correctifs", "corriges", "bugs corriges", "fixes", "fixed",
              "bug fixes", "bugfixes", "resolutions"],
    "other": ["changements", "changes", "ameliorations", "improvements", "modifications",
              "autres", "divers", "misc", "changed"],
    "skip": ["telecharger", "telechargement", "download", "downloads", "installation",
             "installer", "install", "mise a niveau", "upgrade", "full changelog",
             "fichiers", "assets", "checksums", "sha256", "verification", "notes"],
}
FIX_RE = re.compile(
    r"(corrig|correctif|correction|\bfix(es|ed)?\b|r[ée]sol|r[ée]par|\bbug|"
    r"r[ée]gression|regression|\bcrash|plantage|\bplus de\b|"
    r"\bne\s+\S+(?:\s+\S+){0,3}\s+plus\b|[ée]chouai|s['’]arrêtait|"
    r"refuse d[ée]sormais|\bretrait\b|\bretir[ée]\b|au lieu de|n['’]est plus)", re.I)
FEATURE_RE = re.compile(
    r"^\**\s*(nouveau|nouvelle|nouveaux|nouvelles|ajout|ajoute|added|new\b|"
    r"support|prise en charge)", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def heading_key(text):
    text = unicodedata.normalize("NFKD", re.sub(r"[*_`:#]", " ", text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).strip()


def classify_heading(text):
    key = heading_key(text)
    for bucket, names in HEADINGS.items():
        for name in names:
            if name in key:
                return bucket
    return None


def clean_item(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"^#{1,6}\s*", "**", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_items(text):
    items, cur, in_code = [], None, False
    for raw in text.split(NL):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            cur = (cur + " " + line.strip()) if cur else line.strip()
            continue
        if not line.strip() or (HR_RE.match(line) and not in_code):
            if cur:
                items.append(cur)
                cur = None
            continue
        m = BULLET_RE.match(line) if not in_code else None
        if m:
            if cur:
                items.append(cur)
            cur = m.group(1).strip()
        elif cur is not None:
            cur += " " + line.strip()
        else:
            cur = line.strip()
    if cur:
        items.append(cur)
    return [c for c in (clean_item(i) for i in items) if c]


def parse_changelog(body, release_name="", tag=""):
    body = (body or "").replace(chr(13) + NL, NL).replace(chr(13), NL)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    result = {"features": [], "fixes": [], "other": [], "download_notes": [], "sha256": None}
    blocks, heading, buf = [], None, []
    for line in body.split(NL):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append((heading, NL.join(buf)))
            heading, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    blocks.append((heading, NL.join(buf)))
    explicit = False
    for heading, text in blocks:
        bucket = classify_heading(heading) if heading else None
        if bucket in ("features", "fixes"):
            explicit = True
        if bucket == "skip":
            result["download_notes"] += extract_items(text)
            continue
        for item in extract_items(text):
            if bucket in ("features", "fixes", "other"):
                result[bucket].append(item)
            elif FEATURE_RE.match(item):
                result["features"].append(item)
            elif FIX_RE.search(item):
                result["fixes"].append(item)
            else:
                result["features"].append(item)
    noise = {release_name.strip().lower(), tag.strip().lower(),
             (release_name + " " + tag).strip().lower()}
    for key in ("features", "fixes", "other"):
        result[key] = [i for i in result[key] if i.strip().lower() not in noise]
    m = re.search(r"\b([a-f0-9]{64})\b", body, re.I)
    if m:
        result["sha256"] = m.group(1).lower()
    result["explicit_sections"] = explicit
    return result


def chunk_items(items, limit=FIELD_LIMIT):
    chunks, cur = [], ""
    for item in items:
        line = "• " + item
        while len(line) > limit:
            head, line = line[:limit], line[limit:]
            cut = head.rfind(" ")
            if cut > limit * 0.6:
                head, line = head[:cut], head[cut + 1:] + line
            chunks.append(head.rstrip())
            line = "• " + line
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += NL + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def iso_to_unix(value):
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def download_field(rel, version, parsed):
    url = rel.get("html_url", "")
    lines = ["[⬇️ **Télécharger %s sur GitHub**](%s)" % (version, url)]
    assets = [a.get("name", "") for a in rel.get("assets", []) if a.get("name")]
    parts = [a for a in assets if ".part-" in a]
    if parts:
        extra = " (voir instructions de la release)" if parsed["download_notes"] else ""
        lines.append("ISO en **%d parties** à télécharger puis à recoller%s." % (len(parts), extra))
    if assets:
        shown = ", ".join("`%s`" % a for a in assets[:4])
        more = " (+%d)" % (len(assets) - 4) if len(assets) > 4 else ""
        lines.append("Fichiers : " + shown + more)
    return NL.join(lines)[:FIELD_LIMIT]


def enforce_total_limit(embed, url, reserved=0):
    suffix = NL + "… [voir la release complète](%s)" % url
    fields = embed["fields"]
    tail = fields[len(fields) - reserved:] if reserved else []
    head = fields[:len(fields) - reserved] if reserved else fields
    budget = TOTAL_LIMIT - len(embed.get("title", "")) - len(embed.get("description", ""))
    budget -= len(embed["footer"]["text"]) + len(embed["author"]["name"])
    for f in tail:
        budget -= len(f["name"]) + len(f["value"])
    kept = []
    for f in head:
        if len(kept) >= MAX_FIELDS - len(tail):
            break
        budget -= len(f["name"])
        if budget <= len(suffix) + 20:
            break
        if len(f["value"]) > budget:
            f["value"] = f["value"][:max(0, budget - len(suffix))].rstrip() + suffix
            budget = 0
        else:
            budget -= len(f["value"])
        kept.append(f)
    embed["fields"] = kept + tail


def embed_size(embed):
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len((embed.get("footer") or {}).get("text", ""))
    total += len((embed.get("author") or {}).get("name", ""))
    for f in embed.get("fields", []):
        total += len(f["name"]) + len(f["value"])
    return total


def build_payload(rel, parsed, test_mode=False):
    version = version_label(rel)
    url = rel.get("html_url", "")
    avatar = (rel.get("author") or {}).get("avatar_url")
    desc = ["🚀 **Version :** `%s`" % version]
    name = (rel.get("name") or "").strip()
    if name and name.lower() != version.lower():
        desc.append("📦 **Release :** %s" % name)
    ts = iso_to_unix(rel.get("published_at") or "")
    if ts:
        desc.append("📅 **Publiée le :** <t:%d:F>" % ts)
    if rel.get("prerelease"):
        desc.append("⚠️ **Préversion** — version de test, à utiliser avec prudence.")
    fields = []

    def add_series(label, items, empty_text):
        chunks = chunk_items(items) if items else [empty_text]
        for i, chunk in enumerate(chunks):
            fields.append({"name": label if i == 0 else label + " (suite)",
                           "value": chunk, "inline": False})

    add_series("✨ **Nouveautés :**", parsed["features"], EMPTY_FEATURES)
    add_series("🛠️ **Corrections :**", parsed["fixes"], EMPTY_FIXES)
    if parsed["other"]:
        add_series("🔧 **Autres changements :**", parsed["other"], "")
    fields.append({"name": "📥 **Téléchargement :**",
                   "value": download_field(rel, version, parsed), "inline": False})
    if parsed["sha256"]:
        fields.append({"name": "🔐 **SHA-256 de l'ISO assemblée :**",
                       "value": "```" + NL + parsed["sha256"] + NL + "```", "inline": False})
    embed = {
        "title": ("[TEST] " if test_mode else "") + TITLE,
        "url": url,
        "color": COLOR,
        "description": NL.join(desc)[:4000],
        "author": {"name": "Axyvorn • Système d'exploitation", "url": url},
        "fields": fields[:MAX_FIELDS],
        "footer": {"text": FOOTER + (" • message de test" if test_mode else "")},
    }
    if avatar:
        embed["author"]["icon_url"] = avatar
        embed["footer"]["icon_url"] = avatar
    if ts:
        embed["timestamp"] = rel["published_at"]
    enforce_total_limit(embed, url, reserved=1 + (1 if parsed["sha256"] else 0))
    payload = {
        "username": "Axyvorn • Mises à jour",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Télécharger %s" % version,
             "emoji": {"name": "📥"}, "url": url},
            {"type": 2, "style": 5, "label": "Changelog complet",
             "emoji": {"name": "📄"}, "url": url}]}],
    }
    if avatar:
        payload["avatar_url"] = avatar
    return payload


def send_webhook(url, payload, attempts=5):
    endpoint = url.split("?")[0] + "?wait=true&with_components=true"
    last = ""
    for i in range(1, attempts + 1):
        try:
            status, headers, body = http_request(endpoint, "POST", payload, timeout=30)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "erreur réseau : %s" % e
            log("  tentative %d/%d - %s" % (i, attempts, last))
            time.sleep(min(2 ** i, 30))
            continue
        text = body.decode("utf-8", "replace") if body else ""
        if status == 200:
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                msg = {}
            if msg.get("id"):
                return msg
            raise AnnounceError("HTTP 200 sans identifiant de message : réception NON confirmée.")
        if status == 204:
            raise AnnounceError("HTTP 204 : réception non confirmée -> vérifie le salon avant relance.")
        if status == 429:
            delay = 5.0
            try:
                delay = float(json.loads(text).get("retry_after", 5))
            except Exception:
                try:
                    delay = float(headers.get("Retry-After", 5))
                except Exception:
                    pass
            last = "rate limit 429"
            log("  tentative %d/%d - 429, pause %.1fs" % (i, attempts, delay))
            time.sleep(delay + 0.5)
            continue
        if 500 <= status < 600:
            last = "Discord HTTP %d" % status
            log("  tentative %d/%d - %s" % (i, attempts, last))
            time.sleep(min(2 ** i, 30))
            continue
        raise AnnounceError("Discord a refusé le message (HTTP %d) : %s" % (status, mask(text)[:400]))
    raise AnnounceError("Échec de l'envoi après %d tentatives (%s)." % (attempts, last))


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"schema": 1, "announced": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write(NL)


def already_announced(state, rel):
    entry = state["announced"].get(str(rel["id"]))
    return bool(entry and entry.get("mode") in ("prod", "seed"))


def record(state, rel, mode, message=None):
    state["announced"][str(rel["id"])] = {
        "tag": rel.get("tag_name"), "version": version_label(rel),
        "published_at": rel.get("published_at"),
        "body_sha256": hashlib.sha256((rel.get("body") or "").encode("utf-8")).hexdigest(),
        "mode": mode, "message_id": (message or {}).get("id"),
        "channel_id": (message or {}).get("channel_id"),
        "announced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def gh_output(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write("%s=%s" % (k, str(v).replace(NL, " ")) + NL)


def gh_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + NL)


def preview_text(payload):
    e = payload["embeds"][0]
    out = ["", "=" * 62, e["title"], "=" * 62, e["description"], ""]
    for f in e["fields"]:
        out += [f["name"], f["value"], ""]
    out += [(e.get("footer") or {}).get("text", ""), "=" * 62,
            "Taille embed : %d / 6000 caractères" % embed_size(e)]
    return NL.join(out)


def main():
    p = argparse.ArgumentParser(description="Annonce Discord des releases Axyvorn")
    p.add_argument("--mode", choices=["send", "dry-run", "seed"],
                   default=os.environ.get("ANNOUNCE_MODE", "send"))
    p.add_argument("--channel", choices=["prod", "test"],
                   default=os.environ.get("ANNOUNCE_CHANNEL", "prod"))
    p.add_argument("--tag", default=os.environ.get("ANNOUNCE_TAG", "") or None)
    p.add_argument("--source", choices=["event", "api"],
                   default="event" if os.environ.get("GITHUB_EVENT_NAME") == "release" else "api")
    p.add_argument("--force", action="store_true",
                   default=os.environ.get("ANNOUNCE_FORCE", "false").lower() == "true")
    p.add_argument("--include-prereleases", action="store_true",
                   default=os.environ.get("ANNOUNCE_PRERELEASES", "false").lower() == "true")
    p.add_argument("--state", default=os.environ.get("ANNOUNCE_STATE", ".github/discord-announced.json"))
    p.add_argument("--preview", default="discord-preview.json")
    p.add_argument("--max", type=int, default=int(os.environ.get("ANNOUNCE_MAX", "1")))
    args = p.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    state = load_state(args.state)
    log("Dépôt : %s | mode=%s | salon=%s | source=%s" % (REPO, args.mode, args.channel, args.source))

    if args.mode == "seed":
        added = 0
        for rel in list_releases(token):
            ok, _ = eligibility(rel, True)
            if ok and not already_announced(state, rel):
                record(state, rel, "seed")
                added += 1
                log("  mémorisé (sans annonce) : %s" % rel.get("tag_name"))
        save_state(args.state, state)
        gh_output({"sent": "false", "seeded": added})
        log("Seed terminé : %d release(s), aucun message envoyé." % added)
        return 0

    if args.source == "event" and not args.tag:
        rel = release_from_event()
        if not rel:
            log("Aucune release dans l'événement : rien à publier.")
            gh_output({"sent": "false"})
            return 0
        targets = [rel]
    elif args.tag:
        targets = [release_from_api(args.tag, token)]
    else:
        pending = [r for r in list_releases(token)
                   if eligibility(r, args.include_prereleases)[0] and not already_announced(state, r)]
        pending.sort(key=lambda r: r.get("published_at") or "")
        targets = pending[:max(1, args.max)]

    sent_any = False
    for rel in targets:
        ok, reason = eligibility(rel, args.include_prereleases)
        if not ok:
            log("Ignoré %s : %s" % (rel.get("tag_name"), reason))
            continue
        if already_announced(state, rel) and not args.force:
            log("Déjà annoncé : %s (id %s) -> aucun doublon." % (rel.get("tag_name"), rel["id"]))
            continue
        parsed = parse_changelog(rel.get("body"), rel.get("name") or "", rel.get("tag_name") or "")
        payload = build_payload(rel, parsed, test_mode=(args.channel == "test"))
        log(preview_text(payload))
        with open(args.preview, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if args.mode == "dry-run":
            log("MODE TEST (dry-run) : aperçu écrit dans %s, AUCUN message envoyé." % args.preview)
            gh_summary("### Aperçu Discord (dry-run) — %s" % version_label(rel))
            gh_output({"sent": "false", "tag": rel.get("tag_name"), "version": version_label(rel)})
            continue
        var = "DISCORD_WEBHOOK_TEST_URL" if args.channel == "test" else "DISCORD_WEBHOOK_URL"
        url = (os.environ.get(var) or "").strip()
        if not url:
            raise AnnounceError("Variable %s absente : configure le secret GitHub." % var)
        if not re.match(r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+$",
                        url.split("?")[0]):
            raise AnnounceError("Variable %s invalide : URL de webhook Discord attendue." % var)
        log("Envoi vers Discord (%s)..." % args.channel)
        message = send_webhook(url, payload)
        log("CONFIRMÉ par Discord : message id=%s channel_id=%s" % (message["id"], message.get("channel_id")))
        gh_summary("### Annonce publiée : %s%s- Message Discord : `%s`%s- Release : %s"
                   % (version_label(rel), NL, message["id"], NL, rel.get("html_url")))
        gh_output({"sent": "true", "tag": rel.get("tag_name"),
                   "version": version_label(rel), "message_id": message["id"]})
        sent_any = True
        if args.channel == "prod":
            record(state, rel, "prod", message)
            save_state(args.state, state)
        else:
            log("Salon de test : état prod inchangé (la vraie annonce reste à faire).")
    if not sent_any and args.mode == "send":
        log("Aucune nouvelle release à annoncer : rien n'a été envoyé.")
        gh_output({"sent": "false"})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AnnounceError as e:
        log("ERREUR : %s" % mask(e))
        gh_output({"sent": "false"})
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
