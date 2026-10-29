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
    # photo
    # ("reportage photo" couvre aussi "reportage photographique")
    "reportage photo": 6, "reportages photo": 6, "photographe": 5, "shooting": 5,
    "photographique": 4, "photographie": 4, "prise de vue": 4, "prises de vue": 4,
    "prises de vues": 4, "phototheque": 3, "banque d'images": 3, "photos": 2,
    # digital / site
    "refonte du site": 6, "refonte de site": 6, "refonte site": 6, "refonte des sites": 6,
    "refonte": 2, "creation de site": 5, "creation d'un site": 5, "creation du site": 5,
    "conception de site": 5, "conception d'un site": 5, "conception du site": 5,
    "realisation d'un site": 5, "realisation du site": 5, "developpement du site": 4,
    "developpement d'un site": 4, "nouveau site": 4, "site institutionnel": 5,
    "site vitrine": 5, "site e-commerce": 4, "portail web": 4, "portail internet": 4,
    "plateforme web": 3, "developpement web": 4, "webdesign": 5, "web design": 5,
    "ux design": 4, "ui design": 4, "experience utilisateur": 3, "ergonomie": 2,
    "wordpress": 3, "drupal": 3, "cms": 2,
    "site internet": 4, "site web": 4, "sites internet": 4, "sites web": 4,
    "accessibilite numerique": 2, "rgaa": 2,
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
    # faux amis de la photo
    "photocopi", "photovolta", "orthophoto", "photogrammetr", "photo d'identite", "photomaton",
]
# Exclusions levees si l'un de ces mots est aussi present
# ("refonte et maintenance du site" = projet web a garder ; "maintenance du site" seule = TMA, ecartee)
EXCLUS_SAUF_SI = {
    "maintenance": ["refonte", "creation", "conception", "realisation d'un site", "nouveau site"],
}
# Themes affiches dans les resultats (un tag par theme detecte)
THEMES = {
    "360 / campagne": ["360", "campagne", "agence de communication", "agence conseil", "agence creative", "communication globale", "plan de communication", "strategie de communication"],
    "Marque / branding": ["marque", "branding", "identite visuelle", "charte graphique", "rebranding", "positionnement"],
    "Video / photo": ["video", "film", "audiovisuel", "motion design", "podcast", "photographi",
                      "photographe", "reportage photo", "reportages photo", "shooting", "prise de vue",
                      "prises de vue", "phototheque", "banque d'images"],
    "Site / digital": ["site internet", "site web", "sites internet", "sites web", "refonte", "digital",
                       "rgaa", "portail", "webdesign", "web design", "ux design", "cms", "wordpress",
                       "drupal", "site vitrine", "site institutionnel", "developpement web"],
    "Social / influence": ["reseaux sociaux", "social media", "community", "influen", "tiktok", "instagram"],
    "Pub / RP / event": ["publicite", "relations presse", "evenementiel", "activation", "notoriete"],
}

# Mots-cles trouves seulement dans la description detaillee : poids reduit et plafonne
# (les descriptions sont longues et contiennent souvent du texte standard)
COEF_DESCRIPTION = 0.5
PLAFOND_DESCRIPTION = 15
TED_PAGES_MAX = 20  # 100 avis par page

BONUS_PRESSE = 6  # ajoute aux articles de presse qui annoncent une competition
SCORE_MINI = 3  # seuil bas : la page filtre ensuite (5+ par defaut)

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


def analyser_score(texte: str, exclusions: bool = True):
    """Renvoie (score, [[mot, poids], ...] tries par poids, mot_exclu)."""
    t = normaliser(texte)
    exclu = next((m for m in MOTS_EXCLUS if m in t
                  and not any(x in t for x in EXCLUS_SAUF_SI.get(m, []))), "") if exclusions else ""
    if exclu:
        return 0, [], exclu
    detail = sorted(([mot, poids] for mot, poids in MOTS_CLES.items() if mot in t),
                    key=lambda x: -x[1])
    return sum(p for _, p in detail), detail, ""


def scorer(texte: str) -> int:
    return analyser_score(texte)[0]


def calculer_score(a):
    """Score = mots-cles du titre (+ descripteurs) a plein poids
             + mots-cles supplementaires de la description (poids reduit, plafonne)
             + bonus eventuel (presse).
    Les mots exclus ne sont cherches que dans le titre : les descriptions
    contiennent trop de texte standard ("fourniture de", "maintenance"...)."""
    principal = f"{a['titre']} {a['_extrait']}"
    score, detail, exclu = analyser_score(principal)
    a["themes"] = detecter_themes(f"{principal} {a['_desc']}")
    if exclu:
        a.update(score=0, detail=[], exclu=exclu)
        return a
    detail = [[m, p, "t"] for m, p in detail]
    vus = {m for m, _, _ in detail}
    if a["_desc"]:
        reste = PLAFOND_DESCRIPTION
        for m, p in analyser_score(a["_desc"], exclusions=False)[1]:
            if m in vus or reste <= 0:
                continue
            pd = min(reste, max(1, round(p * COEF_DESCRIPTION)))
            detail.append([m, pd, "d"])
            reste -= pd
    a.update(score=sum(p for _, p, _ in detail) + a["bonus"], detail=detail, exclu="")
    return a


def nettoyer(txt) -> str:
    """Retire le HTML eventuel (resumes RSS) et compacte les espaces."""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", str(txt or "")))
    return re.sub(r"\s+", " ", txt).strip()


