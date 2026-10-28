#!/usr/bin/env python3
"""
Veille des consultations "agence de communication 360".

Sources :
  1. BOAMP (API open data officielle)      -> marches publics francais
  2. TED   (API officielle europeenne)     -> avis publies au JOUE, France
  3. Flux RSS configurables (CB News, etc) -> competitions privees / presse

Sortie : veille_com360.csv + veille_com360.html (tries par score puis echeance)
Base SQLite locale pour ne signaler que les NOUVELLES annonces a chaque passage.

Usage :
    pip install requests feedparser
    python veille_ao_com360.py                # 30 derniers jours
    python veille_ao_com360.py --jours 14
    python veille_ao_com360.py --nouveaux     # uniquement ce qui n'a jamais ete vu

NB : les noms de champs des API BOAMP/TED peuvent evoluer. Si une source
renvoie une erreur, le script continue avec les autres et affiche le detail.
"""
import argparse
import csv
import html
import re
import sqlite3
import sys
import unicodedata
import webbrowser
import os
from datetime import date, datetime, timedelta

import requests

try:
    import feedparser
except ImportError:  # RSS optionnel
    feedparser = None

# --------------------------------------------------------------------------
# CONFIG - a adapter
# --------------------------------------------------------------------------
# Mots-cles avec poids (profil : agence creative 360 - strategie de marque,
# branding, campagnes, video, digital/site, social media, influence).
MOTS_CLES = {
    # 360 / agence
    "communication 360": 10, "agence 360": 10, "agence conseil en communication": 8,
    "agence de communication": 7, "agence creative": 7, "agence de publicite": 7,
    "campagne de communication": 8, "campagne publicitaire": 8, "campagne de publicite": 8,
    "strategie de communication": 7, "conseil en communication": 6,
    "plan de communication": 6, "communication globale": 6,
    # marque / branding
    "strategie de marque": 8, "plateforme de marque": 8, "branding": 7,
    "identite de marque": 7, "identite visuelle": 6, "rebranding": 7,
    "image de marque": 5, "marque employeur": 5, "charte graphique": 5,
    "creation de marque": 6, "positionnement de marque": 6, "creation graphique": 4,
    # video / contenus
    "production video": 7, "production audiovisuelle": 7, "film institutionnel": 7, "films institutionnels": 7, "films publicitaires": 8,
    "production de films": 7, "realisation de films": 7, "contenus video": 5,
    "film publicitaire": 8, "motion design": 5, "podcast": 4, "video": 3, "film": 2,
    "contenus editoriaux": 4, "creation de contenus": 4,
    # digital / site
    "refonte du site": 6, "refonte de site": 6, "refonte site": 6, "creation de site": 5,
    "site internet": 3, "site web": 3, "accessibilite numerique": 2, "rgaa": 2,
    "marketing digital": 4, "strategie digitale": 5,
    # social / influence
    "reseaux sociaux": 4, "social media": 4, "community management": 4,
    "influence": 4, "influenceur": 4, "tiktok": 3, "instagram": 2,
    # autres leviers
    "publicite": 3, "relations presse": 3, "evenementiel": 2, "activation": 2,
    "marketing": 2, "notoriete": 3, "lancement de": 1,
}
# Mots qui disqualifient (bruit frequent)
MOTS_EXCLUS = [
    "achat d'espace seul", "centre d'appels", "teleconseiller", "sondage",
    "maintenance", "nettoyage", "gmao", "videosurveillance", "video-surveillance",
    "videoprotection", "visioconference", "telecommunication", "communications electroniques",
    "location de", "fourniture de", "travaux de", "securite incendie",
]
# Themes affiches dans les resultats (un tag par theme detecte)
THEMES = {
    "360 / campagne": ["360", "campagne", "agence de communication", "agence conseil", "agence creative", "communication globale", "plan de communication", "strategie de communication"],
    "Marque / branding": ["marque", "branding", "identite visuelle", "charte graphique", "rebranding", "positionnement"],
    "Video": ["video", "film", "audiovisuel", "motion design", "podcast"],
    "Site / digital": ["site internet", "site web", "refonte", "digital", "rgaa"],
    "Social / influence": ["reseaux sociaux", "social media", "community", "influen", "tiktok", "instagram"],
    "Pub / RP / event": ["publicite", "relations presse", "evenementiel", "activation", "notoriete"],
}

SCORE_MINI = 5

# Flux RSS presse / veille (ajoutez les votres, verifiez les URL de flux)
FLUX_RSS = [
    # "https://www.cbnews.fr/rss.xml",
    # "https://www.strategies.fr/rss.xml",
]

DB_PATH = "veille_com360.sqlite"
HEADERS = {"User-Agent": "veille-ao-com360/1.0 (usage interne)"}
TIMEOUT = 30


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def normaliser(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt.lower().replace("\u2019", "'")).strip()


def scorer(texte: str) -> int:
    t = normaliser(texte)
    if any(m in t for m in MOTS_EXCLUS):
        return 0
    return sum(poids for mot, poids in MOTS_CLES.items() if mot in t)


def detecter_themes(texte: str) -> str:
    t = normaliser(texte)
    return " | ".join(n for n, mots in THEMES.items() if any(m in t for m in mots))


def annonce(source, ident, titre, acheteur, publie, echeance, url, extrait=""):
    return {
        "themes": detecter_themes(f"{titre} {extrait}"),
        "source": source, "id": f"{source}:{ident}", "titre": (titre or "").strip(),
        "acheteur": (acheteur or "").strip(), "publie": publie or "",
        "echeance": echeance or "", "url": url or "",
        "score": scorer(f"{titre} {extrait}"),
    }


# --------------------------------------------------------------------------
# Source 1 : BOAMP (OpenDataSoft)
# --------------------------------------------------------------------------
BOAMP_URL = ("https://boamp-datadila.opendatasoft.com/api/explore/v2.1/"
             "catalog/datasets/boamp/records")