def _liste(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return "" if v is None else str(v)


def _format_montant(v) -> str:
    try:
        n = float(str(v).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return ""
    if n < 1000:  # en dessous, c'est rarement un montant de marche
        return ""
    return f"{n:,.0f} \u20ac HT".replace(",", " ")


def detecter_themes(texte: str) -> str:
    t = normaliser(texte)
    return " | ".join(n for n, mots in THEMES.items() if any(m in t for m in mots))


def annonce(source, ident, titre, acheteur, publie, echeance, url, extrait="",
            description="", departement="", procedure="", montant="",
            depot="", contact=None, lots=None):
    titre = nettoyer(titre)
    extrait = nettoyer(extrait)
    desc = nettoyer(description)
    if normaliser(desc) == normaliser(titre):
        desc = ""
    a = {
        "source": source, "id": f"{source}:{ident}", "titre": titre,
        "acheteur": nettoyer(acheteur), "publie": publie or "",
        "echeance": echeance or "", "url": url or "",
        "_extrait": extrait, "_desc": desc,   # textes complets utilises pour le score
        # infos affichees dans la popin
        "bonus": 0,                # bonus eventuel (annonce presse de competition)
        "description": "",
        "departement": nettoyer(departement), "procedure": nettoyer(procedure),
        "montant": montant or "",
        "aussi": [],               # meme avis publie sur une autre source (doublon fusionne)
        "depot": depot or "",      # lien vers la plateforme de depot / dossier de consultation
        "contact": contact or {},  # {nom, mail, tel}
        "lots": lots or [],        # [{num, titre, max, estime}]
    }
    _maj_description(a)
    return calculer_score(a)


def _maj_description(a):
    d = a["_desc"] or a["_extrait"]
    a["description"] = "" if normaliser(d) == normaliser(a["titre"]) else d[:700]


# --------------------------------------------------------------------------
# Source 1 : BOAMP (OpenDataSoft)
# --------------------------------------------------------------------------
BOAMP_URL = ("https://boamp-datadila.opendatasoft.com/api/explore/v2.1/"
             "catalog/datasets/boamp/records")


def _parcourir(obj):
    """Parcourt un JSON imbrique et renvoie des couples (cle, valeur feuille)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                if isinstance(v, dict) and "#text" in v:
                    yield k, v["#text"]
                yield from _parcourir(v)
            else:
                yield k, v
    elif isinstance(obj, list):
        for v in obj:
            yield from _parcourir(v)


def _infos_boamp(donnees):
    """Extrait (description, montant) du JSON complet de l'avis, si present.
    Le format varie selon les avis : tout est en 'meilleur effort'."""
    import json
    if isinstance(donnees, str):
        try:
            donnees = json.loads(donnees)
        except ValueError:
            return "", ""
    if not isinstance(donnees, (dict, list)):
        return "", ""
    description, montant = "", ""
    for cle, val in _parcourir(donnees):
        k = str(cle).upper()
        if not description and isinstance(val, str) and len(val) > 60 and (
                "DESCRIPTION" in k or "OBJET_COMPLET" in k or k in ("OBJET", "OBJET_MARCHE")):
            description = val
        if not montant and ("VALEUR" in k or "MONTANT" in k) and "DEVISE" not in k:
            montant = _format_montant(val)
        if description and montant:
            break
    return description, montant


# --- Plateforme de depot, contact et lots (meilleur effort, formats eForms et ancien BOAMP) ---
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Hebergeurs a ne jamais proposer comme lien de depot
HOTES_EXCLUS = ("boamp.fr", "europa.eu", "legifrance", "eur-lex", "juradm.fr", "telerecours",
                "w3.org", "data.gouv", "service-public", "economie.gouv.fr", "schemas")
# Mots presents dans le nom des plateformes de marches publics
HOTE_PLATEFORME = re.compile(r"marche|achat|megalis|maximilien|klekoon|synapse|ternum|xmarches|"
                             r"demat|place|aws|e-occitanie|alsacemarchespublics|centreofficielle|"
                             r"ampmetropole|salle")
# Mails a ne pas proposer comme contact (tribunal, greffe...)
MAILS_EXCLUS = ("greffe", "tribunal", "juradm", "telerecours", "ccira", "cnil")


def _hote(url) -> str:
    m = re.match(r"https?://([^/?#]+)", url or "")
    return (m.group(1).lower() if m else "").removeprefix("www.")


def _parcourir_chemin(obj, chemin=()):
    """Comme _parcourir, mais avec le chemin des cles (pour savoir d'ou vient un lien)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and "#text" in v and not isinstance(v["#text"], (dict, list)):
                yield chemin + (str(k),), v["#text"]
            if isinstance(v, (dict, list)):
                yield from _parcourir_chemin(v, chemin + (str(k),))
            else:
                yield chemin + (str(k),), v
    elif isinstance(obj, list):
        for v in obj:
            yield from _parcourir_chemin(v, chemin)


def _texte(v) -> str:
    if isinstance(v, dict):
        return _texte(v.get("#text") or v.get("fra") or v.get("FRA") or "")
    if isinstance(v, list):
        return _texte(v[0]) if v else ""
    return "" if v is None else str(v).strip()


def _choisir_depot(couples):
    """couples = [(chemin_en_majuscules, url)] -> meilleure URL de depot."""
    meilleur, score_max = "", 0
    for chemin, url in couples:
        url = url.rstrip(".,;")
        hote = _hote(url)
        if not hote or any(x in hote for x in HOTES_EXCLUS):
            continue
        sc = 1
        if any(x in chemin for x in ("ENDPOINTID", "SUBMISSION", "DEPOT", "PRESENTATION", "REMISE")):
            sc += 5
        elif any(x in chemin for x in ("CALLFORTENDERSDOCUMENT", "DOCUMENT", "DCE")):
            sc += 4
        elif any(x in chemin for x in ("BUYERPROFILE", "PROFIL")):
            sc += 3
        if HOTE_PLATEFORME.search(hote):
            sc += 2
        if re.search(r"consultation|cons|id=|refPub|idCons", url, re.I):
            sc += 1
        if sc > score_max:
            meilleur, score_max = url, sc
    return meilleur


def _contact(obj):
    """Cherche le dict qui porte un mail (hors tribunal) et prend nom / telephone a cote."""
    trouve = {}

    def visiter(o):
        if trouve:
            return
        if isinstance(o, dict):
            mail = tel = nom = ""
            for k, v in o.items():
                ku, tv = str(k).upper(), _texte(v)
                if not tv or isinstance(v, list) and len(v) > 1 and isinstance(v[0], dict):
                    continue
                if not mail and ("MAIL" in ku or ku.endswith("MEL") or "COURRIEL" in ku) and MAIL_RE.fullmatch(tv):
                    mail = tv
                elif not tel and ("TELEPHONE" in ku or ku.endswith("TEL") or ku == "TEL"):
                    tel = tv
                elif not nom and any(x in ku for x in ("CORRESPONDANT", "NOM_CONTACT", "CONTACT_NOM")) or (
                        not nom and ku.endswith(":NAME") and "PARTY" not in ku):
                    nom = tv if len(tv) < 80 else ""
            if mail and not any(x in mail.lower() for x in MAILS_EXCLUS):
                trouve.update(nom=nom, mail=mail, tel=tel)
                return
            for v in o.values():
                visiter(v)
        elif isinstance(o, list):
            for v in o:
                visiter(v)

    visiter(obj)
    return trouve


def _lots(obj):
    """Repere les lots (LOT-0001...) avec leur titre et leur montant maximum ou estime."""
    lots, vus = [], set()

    def montant_dans(o, cles):
        for chemin, v in _parcourir_chemin(o):
            c = chemin[-1].upper() if chemin else ""
            if any(x in c for x in cles) and "CURRENCY" not in c and "DEVISE" not in c:
                m = _format_montant(_texte(v))
                if m:
                    return m
        return ""

    def visiter(o):
        if isinstance(o, dict):
            ident = ""
            for k, v in o.items():
                ku = str(k).upper()
                tv = _texte(v)
                if (ku.endswith(":ID") or ku in ("ID", "NUM", "NUM_LOT", "NUMERO_LOT")) and re.match(
                        r"^(LOT-\d+|\d{1,3})$", tv) and ("LOT" in tv or "LOT" in ku or "LOT" in str(o)[:200].upper()):
                    ident = tv
                    break
            if ident and ident not in vus and not ident.startswith("LOT-0000"):
                titre = ""
                for chemin, v in _parcourir_chemin(o):
                    c = chemin[-1].upper() if chemin else ""
                    if (c.endswith(":NAME") or c in ("INTITULE", "TITRE", "INTITULE_LOT", "TITLE")) and isinstance(_texte(v), str) and len(_texte(v)) > 3:
                        titre = _texte(v)
                        break
                if titre:
                    vus.add(ident)
                    num = re.sub(r"^LOT-0*", "", ident) or ident
                    lots.append({
                        "num": num, "titre": titre[:160],
                        "max": montant_dans(o, ("FRAMEWORKMAXIMUM", "MAXIMUM", "MAX")),
                        "estime": montant_dans(o, ("ESTIMATEDOVERALL", "ESTIMATED", "VALEUR", "MONTANT")),
                    })
                    return
            for v in o.values():
                visiter(v)
        elif isinstance(o, list):
            for v in o:
                visiter(v)

    visiter(obj)
    return lots[:12]


def _infos_pratiques(donnees):
    """-> dict(depot=url, contact={nom, mail, tel}, lots=[...]) a partir du JSON de l'avis."""
    import json
    if isinstance(donnees, str):
        try:
            donnees = json.loads(donnees)
        except ValueError:
            # pas du JSON : on tente au moins les liens et le mail dans le texte brut
            urls = [("", u) for u in URL_RE.findall(donnees)]
            mails = [m for m in MAIL_RE.findall(donnees) if not any(x in m.lower() for x in MAILS_EXCLUS)]
            return {"depot": _choisir_depot(urls), "contact": {"mail": mails[0]} if mails else {}, "lots": []}
    if not isinstance(donnees, (dict, list)):
        return {"depot": "", "contact": {}, "lots": []}
    couples = []
    for chemin, v in _parcourir_chemin(donnees):
        if isinstance(v, str) and "http" in v:
            ch = "/".join(chemin).upper()
            couples += [(ch, u) for u in URL_RE.findall(v)]
    contact = _contact(donnees)
    if not contact:  # dernier recours : premier mail cite dans un texte libre
        for _, v in _parcourir_chemin(donnees):
            if isinstance(v, str):
                ms = [m for m in MAIL_RE.findall(v) if not any(x in m.lower() for x in MAILS_EXCLUS)]
                if ms:
                    contact = {"mail": ms[0].rstrip(".")}
                    break
    return {"depot": _choisir_depot(couples), "contact": contact, "lots": _lots(donnees)}


def source_boamp(jours: int):
    depuis = (date.today() - timedelta(days=jours)).isoformat()
    resultats, vus = [], set()
    requetes = ["communication", "agence", "campagne", "publicite", "marque", "branding",
                "identite visuelle", "video", "film", "refonte", "site internet", "site web",
                "portail", "web", "wordpress", "drupal",
                "photo", "photographie", "photographique", "photographe", "reportage",
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
                description, montant = _infos_boamp(x.get("donnees"))
                pratique = _infos_pratiques(x.get("donnees"))
                if not pratique["depot"]:
                    pratique["depot"] = _choisir_depot(
                        [("PROFIL", u) for u in URL_RE.findall(str(x.get("url_profil_acheteur") or ""))])
                procedure = " / ".join(filter(None, [
                    _liste(x.get("procedure_libelle") or x.get("type_procedure")),
                    _liste(x.get("nature_libelle"))]))
                resultats.append(annonce(
                    "BOAMP", ident, x.get("objet"), x.get("nomacheteur"),
                    (x.get("dateparution") or "")[:10],
                    (x.get("datelimitereponse") or "")[:10],
                    x.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{ident}",
                    _liste(x.get("descripteur_libelle")),
                    description=description,
                    departement=_liste(x.get("code_departement")),
                    procedure=procedure, montant=montant, **pratique,
                ))
            offset += 100
    return resultats


# --------------------------------------------------------------------------
# Source 2 : TED (API v3 de recherche)
# --------------------------------------------------------------------------
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
CHAMPS_TED = ["publication-number", "notice-title", "buyer-name",
              "publication-date", "deadline-receipt-tender-date-lot", "links"]
# Champs utilises pour la popin de detail (facultatifs : repli automatique si refuses)
CHAMPS_TED_DETAIL = ["description-proc", "description-lot", "procedure-type",
                     "estimated-value-proc", "estimated-value-lot",
                     "place-of-performance-subdiv-lot"]
# Lien de depot et contact : facultatifs aussi, retires en premier si l'API les refuse
CHAMPS_TED_PRATIQUE = ["document-url-lot", "submission-url-lot", "buyer-email"]


def _txt(v):
    """Les champs TED sont des dicts multilingues, des listes, ou les deux imbriques."""
    if isinstance(v, dict):
        if not v:
            return ""
        return _txt(v.get("fra") or v.get("FRA") or next(iter(v.values())))
    if isinstance(v, list):
        return _txt(v[0]) if v else ""
    return "" if v is None else str(v)


def source_ted(jours: int):
    depuis = (date.today() - timedelta(days=jours)).strftime("%Y%m%d")
    corps = {
        "query": (f"classification-cpv IN (79340000 79341000 79341100 79341200 79341400 79342000 79822500 92111200 72413000 72420000 72421000 72212224 79416000 79961000 79961100) "
                  f"AND buyer-country=FRA AND publication-date>={depuis}"),
        "fields": CHAMPS_TED + CHAMPS_TED_DETAIL + CHAMPS_TED_PRATIQUE,
        "limit": 100, "page": 1, "scope": "ALL",
    }
    out, page = [], 1
    while page <= TED_PAGES_MAX:
        corps["page"] = page
        r = requests.post(TED_URL, json=corps, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 400 and page == 1 and corps["fields"] != CHAMPS_TED:
            # un champ facultatif n'est pas reconnu par l'API : on retente avec moins de champs
            if len(corps["fields"]) > len(CHAMPS_TED + CHAMPS_TED_DETAIL):
                print("  [TED] champs depot/contact refuses, on continue sans", file=sys.stderr)
                corps["fields"] = CHAMPS_TED + CHAMPS_TED_DETAIL
            else:
                print("  [TED] champs de detail refuses, repli sur les champs de base", file=sys.stderr)
                corps["fields"] = CHAMPS_TED
            continue
        if not r.ok:
            if page == 1:
                raise RuntimeError(f"HTTP {r.status_code} - {r.text[:300]}")
            print(f"  [TED] page {page} en erreur (HTTP {r.status_code}), arret de la pagination", file=sys.stderr)
            break
        js = r.json()
        notices = js.get("notices", [])
        for n in notices:
            num = n.get("publication-number")
            out.append(annonce(
                "TED", num, _txt(n.get("notice-title")), _txt(n.get("buyer-name")),
                str(_txt(n.get("publication-date")))[:10],
                str(_txt(n.get("deadline-receipt-tender-date-lot")))[:10],
                f"https://ted.europa.eu/fr/notice/-/detail/{num}",
                description=_txt(n.get("description-lot")) or _txt(n.get("description-proc")),
                departement=_txt(n.get("place-of-performance-subdiv-lot")),
                procedure=_txt(n.get("procedure-type")),
                montant=_format_montant(_txt(n.get("estimated-value-proc")) or _txt(n.get("estimated-value-lot"))),
            depot=_choisir_depot([("SUBMISSION", u) for u in URL_RE.findall(_txt(n.get("submission-url-lot")))]
                                 + [("DOCUMENT", u) for u in URL_RE.findall(_txt(n.get("document-url-lot")))]),
            contact={"mail": m} if (m := _txt(n.get("buyer-email"))) and MAIL_RE.fullmatch(m) else {},
            ))
        total = js.get("totalNoticeCount")
        if len(notices) < corps["limit"] or (total and page * corps["limit"] >= total):
            break
        page += 1
    else:
        print(f"  [TED] limite de {TED_PAGES_MAX} pages atteinte (TED_PAGES_MAX)", file=sys.stderr)
    return out


# --------------------------------------------------------------------------
# Source 3 : flux RSS (presse specialisee)
# --------------------------------------------------------------------------
# Recherches presse (Google Actualites + Bing Actualites, sans cle API).
# Elles remontent les competitions d'agences annoncees par CB News, Strategies,
# La Reclame, Offremedia, etc., y compris pour les marques privees.
REQUETES_PRESSE = [
    'site:cbnews.fr (competition OR consultation OR "appel d\'offres" OR pitch)',
    'site:strategies.fr (competition OR consultation OR "appel d\'offres" OR pitch) agence',
    'site:lareclame.fr (competition OR consultation OR pitch) agence',
    'site:offremedia.com (competition OR consultation OR pitch) agence',
    '"lance une compétition" agence communication',
    '"lance une consultation" agence communication',
    '"recherche une agence" communication',
    '"appel d\'offres" "communication 360"',
    '"compétition" "stratégie de marque" OR branding OR "plateforme de marque"',
    '"compétition" agence "campagne" budget',
    'budget communication "en compétition"',
    '"consultation" agence "refonte" site marque',
]
PITCH_RE = re.compile(
    r"competition|consultation|appel d.offres|pitch|recherche une agence|"
    r"lance .{0,40}(agence|budget)|budget .{0,30}(en jeu|a pourvoir|en competition)")


def _entrees_flux(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def source_presse(jours: int):
    if not feedparser:
        raise RuntimeError("feedparser non installe (pip install feedparser)")
    from urllib.parse import urlencode
    flux_urls = []
    for q in REQUETES_PRESSE:
        flux_urls.append("https://news.google.com/rss/search?" + urlencode(
            {"q": f"{q} when:{jours}d", "hl": "fr", "gl": "FR", "ceid": "FR:fr"}))
    flux_urls += FLUX_RSS
    limite = (date.today() - timedelta(days=jours)).isoformat()
    out, erreurs = {}, 0
    for url in flux_urls:
        try:
            entrees = _entrees_flux(url)
        except Exception as e:
            erreurs += 1
            print(f"  [PRESSE] flux ignore ({str(e)[:80]})", file=sys.stderr)
            continue
        for e in entrees:
            titre = e.get("title", "")
            texte = f"{titre} {e.get('summary', '')}"
            nt = normaliser(texte)
            if not PITCH_RE.search(nt):
                continue
            pub = ""
            if e.get("published_parsed"):
                pub = datetime(*e.get("published_parsed")[:6]).date().isoformat()
            if pub and pub < limite:
                continue
            media = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if not media and " - " in titre:
                media = titre.rsplit(" - ", 1)[-1]
            a = annonce("PRESSE", e.get("link"), titre, media, pub, "", e.get("link"),
                        e.get("summary", ""))
            a["bonus"] = BONUS_PRESSE  # une annonce de competition/pitch est deja pertinente en soi
            calculer_score(a)          # (le bonus est annule si un mot exclu est present)
            out[a["id"]] = a
    if erreurs == len(flux_urls):
        raise RuntimeError("aucun flux presse accessible")
    return list(out.values())


# --------------------------------------------------------------------------
# Filtrage, base, exports
# --------------------------------------------------------------------------
def _mots_sig(txt) -> set:
    vides = {"pour", "dans", "avec", "des", "les", "marche", "prestations", "prestation",
             "services", "service", "accord", "cadre", "relatif", "relative", "france"}
    return {m for m in re.findall(r"[a-z0-9]{4,}", normaliser(txt)) if m not in vides}


def _jaccard(x, y) -> float:
    a, b = _mots_sig(x), _mots_sig(y)
    return len(a & b) / len(a | b) if a and b else 0.0


def _ecart_jours(d1, d2):
    try:
        return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)
    except (TypeError, ValueError):
        return None


def fusionner_doublons(annonces):
    """Un meme avis publie sur BOAMP et sur TED : on garde la fiche BOAMP,
    completee avec les infos TED, et on retire la fiche TED.
    Rapprochement : meme date limite (a 1 jour pres), objet et acheteur proches."""
    boamp = [a for a in annonces if a["source"] == "BOAMP"]
    a_retirer = set()
    for t in (a for a in annonces if a["source"] == "TED"):
        meilleur, sim_max = None, 0.0
        for b in boamp:
            ecart = _ecart_jours(t["echeance"], b["echeance"])
            sim_titre = _jaccard(t["titre"], b["titre"])
            sim_ach = _jaccard(t["acheteur"], b["acheteur"])
            if ecart is not None and ecart <= 1:
                ok = (sim_titre >= 0.45 and sim_ach >= 0.3) or sim_titre >= 0.75
            else:  # date limite absente d'un cote : on exige plus de ressemblance
                ecart_pub = _ecart_jours(t["publie"], b["publie"])
                ok = (ecart_pub is not None and ecart_pub <= 10
                      and sim_titre >= 0.75 and sim_ach >= 0.5)
            if ok and sim_titre + sim_ach > sim_max:
                meilleur, sim_max = b, sim_titre + sim_ach
        if not meilleur:
            continue
        b = meilleur
        if len(t["_desc"]) > len(b["_desc"]):
            b["_desc"] = t["_desc"]
        for champ in ("departement", "procedure", "montant", "echeance", "depot", "contact", "lots"):
            if not b[champ]:
                b[champ] = t[champ]
        b["aussi"].append({"source": "TED", "url": t["url"]})
        _maj_description(b)
        calculer_score(b)
        a_retirer.add(t["id"])
    return [a for a in annonces if a["id"] not in a_retirer], len(a_retirer)


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


SOURCES_STATUS = []
ENTONNOIR = []
COLS = ["nouveau", "score", "explication", "themes", "source", "titre", "acheteur",
        "departement", "procedure", "montant", "publie", "echeance", "url", "depot",
        "contact_mail", "aussi_sur"]
CHAMPS_PAGE = ["nouveau", "score", "detail", "bonus", "themes", "source", "titre", "acheteur",
               "description", "departement", "procedure", "montant", "publie", "echeance", "url",
               "aussi", "depot", "contact", "lots"]


def explication(a) -> str:
    parts = [f"{m} (+{p}{', description' if o == 'd' else ''})" for m, p, o in a.get("detail", [])]
    if a.get("bonus"):
        parts.append(f"competition presse (+{a['bonus']})")
    return ", ".join(parts)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veille consultations agences 360</title>
<style>
  :root { --bg:#fafaf7; --fg:#1d1d1b; --mut:#6b6b66; --line:#e2e1da; --card:#fff; --acc:#1a56db; --new:#fff4c2; --warn:#c2410c; --s1:#e8e7e0; --s2:#dbeafe; --s3:#bbf7d0; --s4:#fde68a; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#171716; --fg:#ecebe6; --mut:#9b9a93; --line:#33332f; --card:#202020; --acc:#6ea0ff; --new:#4a4210; --warn:#fb923c; --s1:#2d2d2a; --s2:#1e3a5f; --s3:#14532d; --s4:#713f12; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
  main { max-width:1200px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:22px; margin:0 0 2px; }
  .sub { color:var(--mut); font-size:13px; margin:0 0 16px; }
  .filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; margin-bottom:12px; }
  label { display:flex; flex-direction:column; font-size:12px; color:var(--mut); gap:4px; }
  select,input[type=text] { font:inherit; padding:7px 9px; border:1px solid var(--line); border-radius:7px; background:var(--bg); color:var(--fg); width:100%; }
  .chk { flex-direction:row; align-items:center; gap:8px; font-size:14px; color:var(--fg); padding-top:18px; }
  .bar { display:flex; justify-content:space-between; align-items:center; margin:8px 0; font-size:13px; color:var(--mut); gap:8px; flex-wrap:wrap; }
  button { font:inherit; font-size:13px; padding:6px 12px; border-radius:7px; border:1px solid var(--acc); background:transparent; color:var(--acc); cursor:pointer; }
  .wrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card); }
  table { border-collapse:collapse; width:100%; min-width:900px; }
  th,td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; font-size:14px; }
  th { font-size:12px; color:var(--mut); font-weight:600; white-space:nowrap; }
  tr.new td { background:var(--new); }
  a { color:var(--acc); text-decoration:none; } a:hover { text-decoration:underline; }
  .tag { font-size:11px; padding:1px 7px; border-radius:99px; border:1px solid var(--line); color:var(--mut); display:inline-block; margin:0 3px 3px 0; white-space:nowrap; }
  .pill { font-weight:600; padding:2px 9px; border-radius:99px; display:inline-block; }
  .p1 { background:var(--s1); } .p2 { background:var(--s2); } .p3 { background:var(--s3); } .p4 { background:var(--s4); }
  .soon { color:var(--warn); font-weight:600; }
  .empty { padding:28px; text-align:center; color:var(--mut); }

  /* Bandeau de synthese */
  .synth { display:grid; grid-template-columns:minmax(150px,.7fr) 1fr 1.3fr; gap:12px; margin-bottom:12px; }
  @media (max-width:860px) { .synth { grid-template-columns:1fr; } }
  .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; min-width:0; }
  .box h2 { font-size:13px; font-weight:600; color:var(--mut); margin:0 0 6px; display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
  .box h2 button { font-size:12px; padding:2px 8px; }
  .big { font-size:34px; font-weight:700; line-height:1.1; }
  .big small { font-size:13px; font-weight:400; color:var(--mut); display:block; margin-top:4px; }
  .mini { list-style:none; margin:0; padding:0; }
  .mini li { display:flex; gap:8px; align-items:baseline; padding:4px 0; border-top:1px solid var(--line); font-size:13px; }
  .mini li:first-child { border-top:0; }
  .mini a { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--fg); }
  .mini .k { flex:none; font-size:12px; color:var(--mut); min-width:34px; }
  .mini .k.soon { color:var(--warn); }
  .mini.none { color:var(--mut); font-size:13px; margin:0; }

  /* Volet de detail a droite */
  main { max-width:1560px; }
  .layout { display:grid; grid-template-columns:minmax(0,1fr) 390px; gap:16px; align-items:start; }
  table { min-width:760px; }
  tr[data-pop] { cursor:pointer; }
  tr[data-pop]:hover td { background:color-mix(in srgb, var(--acc) 6%, transparent); }
  tr.new[data-pop]:hover td { background:var(--new); }
  tr.sel td { background:color-mix(in srgb, var(--acc) 10%, transparent); }
  tr.sel td:first-child { box-shadow:inset 3px 0 0 var(--acc); }
  tr.sel.pin td:first-child { box-shadow:inset 5px 0 0 var(--acc); }
  li[data-pop] { cursor:pointer; }
  .panel { position:sticky; top:12px; max-height:calc(100vh - 24px); overflow:auto; background:var(--card); color:var(--fg);
           border:1px solid var(--line); border-radius:10px; font-size:13px; }
  .panel-top { position:sticky; top:0; z-index:1; display:flex; align-items:center; justify-content:space-between; gap:8px;
               padding:8px 12px 8px 16px; background:var(--card); border-bottom:1px solid var(--line); color:var(--mut); font-size:12px; }
  .panel-top .acts { display:flex; gap:6px; }
  .panel-top button { font-size:12px; padding:3px 9px; }
  .panel-top .pinned { color:var(--acc); font-weight:600; }
  #pClose { display:none; }
  .panel-body { padding:14px 16px 16px; }
  .panel .vide { color:var(--mut); line-height:1.6; margin:0; }
  .panel h3 { font-size:15px; margin:0 0 2px; line-height:1.35; }
  .panel .who { color:var(--mut); margin:0 0 12px; }
  .panel dl { display:grid; grid-template-columns:auto 1fr; gap:3px 12px; margin:0 0 10px; }
  .panel dt { color:var(--mut); } .panel dd { margin:0; overflow-wrap:anywhere; }
  .panel .desc { margin:0 0 10px; color:var(--fg); }
  .panel .why-h { display:flex; align-items:center; gap:8px; margin-bottom:6px; font-weight:600; }
  .panel .why-row { display:grid; grid-template-columns:1fr 70px 30px; gap:8px; align-items:center; padding:2px 0; }
  .panel .why-row span:last-child { text-align:right; font-variant-numeric:tabular-nums; color:var(--mut); }
  .panel .gauge { height:6px; border-radius:3px; background:var(--s1); overflow:hidden; }
  .panel .gauge i { display:block; height:100%; background:var(--acc); }
  .panel .gauge.b i { background:var(--warn); }
  .panel .gauge.d i { background:color-mix(in srgb, var(--acc) 45%, var(--s1)); }
  .panel .hint { color:var(--mut); font-size:12px; margin:10px 0 0; }
  .panel .go { display:block; text-align:center; background:var(--acc); color:#fff; border-radius:7px; padding:9px 10px; font-weight:600; margin:0 0 6px; }
  @media (prefers-color-scheme: dark) { .panel .go { color:#0d1b33; } }
  .panel .go:hover { text-decoration:none; filter:brightness(1.08); }
  .panel .go2 { display:block; text-align:center; border:1px solid var(--line); border-radius:7px; padding:7px 10px; margin:0 0 6px; }
  .panel .go-note { color:var(--mut); font-size:12px; margin:0 0 12px; }
  .panel .blk { border-top:1px solid var(--line); padding-top:10px; margin-top:10px; }
  .panel .blk-h { font-weight:600; margin-bottom:6px; }
  .panel .ct { display:grid; grid-template-columns:auto 1fr; gap:3px 12px; margin:0; }
  .panel .lot { display:grid; grid-template-columns:auto 1fr auto; gap:8px; padding:3px 0; align-items:baseline; }
  .panel .lot b { font-weight:600; color:var(--mut); font-size:12px; }
  .panel .lot span:last-child { color:var(--mut); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .panel .warn { color:var(--warn); }
  .dep { display:inline-block; margin-top:4px; font-size:12px; }
  /* Ecrans etroits : le volet devient un tiroir en bas de l'ecran */
  @media (max-width:1100px) {
    .layout { grid-template-columns:1fr; }
    .panel { position:fixed; left:8px; right:8px; bottom:8px; top:auto; max-height:72vh; z-index:40;
             box-shadow:0 -8px 30px rgba(0,0,0,.2); display:none; }
    .panel.open { display:block; }
    #pClose { display:inline-block; }
  }
</style>
</head>
<body>
<main>
  <h1>Consultations agences 360</h1>
  <p class="sub">Mise à jour du __DATE__ &middot; __STATUS__<br>Tri : __FUNNEL__ → <span id="shown"></span></p>

  <section class="synth" id="synth" aria-label="Synthèse"></section>

  <div class="filters">
    <label>Pertinence
      <select id="fScore">
        <option value="3">Toutes</option>
        <option value="5" selected>Pertinentes (5+)</option>
        <option value="8">Bonnes (8+)</option>
        <option value="12">Fortes (12+)</option>
        <option value="18">Très fortes (18+)</option>
      </select>
    </label>
    <label>Publiée
      <select id="fPub">
        <option value="0">Toutes dates</option>
        <option value="2">Depuis 48 h</option>
        <option value="7">7 derniers jours</option>
        <option value="14">14 derniers jours</option>
        <option value="30">30 derniers jours</option>
      </select>
    </label>
    <label>Date limite
      <select id="fEch">
        <option value="all">Toutes</option>
        <option value="7">Dans les 7 jours</option>
        <option value="14">Dans les 14 jours</option>
        <option value="30">Dans les 30 jours</option>
        <option value="30+">Dans plus de 30 jours</option>
        <option value="none">Non renseignée</option>
      </select>
    </label>
    <label>Thème <select id="fTheme"><option value="">Tous</option></select></label>
    <label>Source <select id="fSource"><option value="">Toutes</option></select></label>
    <label>Trier par
      <select id="fSort">
        <option value="score">Pertinence</option>
        <option value="pub">Publication récente</option>
        <option value="ech">Date limite proche</option>
      </select>
    </label>
    <label>Recherche <input type="text" id="fText" placeholder="acheteur, mot..."></label>
    <label class="chk"><input type="checkbox" id="fNew"> Nouveautés seulement</label>
  </div>

  <div class="bar">
    <span id="count"></span>
    <span><button id="reset">Réinitialiser</button> <button id="csv">Export CSV</button></span>
  </div>

  <div class="layout">
  <div class="wrap">
    <table>
      <thead><tr><th></th><th>Score</th><th>Thèmes</th><th>Source</th><th>Objet</th><th>Acheteur</th><th>Publiée</th><th>Date limite</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div id="empty" class="empty" hidden>Aucune annonce ne correspond à ces filtres.</div>
  </div>
  <aside class="panel" id="panel" aria-live="polite" aria-label="Détail de l’annonce">
    <div class="panel-top"><span id="pState">Aperçu au survol</span>
      <span class="acts"><button id="pPin" hidden>Épingler</button><button id="pClose">Fermer</button></span></div>
    <div class="panel-body" id="pBody"><p class="vide">Survolez une annonce pour afficher son détail ici.<br>Cliquez sur la ligne pour garder ce détail affiché pendant que vous parcourez la liste.</p></div>
  </aside>
  </div>
</main>

<script>
const DATA = __DATA__;

function norm(s) {
  return (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}
function daysBetween(a, b) { // b - a, en jours
  return Math.round((new Date(b + "T00:00:00") - new Date(a + "T00:00:00")) / 864e5);
}
function filtrer(rows, f, today) {
  const txt = norm(f.text);
  const out = rows.filter(a => {
    if (a.score < f.score) return false;
    if (f.pub > 0 && (!a.publie || daysBetween(a.publie, today) > f.pub)) return false;
    if (f.ech !== "all") {
      if (f.ech === "none") { if (a.echeance) return false; }
      else {
        if (!a.echeance) return false;
        const d = daysBetween(today, a.echeance);
        if (f.ech === "30+") { if (d <= 30) return false; }
        else if (d > parseInt(f.ech, 10)) return false;
      }
    }
    if (f.theme && !(a.themes || "").includes(f.theme)) return false;
    if (f.source && a.source !== f.source && !(a.aussi || []).some(x => x.source === f.source)) return false;
    if (f.onlyNew && !a.nouveau) return false;
    if (txt && !norm(a.titre + " " + a.acheteur).includes(txt)) return false;
    return true;
  });
  const key = {
    score: (x, y) => y.score - x.score || (y.publie || "").localeCompare(x.publie || ""),
    pub:   (x, y) => (y.publie || "").localeCompare(x.publie || "") || y.score - x.score,
    ech:   (x, y) => (x.echeance || "9999").localeCompare(y.echeance || "9999") || y.score - x.score
  }[f.sort];
  return out.sort(key);
}
if (typeof globalThis !== "undefined") globalThis.filtrer = filtrer;

if (typeof document !== "undefined") {
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const today = new Date().toISOString().slice(0, 10);
  let current = [];
  let selI = -1, pinned = false;   // annonce affichee dans le volet / volet fige

  DATA.forEach((a, i) => { a._i = i; a.detail = a.detail || []; });
  const themes = new Set(), sources = new Set();
  DATA.forEach(a => { (a.themes || "").split(" | ").filter(Boolean).forEach(t => themes.add(t)); sources.add(a.source); });
  [...themes].sort().forEach(t => $("fTheme").insertAdjacentHTML("beforeend", `<option>${esc(t)}</option>`));
  [...sources].sort().forEach(s => $("fSource").insertAdjacentHTML("beforeend", `<option>${esc(s)}</option>`));

  const ids = ["fScore", "fPub", "fEch", "fTheme", "fSource", "fSort", "fText", "fNew"];
  const read = () => ({
    score: +$("fScore").value, pub: +$("fPub").value, ech: $("fEch").value,
    theme: $("fTheme").value, source: $("fSource").value, sort: $("fSort").value,
    text: $("fText").value, onlyNew: $("fNew").checked
  });
  const hote = u => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return "la plateforme"; } };
  const pillClass = s => s >= 18 ? "p4" : s >= 12 ? "p3" : s >= 8 ? "p2" : "p1";

  function render() {
    current = filtrer(DATA, read(), today);
    $("rows").innerHTML = current.map(a => {
      let ech = "—", cls = "";
      if (a.echeance) {
        const d = daysBetween(today, a.echeance);
        ech = esc(a.echeance) + (d >= 0 ? ` (J-${d})` : "");
        if (d <= 7) cls = "soon";
      }
      const tags = (a.themes || "").split(" | ").filter(Boolean).map(t => `<span class="tag">${esc(t)}</span>`).join("");
      return `<tr class="${a.nouveau ? "new" : ""}${a._i === selI ? " sel" : ""}${a._i === selI && pinned ? " pin" : ""}" data-pop="${a._i}">
        <td>${a.nouveau ? '<span class="tag">NEW</span>' : ""}</td>
        <td><span class="pill ${pillClass(a.score)}">${a.score}</span></td>
        <td>${tags}</td><td><span class="tag">${esc(a.source)}</span>${(a.aussi || []).map(x => `<span class="tag" title="Aussi publié sur ${esc(x.source)}">+${esc(x.source)}</span>`).join("")}</td>
        <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.titre)}</a>${a.depot ? `<br><a class="dep" href="${esc(a.depot)}" target="_blank" rel="noopener">Déposer sur ${esc(hote(a.depot))}</a>` : ""}</td>
        <td>${esc(a.acheteur)}</td><td>${esc(a.publie)}</td><td class="${cls}">${ech}</td></tr>`;
    }).join("");
    $("empty").hidden = current.length > 0;
    $("count").textContent = `${current.length} annonce(s) sur ${DATA.length}`;
    $("shown").textContent = `${current.length} affichée(s) avec les filtres actuels`;
    renderSynth();
  }

  // ---------- Bandeau de synthese (suit le filtre Pertinence) ----------
  function renderSynth() {
    const mini = +$("fScore").value;
    const base = DATA.filter(a => a.score >= mini);
    const nouv = base.filter(a => a.nouveau);
    const semaine = base.filter(a => a.echeance && daysBetween(today, a.echeance) >= 0 && daysBetween(today, a.echeance) <= 7)
                        .sort((x, y) => x.echeance.localeCompare(y.echeance) || y.score - x.score);
    const top = [...base].sort((x, y) => y.score - x.score || (y.publie || "").localeCompare(x.publie || "")).slice(0, 5);
    const item = (a, k, kcls = "") => `<li data-pop="${a._i}"><span class="k ${kcls}">${k}</span>
      <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.titre)}</a></li>`;
    const liste = (arr, f, vide) => arr.length ? `<ul class="mini">${arr.map(f).join("")}</ul>` : `<p class="mini none">${vide}</p>`;
    $("synth").innerHTML = `
      <div class="box">
        <h2>Nouvelles annonces ${nouv.length ? '<button data-act="new">Afficher</button>' : ""}</h2>
        <div class="big">${nouv.length}<small>depuis le dernier passage</small></div>
      </div>
      <div class="box">
        <h2>Échéances cette semaine ${semaine.length ? '<button data-act="week">Afficher</button>' : ""}</h2>
        ${liste(semaine.slice(0, 5), a => { const d = daysBetween(today, a.echeance); return item(a, d === 0 ? "auj." : "J-" + d, d <= 3 ? "soon" : ""); }, "Aucune date limite dans les 7 jours.")}
      </div>
      <div class="box">
        <h2>Top 5 des scores</h2>
        ${liste(top, a => item(a, `<span class="pill ${pillClass(a.score)}">${a.score}</span>`), "Aucune annonce à ce niveau de pertinence.")}
      </div>`;
  }
  $("synth").addEventListener("click", e => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    if (b.dataset.act === "new") { $("fNew").checked = true; }
    if (b.dataset.act === "week") { $("fEch").value = "7"; $("fSort").value = "ech"; }
    render();
    $("rows").closest(".wrap").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // ---------- Volet de detail a droite ----------
  const etroit = () => matchMedia("(max-width:1100px)").matches || matchMedia("(hover: none)").matches;
  let tSurvol = 0;

  function popHTML(a) {
    const infos = [
      ["Département", a.departement], ["Procédure", a.procedure], ["Montant estimé", a.montant],
      ["Publiée", a.publie],
      ["Date limite", a.echeance ? a.echeance + (daysBetween(today, a.echeance) >= 0 ? ` (J-${daysBetween(today, a.echeance)})` : "") : ""]
    ].filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")
      + ((a.aussi || []).length ? `<dt>Aussi publié sur</dt><dd>${a.aussi.map(x => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.source)}</a>`).join(", ")}</dd>` : "");
    const lignes = a.detail.map(([m, p, o]) => [o === "d" ? m + " (description)" : m, p, o === "d" ? "d" : ""]);
    if (a.bonus) lignes.push(["annonce de compétition (presse)", a.bonus, "b"]);
    const max = Math.max(1, ...lignes.map(l => l[1]));
    const why = lignes.length
      ? lignes.map(([m, p, c]) => `<div class="why-row"><span>${esc(m)}</span><span class="gauge ${c}"><i style="width:${Math.round(p / max * 100)}%"></i></span><span>+${p}</span></div>`).join("")
      : `<p class="hint">Aucun mot-clé détaillé pour cette annonce.</p>`;
    const desc = a.description ? `<p class="desc">${esc(a.description.length > 450 ? a.description.slice(0, 450) + "…" : a.description)}</p>` : "";
    const c = a.contact || {};
    let depot = "";
    if (a.depot) {
      depot = `<a class="go" href="${esc(a.depot)}" target="_blank" rel="noopener">Dossier et dépôt sur ${esc(hote(a.depot))}</a>
        <p class="go-note" style="margin-bottom:6px">Les questions sur le contenu se posent aussi sur cette plateforme.</p>`;
    } else if (a.source !== "PRESSE") {
      depot = `<p class="go-note warn">Lien de dépôt non trouvé : cherchez « profil d’acheteur » ou « adresse de présentation » dans l’avis.</p>`;
    }
    const contact = (c.mail || c.tel || c.nom) ? `<div class="blk"><div class="blk-h">Contact</div><dl class="ct">
        ${c.nom ? `<dt>Nom</dt><dd>${esc(c.nom)}</dd>` : ""}
        ${c.mail ? `<dt>Mail</dt><dd><a href="mailto:${esc(c.mail)}">${esc(c.mail)}</a></dd>` : ""}
        ${c.tel ? `<dt>Téléphone</dt><dd><a href="tel:${esc(c.tel.replace(/\s/g, ""))}">${esc(c.tel)}</a></dd>` : ""}
      </dl>${a.depot ? `<p class="hint" style="margin-top:6px">Pour les renseignements administratifs.</p>` : ""}</div>` : "";
    const lots = (a.lots || []).length ? `<div class="blk"><div class="blk-h">${a.lots.length} lot${a.lots.length > 1 ? "s" : ""}</div>
      ${a.lots.map(l => `<div class="lot"><b>Lot ${esc(l.num)}</b><span>${esc(l.titre)}</span><span>${l.max ? esc(l.max) + " max" : esc(l.estime || "")}</span></div>`).join("")}</div>` : "";
    return `<h3>${esc(a.titre)}</h3>
      <p class="who">${esc(a.acheteur || "Acheteur non renseigné")} – ${esc(a.source)}</p>
      ${depot}<a class="go2" href="${esc(a.url)}" target="_blank" rel="noopener">Voir l’avis complet (${esc(a.source)})</a>${desc}${infos ? `<dl>${infos}</dl>` : ""}${lots}${contact}
      <div class="why blk"><div class="why-h">Pourquoi ce score <span class="pill ${pillClass(a.score)}">${a.score}</span></div>${why}</div>
`;
  }
  function afficher(i, figer) {
    const a = DATA[i];
    if (!a) return;
    selI = i;
    if (figer !== undefined) pinned = figer;
    $("pBody").innerHTML = popHTML(a);
    $("pBody").parentElement.scrollTop = 0;
    $("pState").textContent = pinned ? "Épinglée" : "Aperçu au survol";
    $("pState").className = pinned ? "pinned" : "";
    $("pPin").hidden = false;
    $("pPin").textContent = pinned ? "Libérer" : "Épingler";
    document.querySelectorAll("#rows tr").forEach(tr => {
      const on = +tr.dataset.pop === selI;
      tr.classList.toggle("sel", on);
      tr.classList.toggle("pin", on && pinned);
    });
  }
  function liberer() { if (selI >= 0) afficher(selI, false); }

  // Survol : le volet suit l'annonce survolee (petit delai pour ignorer les passages rapides)
  document.addEventListener("mouseover", e => {
    if (etroit() || pinned) return;
    const el = e.target.closest("[data-pop]");
    if (!el || $("panel").contains(el)) return;
    clearTimeout(tSurvol);
    if (+el.dataset.pop !== selI) tSurvol = setTimeout(() => afficher(+el.dataset.pop), 120);
  });
  // Clic sur une ligne (hors liens) : epingle / libere ; sur ecran etroit, ouvre le tiroir
  document.addEventListener("click", e => {
    const el = e.target.closest("[data-pop]");
    if (!el || e.target.closest("a, button")) return;
    clearTimeout(tSurvol);
    const i = +el.dataset.pop;
    if (etroit()) { afficher(i, true); $("panel").classList.add("open"); return; }
    if (pinned && i === selI) liberer(); else afficher(i, true);
  });
  $("pPin").addEventListener("click", () => pinned ? liberer() : afficher(selI, true));
  $("pClose").addEventListener("click", () => { $("panel").classList.remove("open"); liberer(); });
  // Clavier : le volet suit le lien qui a le focus
  document.addEventListener("focusin", e => {
    const el = e.target.closest && e.target.closest("[data-pop]");
    if (el && !pinned && !$("panel").contains(el)) afficher(+el.dataset.pop);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { $("panel").classList.remove("open"); liberer(); }
  });

  ids.forEach(id => $(id).addEventListener("input", render));
  $("reset").addEventListener("click", () => {
    $("fScore").value = "5"; $("fPub").value = "0"; $("fEch").value = "all"; $("fTheme").value = "";
    $("fSource").value = ""; $("fSort").value = "score"; $("fText").value = ""; $("fNew").checked = false; render();
  });
  $("csv").addEventListener("click", () => {
    const cols = ["nouveau", "score", "explication", "themes", "source", "titre", "acheteur", "departement", "procedure", "montant", "publie", "echeance", "url", "depot", "contact_mail", "aussi_sur"];
    const q = v => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
    current.forEach(r => {
      r.explication = r.detail.map(([m, p, o]) => `${m} (+${p}${o === "d" ? ", description" : ""})`).concat(r.bonus ? [`compétition presse (+${r.bonus})`] : []).join(", ");
      r.aussi_sur = (r.aussi || []).map(x => x.url).join(" ");
      r.contact_mail = (r.contact || {}).mail || "";
    });
    const csv = "\ufeff" + [cols.join(";")].concat(current.map(r => cols.map(c => q(r[c])).join(";"))).join("\r\n");
    const l = document.createElement("a");
    l.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    l.download = "veille_360_" + today + ".csv"; l.click();
  });
  render();
}
</script>
</body>
</html>
"""


def exporter(annonces):
    for a in annonces:
        a["explication"] = explication(a)
        a["aussi_sur"] = " ".join(x["url"] for x in a.get("aussi", []))
        a["contact_mail"] = (a.get("contact") or {}).get("mail", "")
    with open("veille_com360.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(annonces)

    import json
    data = [{k: x.get(k, "") for k in CHAMPS_PAGE} for x in annonces]
    donnees = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (HTML_TEMPLATE
            .replace("__DATE__", f"{date.today():%d/%m/%Y}")
            .replace("__STATUS__", html.escape("  |  ".join(SOURCES_STATUS)))
            .replace("__FUNNEL__", html.escape(" → ".join(ENTONNOIR)))
            .replace("__DATA__", donnees))
    with open("veille_com360.html", "w", encoding="utf-8") as f:
        f.write(page)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--jours", type=int, default=30)
    p.add_argument("--nouveaux", action="store_true", help="ne garder que les annonces jamais vues")
    args = p.parse_args()

    toutes = []
    for nom, fn in [("BOAMP", source_boamp), ("TED", source_ted), ("PRESSE", source_presse)]:
        try:
            res = fn(args.jours)
            print(f"[{nom}] {len(res)} annonces recuperees")
            SOURCES_STATUS.append(f"{nom} : OK ({len(res)})")
            toutes += res
        except Exception as e:  # une source en panne ne bloque pas les autres
            print(f"[{nom}] ERREUR : {e}", file=sys.stderr)
            SOURCES_STATUS.append(f"{nom} : ERREUR ({str(e)[:90]})")

    uniques = list({a["id"]: a for a in toutes}.values())
    uniques, nb_fusions = fusionner_doublons(uniques)
    retenues = [a for a in uniques if a["score"] >= SCORE_MINI and encore_ouvert(a)]
    ENTONNOIR.append(f"{len(toutes)} annonces récupérées")
    if nb_fusions:
        ENTONNOIR.append(f"{nb_fusions} doublon(s) BOAMP/TED fusionné(s)")
    ENTONNOIR.append(f"{len(retenues)} retenues (score {SCORE_MINI}+ et encore ouvertes)")
    print("Tri : " + " -> ".join(ENTONNOIR))
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