def source_boamp(jours: int):
    depuis = (date.today() - timedelta(days=jours)).isoformat()
    resultats, vus = [], set()
    requetes = ["communication", "agence", "campagne", "publicite", "marque", "branding",
                "identite visuelle", "video", "film", "refonte", "site internet",
                "influence", "reseaux sociaux", "creation graphique", "marketing", "podcast"]
    for mot in requetes:
        offset = 0
        while offset < 300:
            params = {
                "where": f'dateparution >= date\'{depuis}\' AND search(objet, "{mot}")',
                "order_by": "dateparution desc",
                "limit": 100, "offset": offset,
            }
            r = requests.get(BOAMP_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            lignes = r.json().get("results", [])
            if not lignes:
                break
            for x in lignes:
                ident = x.get("idweb") or x.get("id")
                if ident in vus:
                    continue
                vus.add(ident)
                resultats.append(annonce(
                    "BOAMP", ident, x.get("objet"), x.get("nomacheteur"),
                    (x.get("dateparution") or "")[:10],
                    (x.get("datelimitereponse") or "")[:10],
                    x.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{ident}",
                    str(x.get("descripteur_libelle") or ""),
                ))
            offset += 100
    return resultats


# --------------------------------------------------------------------------
# Source 2 : TED (API v3 de recherche)
# --------------------------------------------------------------------------
TED_URL = "https://api.ted.europa.eu/v3/notices/search"


def _txt(v):
    """Les champs TED sont souvent des dicts multilingues ou des listes."""
    if isinstance(v, dict):
        return v.get("fra") or v.get("FRA") or next(iter(v.values()), "") if v else ""
    if isinstance(v, list):
        return _txt(v[0]) if v else ""
    return v or ""


def source_ted(jours: int):
    depuis = (date.today() - timedelta(days=jours)).strftime("%Y%m%d")
    corps = {
        "query": (f"classification-cpv IN (79340000 79341000 79341100 79341200 79341400 79342000 79822500 92111200 72413000 79416000) "
                  f"AND buyer-country=FRA AND publication-date>={depuis}"),
        "fields": ["publication-number", "notice-title", "buyer-name",
                   "publication-date", "deadline-receipt-tender-date-lot", "links"],
        "limit": 100, "page": 1, "scope": "ALL",
    }
    r = requests.post(TED_URL, json=corps, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for n in r.json().get("notices", []):
        num = n.get("publication-number")
        out.append(annonce(
            "TED", num, _txt(n.get("notice-title")), _txt(n.get("buyer-name")),
            str(_txt(n.get("publication-date")))[:10],
            str(_txt(n.get("deadline-receipt-tender-date-lot")))[:10],
            f"https://ted.europa.eu/fr/notice/-/detail/{num}",
        ))
    return out


# --------------------------------------------------------------------------
# Source 3 : flux RSS (presse specialisee)
# --------------------------------------------------------------------------
def source_rss(jours: int):
    if not feedparser or not FLUX_RSS:
        return []
    out = []
    for url in FLUX_RSS:
        flux = feedparser.parse(url)
        for e in flux.entries:
            texte = f"{e.get('title', '')} {e.get('summary', '')}"
            # on ne garde que les articles qui parlent de competition / consultation
            if not re.search(r"competition|consultation|appel d.offres|pitch|budget", normaliser(texte)):
                continue
            pub = ""
            if e.get("published_parsed"):
                pub = datetime(*e.published_parsed[:6]).date().isoformat()
            out.append(annonce("PRESSE", e.get("link"), e.get("title"),
                               flux.feed.get("title", ""), pub, "", e.get("link"),
                               e.get("summary", "")))
    return out


# --------------------------------------------------------------------------
# Filtrage, base, exports
# --------------------------------------------------------------------------
def encore_ouvert(a) -> bool:
    if not a["echeance"]:
        return True  # echeance inconnue : on garde
    return a["echeance"] >= date.today().isoformat()


def marquer_nouveaux(annonces):
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS vues (id TEXT PRIMARY KEY, vu_le TEXT)")
    for a in annonces:
        deja = con.execute("SELECT 1 FROM vues WHERE id=?", (a["id"],)).fetchone()
        a["nouveau"] = not deja
        if not deja:
            con.execute("INSERT INTO vues VALUES (?, ?)", (a["id"], date.today().isoformat()))
    con.commit()
    con.close()


COLS = ["nouveau", "score", "themes", "source", "titre", "acheteur", "publie", "echeance", "url"]


def exporter(annonces):
    with open("veille_com360.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(annonces)

    lignes = "".join(
        f"<tr class='{'new' if a['nouveau'] else ''}'><td>{'NEW' if a['nouveau'] else ''}</td>"
        f"<td>{a['score']}</td><td>{html.escape(a['themes'])}</td><td>{a['source']}</td>"
        f"<td><a href='{html.escape(a['url'])}'>{html.escape(a['titre'])}</a></td>"
        f"<td>{html.escape(a['acheteur'])}</td><td>{a['publie']}</td><td>{a['echeance']}</td></tr>"
        for a in annonces)
    page = f"""<!doctype html><meta charset='utf-8'><title>Veille 360</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}}.new{{background:#fff8d6}}</style>
<h1>Consultations agences 360 - {date.today():%d/%m/%Y}</h1>
<table><tr><th></th><th>Score</th><th>Thèmes</th><th>Source</th><th>Objet</th><th>Acheteur</th><th>Publie</th><th>Echeance</th></tr>
{lignes}</table>"""
    with open("veille_com360.html", "w", encoding="utf-8") as f:
        f.write(page)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jours", type=int, default=30)
    p.add_argument("--nouveaux", action="store_true", help="ne garder que les annonces jamais vues")
    args = p.parse_args()

    toutes = []
    for nom, fn in [("BOAMP", source_boamp), ("TED", source_ted), ("RSS", source_rss)]:
        try:
            res = fn(args.jours)
            print(f"[{nom}] {len(res)} annonces recuperees")
            toutes += res
        except Exception as e:  # une source en panne ne bloque pas les autres
            print(f"[{nom}] ERREUR : {e}", file=sys.stderr)

    uniques = {a["id"]: a for a in toutes}.values()
    retenues = [a for a in uniques if a["score"] >= SCORE_MINI and encore_ouvert(a)]
    marquer_nouveaux(retenues)
    if args.nouveaux:
        retenues = [a for a in retenues if a["nouveau"]]
    retenues.sort(key=lambda a: (-a["score"], a["echeance"] or "9999"))

    exporter(retenues)
    print(f"{len(retenues)} annonces pertinentes -> veille_com360.csv / veille_com360.html")
    for a in retenues[:10]:
        print(f"  [{a['score']:>2}] {a['echeance'] or '?':10} {a['source']:6} {a['titre'][:80]}")
    if not os.environ.get("CI"):
        webbrowser.open("file://" + os.path.abspath("veille_com360.html"))  # ouvre le resultat


if __name__ == "__main__":
    main()
