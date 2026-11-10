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
    python veille_ao_com360.py --nouveaux     # uniquement les annonces reperees aujourd hui

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

# --------------------------------------------------------------------------
# SITES A SURVEILLER : pages "appels d'offres" d'organismes qui publient leurs
# consultations sur leur propre site (interprofessions, federations, URPS...).
# Le script lit la page chaque jour et garde les liens qui parlent de consultation.
# Ajoutez vos pages ici ; une page qui ne repond pas est signalee dans le terminal.
# --------------------------------------------------------------------------
SITES_A_SURVEILLER = [
    {"nom": "Interbev (viande)", "url": "https://www.interbev.fr/interbev/appels-doffres/"},
    {"nom": "CNIEL (filiere laitiere)", "url": "http://www.filiere-laitiere.fr/en/appels-doffres"},
    {"nom": "Interfel (fruits et legumes)", "url": "https://interfel.e-marchepublics.com/"},
]
BONUS_SITE = 4  # lien qui parle explicitement de consultation, sur une page surveillee

# Mots qui signalent une consultation dans un lien ou un bloc de texte
MOTS_CONSULTATION = [
    "appel d'offres", "appel d'offre", "appels d'offres", "appel a concurrence",
    "appels a concurrence", "appel a candidatures", "appel a candidature", "mise en concurrence",
    "mise en competition", "consultation", "cahier des charges", "reglement de consultation",
    "recherche une agence", "recherche d'une agence", "selection d'une agence", "choix d'une agence",
]

# --------------------------------------------------------------------------
# MARQUES A SUIVRE : pas d'appel d'offres publie, mais des SIGNAUX dans l'actualite
# (nouveau directeur marketing, levee de fonds, rachat, changement d'identite...).
# "precision" (facultatif) : mots ajoutes a la recherche pour eviter les homonymes.
# --------------------------------------------------------------------------
MARQUES_A_SUIVRE = [
    # --- Marques d'Ile-de-France a potentiel digital (selection a qualifier) ---
    # Maisons parisiennes historiques
    {"marque": "Mariage Freres", "precision": "the"},
    {"marque": "Kusmi Tea"},
    {"marque": "Cire Trudon", "precision": "bougie OR bougies OR maison"},
    {"marque": "Repetto", "precision": "ballerines OR danse OR mode"},
    {"marque": "Le Tanneur", "precision": "maroquinerie OR sacs"},
    {"marque": "Lancel", "precision": "maroquinerie OR sacs"},
    {"marque": "Pylones", "precision": "boutique OR cadeaux OR objets"},
    {"marque": "Christofle"},
    # Boissons et food made in Paris / IDF
    {"marque": "Paname Brewing"},
    {"marque": "Brasserie de la Goutte d'Or"},
    {"marque": "Distillerie de Paris"},
    {"marque": "Maison Chateau Rouge"},
    {"marque": "Le Slip Francais"},

    # --- Grands groupes qui se developpent sur les tendances ---
    # (type "groupe" : recherche et poids orientes nouvelles marques, tendances, acquisitions)
    # Spiritueux, vins, bieres
    {"marque": "Diageo", "type": "groupe"},
    {"marque": "Pernod Ricard", "type": "groupe"},
    {"marque": "Moet Hennessy", "type": "groupe"},
    {"marque": "Remy Cointreau", "type": "groupe"},
    {"marque": "Bacardi", "type": "groupe"},
    {"marque": "Heineken", "type": "groupe"},
    {"marque": "Kronenbourg", "type": "groupe"},
    {"marque": "AB InBev", "type": "groupe"},
    # Boissons et alimentaire
    {"marque": "Coca-Cola", "type": "groupe"},
    {"marque": "PepsiCo", "type": "groupe"},
    {"marque": "Danone", "type": "groupe"},
    {"marque": "Nestle", "type": "groupe"},
    {"marque": "Mondelez", "type": "groupe"},
    {"marque": "Unilever", "type": "groupe"},
    {"marque": "Ferrero", "type": "groupe"},
    {"marque": "Groupe Bel", "type": "groupe"},
    {"marque": "Savencia", "type": "groupe"},
    {"marque": "Lactalis", "type": "groupe"},
    # Beaute
    {"marque": "L'Oreal", "type": "groupe"},
    {"marque": "Coty", "precision": "parfum OR beaute OR cosmetique", "type": "groupe"},
]
SIGNAUX_REQUETE = ('agence OR "appel d\'offres" OR "directeur marketing" OR "directrice marketing" '
                   'OR "directeur de la communication" OR "directrice de la communication" OR "levee de fonds" '
                   'OR rachat OR reprise OR "nouvelle identite" OR rebranding OR lancement OR recrute')
# Grands groupes : on cherche les lancements de marques, les tendances et les acquisitions
SIGNAUX_REQUETE_GROUPE = ('lance OR lancement OR "nouvelle marque" OR "sans alcool" OR tendance OR rachat '
                          'OR acquisition OR "prise de participation" OR agence OR "appel d\'offres" '
                          'OR "directeur marketing" OR "directrice marketing"')
MOTS_SIGNAUX_GROUPE = {
    # besoin d'agence explicite
    "recherche une agence": 10, "lance une competition": 10, "change d'agence": 8,
    "nouvelle agence": 6, "appel d'offres": 5, "confie son budget": 4, "retient l'agence": 3,
    # nouvelles marques / produits
    "lance une marque": 7, "lance sa marque": 6, "nouvelle marque": 6, "nouvelle boisson": 5,
    "nouvelle gamme": 4, "edition limitee": 4, "lancement": 3, "lance ": 2, "en france": 2,
    # tendances
    "sans alcool": 5, "no-low": 5, "no & low": 5, "faible teneur": 5, "pret-a-boire": 5,
    "pret a boire": 5, "ready to drink": 5, "ready-to-drink": 5, "premiumisation": 4,
    "boisson fonctionnelle": 5, "proteine": 3, "vegetal": 3, "plant-based": 4, "vegan": 3,
    "gen z": 4, "generation z": 4, "tiktok": 3, "influence": 3, "collaboration": 3,
    "collab": 3, "pop-up": 4, "tendance": 2, "premium": 2,
    # acquisitions de marques emergentes
    "rachat": 4, "rachete": 4, "acquisition": 4, "acquiert": 4, "prise de participation": 4,
    "start-up": 3, "startup": 3, "incubateur": 3,
    # organisation marketing
    "directeur marketing": 5, "directrice marketing": 5, "nouveau directeur": 3,
    "nouvelle directrice": 3, "nomme": 2,
}
# Les grands groupes font beaucoup parler d'eux : seuil plus haut pour ne garder que les vrais signaux
SEUIL_GROUPE = 7

# Poids des signaux (meme principe que MOTS_CLES)
MOTS_SIGNAUX = {
    "recherche une agence": 10, "lance une competition": 10, "lance un appel d'offres": 10,
    "consultation d'agences": 10, "change d'agence": 8, "nouvelle agence": 6,
    "confie son budget": 3, "retient l'agence": 3, "choisit l'agence": 3,
    "directeur marketing": 5, "directrice marketing": 5, "directeur de la communication": 5,
    "directrice de la communication": 5, "directeur du marketing": 5, "directrice du marketing": 5,
    "chief marketing officer": 5, "nomme": 2, "nomination": 2, "rejoint": 1,
    "levee de fonds": 6, "leve ": 2, "millions d'euros": 2,
    "rachat": 4, "rachete": 4, "reprise": 4, "repreneur": 4,
    "rebranding": 5, "nouvelle identite": 5, "nouveau logo": 5, "change de nom": 5,
    "e-commerce": 3, "boutique en ligne": 3, "site marchand": 3, "digital": 2,
    "lancement": 2, "lance ": 1, "nouvelle gamme": 2, "international": 2, "export": 2,
    "anniversaire": 2, "recrute": 2, "ouvre": 1, "croissance": 1,
}

DB_PATH = "veille_com360.sqlite"
HEADERS = {"User-Agent": "veille-ao-com360/1.0 (usage interne)"}
TIMEOUT = 30


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------
def normaliser(txt: str) -> str:
    # apostrophes typographiques (’ ‘ ʼ) remplacees AVANT la suppression des accents,
    # sinon "creation d’un site" devenait "creation dun site" et ne matchait plus
    txt = re.sub("[\u2019\u2018\u02bc\u00b4`]", "'", txt or "")
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt.lower()).strip()


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


# ---- Sites surveilles ----------------------------------------------------
MOIS_FR = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
           "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}
DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})\b")
DATE_TXT_RE = re.compile(r"\b(\d{1,2})(?:er)? (janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre) (20\d{2})\b")


def _dates_dans(texte):
    """Dates trouvees dans un texte (deja normalise), sous forme de date()."""
    out = []
    for j, m, a in DATE_NUM_RE.findall(texte):
        try:
            out.append(date(int(a), int(m), int(j)))
        except ValueError:
            pass
    for j, m, a in DATE_TXT_RE.findall(texte):
        try:
            out.append(date(int(a), MOIS_FR[m], int(j)))
        except ValueError:
            pass
    return out


def _liens_page(page_html, base_url):
    """-> [(texte_du_lien, url_absolue, texte_autour)] pour chaque lien de la page."""
    from html.parser import HTMLParser
    from urllib.parse import urljoin

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.liens, self.en_cours, self.textes = [], None, []
            self.lignes = [0]
            for ligne in page_html.splitlines(keepends=True):
                self.lignes.append(self.lignes[-1] + len(ligne))

        def pos(self):
            l, c = self.getpos()
            return self.lignes[l - 1] + c

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                d = dict(attrs)
                if d.get("href") and not d["href"].startswith(("#", "mailto:", "tel:", "javascript:")):
                    self.en_cours = [d["href"], [d.get("title") or ""], self.pos()]

        def handle_data(self, data):
            if self.en_cours:
                self.en_cours[1].append(data)

        def handle_endtag(self, tag):
            if tag == "a" and self.en_cours:
                href, morceaux, debut = self.en_cours
                self.liens.append((" ".join(" ".join(morceaux).split()), urljoin(base_url, href), debut))
                self.en_cours = None

    p = P()
    try:
        p.feed(page_html)
    except Exception:
        pass
    out = []
    bas = page_html.lower()
    for texte, url, debut in p.liens:
        out.append((texte, url, nettoyer(_bloc_autour(page_html, bas, debut))))
    return out


def _bloc_autour(page_html, bas, debut):
    """Texte de l'element qui contient le lien (ligne de liste, de tableau, article...),
    pour y lire les dates sans deborder sur les annonces voisines."""
    for ouvrante, fermante in (("<li", "</li"), ("<tr", "</tr"), ("<article", "</article"), ("<div", "</div")):
        i = bas.rfind(ouvrante, max(0, debut - 1500), debut + 1)
        if i == -1:
            continue
        j = bas.find(fermante, debut)
        if j != -1 and j - debut < 2000:
            return page_html[i:j]
    return page_html[max(0, debut - 300): debut + 300]


def _autorise(url) -> bool:
    """Respecte le fichier robots.txt du site (en cas de doute, on autorise)."""
    from urllib import robotparser
    from urllib.parse import urlsplit
    sp = urlsplit(url)
    try:
        r = requests.get(f"{sp.scheme}://{sp.netloc}/robots.txt", headers=HEADERS, timeout=10)
        if r.status_code >= 400:
            return True
        rp = robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except requests.RequestException:
        return True


def source_sites(jours: int):
    if not SITES_A_SURVEILLER:
        return []
    limite = date.today() - timedelta(days=jours)
    out, erreurs = {}, []
    for site in SITES_A_SURVEILLER:
        nom, url = site["nom"], site["url"]
        try:
            if not _autorise(url):
                erreurs.append(f"{nom} (robots.txt)")
                continue
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
        except Exception as e:
            erreurs.append(nom)
            print(f"  [SITES] {nom} : page inaccessible ({str(e)[:80]})", file=sys.stderr)
            continue
        trouves = 0
        for texte, lien, autour in _liens_page(r.text, url):
            if len(texte) < 12:
                continue
            nt = normaliser(texte)
            na = normaliser(autour)
            if not any(m in nt or m in normaliser(lien) for m in MOTS_CONSULTATION):
                # le lien seul n'en parle pas : on accepte s'il est tres pertinent et que le bloc parle de consultation
                if not (scorer(texte) >= 6 and any(m in na for m in MOTS_CONSULTATION)):
                    continue
            # Dates autour du lien : une date passee = publication, une date future = date limite
            dates = _dates_dans(nt) or _dates_dans(na)
            passees = [d for d in dates if d <= date.today()]
            futures = [d for d in dates if d > date.today()]
            publie = max(passees).isoformat() if passees else ""
            echeance = min(futures).isoformat() if futures else ""
            if publie and not echeance and date.fromisoformat(publie) < limite:
                continue  # annonce ancienne, sans date limite a venir
            a = annonce("SITES", lien, texte, nom, publie, echeance, lien)
            if a["score"] > 0:
                a["bonus"] = BONUS_SITE
                calculer_score(a)
            a["depot"] = lien
            out[a["id"]] = a
            trouves += 1
        print(f"  [SITES] {nom} : {trouves} lien(s) de consultation")
    if erreurs and len(erreurs) == len(SITES_A_SURVEILLER):
        raise RuntimeError("aucun site accessible : " + ", ".join(erreurs))
    if erreurs:
        AVERTISSEMENTS.append(f"sites en erreur : {', '.join(erreurs)}")
    return list(out.values())


# ---- Signaux marques (actualite Google News) ------------------------------
def source_signaux(jours: int):
    if not MARQUES_A_SUIVRE:
        return []
    if not feedparser:
        raise RuntimeError("feedparser non installe (pip install feedparser)")
    import time
    from urllib.parse import urlencode
    limite = (date.today() - timedelta(days=jours)).isoformat()
    out, erreurs = {}, 0
    for m in MARQUES_A_SUIVRE:
        marque = m["marque"]
        groupe = m.get("type") == "groupe"
        requete = SIGNAUX_REQUETE_GROUPE if groupe else SIGNAUX_REQUETE
        poids = MOTS_SIGNAUX_GROUPE if groupe else MOTS_SIGNAUX
        q = f'"{marque}"' + (f" ({m['precision']})" if m.get("precision") else "") + f" ({requete}) when:{jours}d"
        url = "https://news.google.com/rss/search?" + urlencode({"q": q, "hl": "fr", "gl": "FR", "ceid": "FR:fr"})
        try:
            entrees = _entrees_flux(url)
        except Exception as e:
            erreurs += 1
            print(f"  [SIGNAUX] {marque} : flux ignore ({str(e)[:60]})", file=sys.stderr)
            continue
        nm = normaliser(marque)
        for e in entrees:
            titre = e.get("title", "")
            if nm not in normaliser(titre):
                continue  # la marque doit etre dans le titre (evite les mentions en passant)
            pub = ""
            if e.get("published_parsed"):
                pub = datetime(*e.get("published_parsed")[:6]).date().isoformat()
            if pub and pub < limite:
                continue
            texte = normaliser(f"{titre} {e.get('summary', '')}")
            detail = sorted(([k, p, "t"] for k, p in poids.items() if k in texte), key=lambda x: -x[1])
            if not detail or (groupe and sum(p for _, p, _ in detail) < SEUIL_GROUPE):
                continue
            media = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if not media and " - " in titre:
                media = titre.rsplit(" - ", 1)[-1]
            a = annonce("SIGNAUX", e.get("link"), titre, marque, pub, "", e.get("link"), e.get("summary", ""))
            a["detail"], a["score"], a["exclu"] = detail, sum(p for _, p, _ in detail), ""
            a["themes"] = "Signal groupe" if groupe else "Signal marque"
            a["procedure"] = f"Actualité ({media})" if media else "Actualité"
            out[a["id"]] = a
        time.sleep(1)  # politesse envers Google News
    if erreurs == len(MARQUES_A_SUIVRE):
        raise RuntimeError("aucun flux accessible")
    return list(out.values())


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
    auj = date.today().isoformat()
    for a in annonces:
        deja = con.execute("SELECT vu_le FROM vues WHERE id=?", (a["id"],)).fetchone()
        # "Nouvelle" = reperee pour la premiere fois aujourd'hui : relancer le script
        # plusieurs fois dans la journee ne fait plus disparaitre les nouveautes du jour.
        a["vu_le"] = deja[0] if deja else auj
        a["nouveau"] = a["vu_le"] == auj
        if not deja:
            con.execute("INSERT INTO vues VALUES (?, ?)", (a["id"], auj))
    con.commit()
    con.close()


SOURCES_STATUS = []
AVERTISSEMENTS = []
ENTONNOIR = []
COLS = ["nouveau", "score", "explication", "themes", "source", "titre", "acheteur",
        "departement", "procedure", "montant", "publie", "echeance", "url", "depot",
        "contact_mail", "aussi_sur"]
CHAMPS_PAGE = ["id", "nouveau", "vu_le", "score", "detail", "bonus", "themes", "source", "titre", "acheteur",
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
  :root {
    --bg:#f6f5f2; --fg:#1c1b19; --mut:#6d6b65; --line:#e4e2db; --card:#ffffff; --soft:#f0eee8;
    --acc:#4f46e5; --acc2:#7c3aed; --warn:#ea580c; --danger:#dc2626; --ok:#16a34a; --fav:#e0a100;
    --shadow:0 1px 2px rgba(20,20,40,.05), 0 4px 14px rgba(20,20,40,.06);
    --shadow-h:0 2px 4px rgba(20,20,40,.06), 0 12px 28px rgba(20,20,40,.12);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#141416; --fg:#ecebe8; --mut:#a09e98; --line:#2e2e33; --card:#1d1d21; --soft:#26262b;
      --acc:#8b85ff; --acc2:#b18cff; --warn:#fb923c; --danger:#f87171; --ok:#4ade80; --fav:#fbbf24;
      --shadow:0 1px 2px rgba(0,0,0,.3); --shadow-h:0 10px 28px rgba(0,0,0,.45);
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:1560px; margin:0 auto; padding:24px 20px 64px; }
  a { color:var(--acc); text-decoration:none; } a:hover { text-decoration:underline; }
  .ic { width:16px; height:16px; flex:none; stroke:currentColor; fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; vertical-align:-3px; }
  .ic.s { width:14px; height:14px; } .ic.l { width:22px; height:22px; }

  /* En-tete */
  .head { display:flex; align-items:center; gap:14px; margin-bottom:18px; }
  .logo { width:46px; height:46px; border-radius:13px; display:grid; place-items:center; color:#fff;
          background:linear-gradient(135deg,#4f46e5,#a855f7 55%,#ec4899); box-shadow:0 6px 16px rgba(124,58,237,.35); }
  h1 { font-size:23px; margin:0; letter-spacing:-.01em; }
  .sub { color:var(--mut); font-size:12.5px; margin:2px 0 0; }

  /* Bandeau de synthese */
  .synth { display:grid; grid-template-columns:minmax(200px,.7fr) 1fr 1.3fr; gap:14px; margin-bottom:14px; }
  @media (max-width:900px) { .synth { grid-template-columns:1fr; } }
  .box { --c:var(--acc); background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px; min-width:0;
         box-shadow:var(--shadow); border-top:4px solid var(--c); }
  .box h2 { font-size:13px; font-weight:650; margin:0 0 8px; display:flex; align-items:center; gap:8px; }
  .box h2 .bi { width:26px; height:26px; border-radius:8px; display:grid; place-items:center; color:var(--c);
                background:color-mix(in srgb, var(--c) 15%, transparent); }
  .box h2 button { margin-left:auto; }
  .big { font-size:38px; font-weight:750; line-height:1.05; color:var(--c); }
  .big small { font-size:12.5px; font-weight:400; color:var(--mut); display:block; margin-top:4px; }
  .mini { list-style:none; margin:0; padding:0; }
  .mini li { display:flex; gap:8px; align-items:center; padding:5px 0; border-top:1px solid var(--line); font-size:13px; cursor:pointer; }
  .mini li:first-child { border-top:0; }
  .mini li:hover .t { color:var(--acc); }
  .mini .t { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .mini .k { flex:none; font-size:11.5px; font-weight:650; color:var(--mut); min-width:38px; }
  .mini .k.soon { color:var(--danger); } .mini .k.mid { color:var(--warn); }
  .mini.none { color:var(--mut); font-size:13px; margin:0; cursor:default; }

  /* Filtres */
  .filters { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:12px; background:var(--card);
             border:1px solid var(--line); border-radius:14px; padding:14px 16px; margin-bottom:12px; box-shadow:var(--shadow); }
  label { display:flex; flex-direction:column; font-size:12px; color:var(--mut); gap:4px; font-weight:550; }
  select, input[type=text] { font:inherit; font-size:14px; font-weight:400; padding:7px 10px; border:1px solid var(--line); border-radius:9px; background:var(--soft); color:var(--fg); width:100%; }
  select:focus, input:focus { outline:2px solid color-mix(in srgb, var(--acc) 50%, transparent); outline-offset:1px; }
  .chk { flex-direction:row; align-items:center; gap:8px; font-size:14px; color:var(--fg); padding-top:18px; font-weight:400; }
  .chk input { accent-color:var(--acc); width:16px; height:16px; }
  .bar { display:flex; justify-content:space-between; align-items:center; margin:10px 0 12px; font-size:13px; color:var(--mut); gap:8px; flex-wrap:wrap; }
  .bar .acts { display:flex; gap:8px; }
  button { font:inherit; font-size:13px; padding:6px 12px; border-radius:9px; border:1px solid var(--line); background:var(--card); color:var(--fg);
           cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
  button:hover { border-color:var(--acc); color:var(--acc); }
  button:focus-visible, a:focus-visible { outline:2px solid var(--acc); outline-offset:2px; }
  .box h2 button { font-size:12px; padding:3px 9px; }

  /* Grille de cartes : 4 a 5 par ligne sur grand ecran */
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(255px, 1fr)); gap:14px; }
  .card { --c:var(--acc); position:relative; display:flex; flex-direction:column; gap:10px; background:var(--card);
          border:1px solid var(--line); border-radius:14px; padding:14px 14px 12px; box-shadow:var(--shadow);
          cursor:pointer; transition:transform .15s, box-shadow .15s, border-color .15s; overflow:hidden; }
  .card::before { content:""; position:absolute; inset:0 0 auto 0; height:4px; background:var(--c); }
  .card:hover { transform:translateY(-2px); box-shadow:var(--shadow-h); border-color:color-mix(in srgb, var(--c) 40%, var(--line)); }
  @media (prefers-reduced-motion: reduce) { .card { transition:none; } .card:hover { transform:none; } }
  .card.new { background:linear-gradient(180deg, color-mix(in srgb, #facc15 12%, var(--card)), var(--card) 45%); }
  .top { display:flex; align-items:center; gap:6px; min-height:28px; }
  .src { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; font-weight:700; letter-spacing:.02em; padding:3px 8px;
         border-radius:99px; color:color-mix(in srgb, var(--c) 85%, var(--fg)); background:color-mix(in srgb, var(--c) 14%, transparent); }
  .newb { display:inline-flex; align-items:center; gap:4px; font-size:11px; font-weight:700; padding:3px 7px; border-radius:99px;
          color:#8a5a00; background:#fde68a; }
  @media (prefers-color-scheme: dark) { .newb { color:#1c1400; background:#fbbf24; } }
  .plus { font-size:11px; color:var(--mut); }
  .top .sp { flex:1; }
  .pill { font-weight:750; font-size:13px; min-width:34px; height:28px; padding:0 9px; border-radius:99px; display:inline-grid; place-items:center; font-variant-numeric:tabular-nums; }
  .p1 { background:var(--soft); color:var(--mut); }
  .p2 { background:color-mix(in srgb, #3b82f6 18%, transparent); color:color-mix(in srgb, #2563eb 85%, var(--fg)); }
  .p3 { background:color-mix(in srgb, #22c55e 20%, transparent); color:color-mix(in srgb, #15803d 85%, var(--fg)); }
  .p4 { background:linear-gradient(135deg,#f59e0b,#ef4444); color:#fff; box-shadow:0 3px 10px rgba(239,68,68,.3); }
  .star { border:0; background:transparent; padding:3px; border-radius:8px; color:var(--mut); line-height:0; }
  .star:hover { color:var(--fav); background:color-mix(in srgb, var(--fav) 12%, transparent); }
  .star[aria-pressed="true"] { color:var(--fav); }
  .star[aria-pressed="true"] .ic { fill:currentColor; }
  .card h3 { font-size:15px; line-height:1.35; font-weight:650; margin:0; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }
  .who { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--mut); min-width:0; }
  .who span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .meta { display:flex; flex-wrap:wrap; gap:6px; }
  .m { display:inline-flex; align-items:center; gap:5px; font-size:12px; padding:3px 8px; border-radius:8px; background:var(--soft); color:var(--fg); white-space:nowrap; }
  .m.soon { background:color-mix(in srgb, var(--danger) 15%, transparent); color:var(--danger); font-weight:650; }
  .m.mid { background:color-mix(in srgb, var(--warn) 15%, transparent); color:var(--warn); font-weight:650; }
  .m.none { color:var(--mut); }
  .themes { display:flex; flex-wrap:wrap; gap:5px; }
  .th { --c:#64748b; display:inline-flex; align-items:center; gap:4px; font-size:11.5px; font-weight:600; padding:2px 8px; border-radius:99px;
        color:color-mix(in srgb, var(--c) 85%, var(--fg)); background:color-mix(in srgb, var(--c) 13%, transparent); }
  .foot { margin-top:auto; display:flex; gap:6px; padding-top:10px; border-top:1px solid var(--line); }
  .btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; font-size:13px; font-weight:600; padding:7px 10px; border-radius:9px; white-space:nowrap; }
  .btn.pri { flex:1; background:var(--c); color:#fff; border:1px solid transparent; }
  .btn.pri:hover { text-decoration:none; filter:brightness(1.08); }
  @media (prefers-color-scheme: dark) { .btn.pri { color:#111; } }
  .btn.sec { flex:1; border:1px solid var(--line); color:var(--fg); background:transparent; }
  .btn.sec:hover { border-color:var(--c); color:var(--c); text-decoration:none; }
  .btn.ico { flex:none; width:36px; padding:7px 0; border:1px solid var(--line); color:var(--mut); background:transparent; }
  .btn.ico:hover { color:var(--c); border-color:var(--c); }
  .empty { padding:48px 20px; text-align:center; color:var(--mut); background:var(--card); border:1px dashed var(--line); border-radius:14px; }

  /* Fenetre de detail */
  dialog.dlg { width:min(640px, calc(100vw - 24px)); max-height:min(86vh, 900px); padding:0; border:1px solid var(--line);
               border-radius:16px; background:var(--card); color:var(--fg); box-shadow:0 24px 60px rgba(0,0,0,.3); overflow:hidden; }
  dialog.dlg::backdrop { background:rgba(15,15,25,.45); backdrop-filter:blur(2px); }
  .dlg-in { display:flex; flex-direction:column; max-height:min(86vh, 900px); }
  .dlg-top { --c:var(--acc); display:flex; align-items:center; gap:8px; padding:12px 14px 12px 18px; border-bottom:1px solid var(--line);
             border-top:5px solid var(--c); }
  .dlg-top .sp { flex:1; }
  .x { border:0; background:transparent; padding:6px; line-height:0; border-radius:8px; color:var(--mut); }
  .x:hover { background:var(--soft); color:var(--fg); }
  .dlg-body { padding:16px 20px 20px; overflow:auto; font-size:14px; }
  .dlg-body h3 { font-size:18px; margin:0 0 4px; line-height:1.3; }
  .dlg-body .who2 { color:var(--mut); margin:0 0 14px; display:flex; align-items:center; gap:6px; }
  .acts2 { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
  .acts2 .btn { flex:1 1 200px; padding:9px 12px; }
  .note { color:var(--mut); font-size:12.5px; margin:4px 0 14px; }
  .note.warn { color:var(--warn); }
  .dlg-body .desc { margin:0 0 14px; }
  .dlg-body dl { display:grid; grid-template-columns:auto 1fr; gap:5px 14px; margin:0 0 6px; }
  .dlg-body dt { color:var(--mut); display:flex; align-items:center; gap:6px; } .dlg-body dd { margin:0; overflow-wrap:anywhere; }
  .blk { border-top:1px solid var(--line); padding-top:12px; margin-top:12px; }
  .blk-h { font-weight:650; margin-bottom:8px; display:flex; align-items:center; gap:8px; }
  .lot { display:grid; grid-template-columns:auto 1fr auto; gap:10px; padding:4px 0; align-items:baseline; }
  .lot b { font-weight:650; color:var(--mut); font-size:12px; }
  .lot span:last-child { color:var(--mut); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .why-row { display:grid; grid-template-columns:1fr 90px 34px; gap:10px; align-items:center; padding:3px 0; }
  .why-row span:last-child { text-align:right; font-variant-numeric:tabular-nums; color:var(--mut); }
  .gauge { height:7px; border-radius:4px; background:var(--soft); overflow:hidden; }
  .gauge i { display:block; height:100%; background:linear-gradient(90deg, var(--acc), var(--acc2)); }
  .gauge.b i { background:var(--warn); }
  .gauge.d i { background:color-mix(in srgb, var(--acc) 40%, var(--soft)); }
  .hint { color:var(--mut); font-size:12.5px; margin:8px 0 0; }
  @media (max-width:600px) { main { padding:16px 12px 48px; } .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<main>
  <div class="head">
    <div class="logo"><svg class="ic l" viewBox="0 0 24 24"><path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg></div>
    <div>
      <h1>Consultations agences 360</h1>
      <p class="sub">Mise à jour du __DATE__ &middot; __STATUS__<br>Tri : __FUNNEL__ → <span id="shown"></span></p>
    </div>
  </div>

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
    <label>Recherche <input type="text" id="fText" placeholder="acheteur, mot…"></label>
    <label class="chk"><input type="checkbox" id="fNew"> Nouveautés seulement</label>
    <label class="chk"><input type="checkbox" id="fFav"> <span id="fFavLbl">Favoris seulement</span></label>
  </div>

  <div class="bar">
    <span id="count"></span>
    <span class="acts"><button id="reset">Réinitialiser</button> <button id="csv">Export CSV</button></span>
  </div>

  <section class="grid" id="cards" aria-label="Annonces"></section>
  <div id="empty" class="empty" hidden>Aucune annonce ne correspond à ces filtres.</div>
</main>

<dialog class="dlg" id="dlg" aria-labelledby="dlgTitle">
  <div class="dlg-in">
    <div class="dlg-top" id="dlgTop"></div>
    <div class="dlg-body" id="dlgBody"></div>
  </div>
</dialog>

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
    if (f.onlyFav && !a.fav) return false;
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
  let current = [], ouvert = -1;

  // ---------- Pictos (traits SVG, couleur = texte) ----------
  const P = {
    landmark: '<path d="M3 21h18"/><path d="M5 21V11"/><path d="M19 21V11"/><path d="M9.5 21V11"/><path d="M14.5 21V11"/><path d="M12 3 21 8H3z"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>',
    news: '<path d="M4 5h12v14H6a2 2 0 0 1-2-2z"/><path d="M16 9h4v8a2 2 0 0 1-4 0"/><path d="M7.5 9h5"/><path d="M7.5 12.5h5"/><path d="M7.5 16h3"/>',
    link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1.2 1.2"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/>',
    radar: '<path d="M4.9 19.1a10 10 0 0 1 0-14.2"/><path d="M7.8 16.2a6 6 0 0 1 0-8.4"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 0 1 0 8.4"/><path d="M19.1 4.9a10 10 0 0 1 0 14.2"/>',
    star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>',
    spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.5 2.5M15.2 15.2l2.5 2.5M6.3 17.7l2.5-2.5M15.2 8.8l2.5-2.5"/>',
    building: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 7h1M14 7h1M9 11h1M14 11h1M9 15h1M14 15h1"/><path d="M10 21v-3h4v3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    euro: '<path d="M18 6.5A7 7 0 1 0 18 17.5"/><path d="M4 10h9M4 14h9"/>',
    pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    send: '<path d="M21 3 10 14"/><path d="m21 3-7 18-4-7-7-4z"/>',
    ext: '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    trophy: '<path d="M8 4h8v5a4 4 0 0 1-8 0z"/><path d="M8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4"/><path d="M12 13v4M8 21h8M9.5 17h5"/>',
    cal: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a1 1 0 0 1-1 1A16 16 0 0 1 4 5a1 1 0 0 1 1-1z"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    gauge: '<path d="M12 14l4-4"/><path d="M3.3 17a10 10 0 1 1 17.4 0"/>',
    mega: '<path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>',
    tag: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    cam: '<path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/>',
    chat: '<path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12z"/>',
    screen: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'
  };
  const ic = (n, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n] || P.tag}</svg>`;

  // Couleur et picto par source et par theme
  const SRC = {
    BOAMP:   { c: "#2563eb", i: "landmark", l: "BOAMP" },
    TED:     { c: "#7c3aed", i: "globe",    l: "TED (UE)" },
    PRESSE:  { c: "#ea580c", i: "news",     l: "Presse" },
    SITES:   { c: "#0d9488", i: "link",     l: "Site" },
    SIGNAUX: { c: "#db2777", i: "radar",    l: "Signal" }
  };
  const src = s => SRC[s] || { c: "#64748b", i: "tag", l: s };
  const THEME = {
    "360 / campagne":     { c: "#7c3aed", i: "mega" },
    "Marque / branding":  { c: "#c026d3", i: "tag" },
    "Video / photo":      { c: "#e11d48", i: "cam" },
    "Site / digital":     { c: "#0284c7", i: "screen" },
    "Social / influence": { c: "#16a34a", i: "chat" },
    "Pub / RP / event":   { c: "#d97706", i: "mic" },
    "Signal marque":      { c: "#db2777", i: "radar" },
    "Signal groupe":      { c: "#9333ea", i: "radar" }
  };
  const theme = t => THEME[t] || { c: "#64748b", i: "tag" };
  const listeThemes = a => (a.themes || "").split(" | ").filter(Boolean);
  const chip = t => `<span class="th" style="--c:${theme(t).c}">${ic(theme(t).i, "s")}${esc(t)}</span>`;

  // ---------- Favoris (memorises dans ce navigateur, d'un lancement a l'autre) ----------
  const CLE_FAV = "veille360:favoris";
  let favoris = new Set();
  try { favoris = new Set(JSON.parse(localStorage.getItem(CLE_FAV) || "[]")); } catch (e) { favoris = new Set(); }
  function sauverFavoris() {
    try { localStorage.setItem(CLE_FAV, JSON.stringify([...favoris])); } catch (e) { /* stockage indisponible : favoris valables pour cette session */ }
  }
  const etoile = a => `<button class="star" data-fav="${a._i}" aria-pressed="${a.fav ? "true" : "false"}" title="${a.fav ? "Retirer des favoris" : "Ajouter aux favoris"}" aria-label="${a.fav ? "Retirer des favoris" : "Ajouter aux favoris"}">${ic("star")}</button>`;

  DATA.forEach((a, i) => { a._i = i; a.detail = a.detail || []; a.fav = favoris.has(a.id); });
  const themes = new Set(), sources = new Set();
  DATA.forEach(a => { listeThemes(a).forEach(t => themes.add(t)); sources.add(a.source); });
  [...themes].sort().forEach(t => $("fTheme").insertAdjacentHTML("beforeend", `<option>${esc(t)}</option>`));
  [...sources].sort().forEach(s => $("fSource").insertAdjacentHTML("beforeend", `<option value="${esc(s)}">${esc(src(s).l)}</option>`));

  const ids = ["fScore", "fPub", "fEch", "fTheme", "fSource", "fSort", "fText", "fNew", "fFav"];
  const read = () => ({
    score: +$("fScore").value, pub: +$("fPub").value, ech: $("fEch").value,
    theme: $("fTheme").value, source: $("fSource").value, sort: $("fSort").value,
    text: $("fText").value, onlyNew: $("fNew").checked, onlyFav: $("fFav").checked
  });
  const hote = u => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return "la plateforme"; } };
  const pillClass = s => s >= 18 ? "p4" : s >= 12 ? "p3" : s >= 8 ? "p2" : "p1";
  const dateFr = d => d ? new Date(d + "T00:00:00").toLocaleDateString("fr-FR", { day: "numeric", month: "short" }) : "";

  function echeanceChip(a) {
    if (!a.echeance) {
      if ((a.source === "SIGNAUX" || a.source === "PRESSE") && a.publie) return `<span class="m">${ic("cal", "s")}Publié le ${dateFr(a.publie)}</span>`;
      return `<span class="m none">${ic("clock", "s")}Pas de date limite</span>`;
    }
    const d = daysBetween(today, a.echeance);
    const cls = d <= 3 ? "soon" : d <= 7 ? "mid" : "";
    const quand = d < 0 ? "clôturée" : d === 0 ? "aujourd’hui" : `J-${d}`;
    return `<span class="m ${cls}" title="Date limite : ${esc(a.echeance)}">${ic("clock", "s")}${dateFr(a.echeance)} · ${quand}</span>`;
  }

  function carte(a) {
    const s = src(a.source);
    const th = listeThemes(a);
    const metas = [echeanceChip(a)];
    if (a.montant) metas.push(`<span class="m">${ic("euro", "s")}${esc(a.montant)}</span>`);
    if (a.departement) metas.push(`<span class="m" title="Département">${ic("pin", "s")}${esc(a.departement.length > 14 ? a.departement.slice(0, 14) + "…" : a.departement)}</span>`);
    if ((a.lots || []).length > 1) metas.push(`<span class="m">${ic("layers", "s")}${a.lots.length} lots</span>`);
    let pri;
    if (a.source === "SIGNAUX") pri = `<a class="btn pri" href="${esc(a.url)}" target="_blank" rel="noopener">${ic("news", "s")}Article</a>`;
    else if (a.depot) pri = `<a class="btn pri" href="${esc(a.depot)}" target="_blank" rel="noopener" title="${esc(hote(a.depot))}">${ic("send", "s")}${a.source === "SITES" ? "Consultation" : "Déposer"}</a>`;
    else pri = `<a class="btn pri" href="${esc(a.url)}" target="_blank" rel="noopener">${ic("ext", "s")}Voir l’avis</a>`;
    const ext = (a.source !== "SIGNAUX" && a.depot && a.depot !== a.url)
      ? `<a class="btn ico" href="${esc(a.url)}" target="_blank" rel="noopener" title="Voir l’avis complet" aria-label="Voir l’avis complet">${ic("ext", "s")}</a>` : "";
    return `<article class="card${a.nouveau ? " new" : ""}" style="--c:${s.c}" data-open="${a._i}">
      <div class="top">
        <span class="src">${ic(s.i, "s")}${esc(s.l)}</span>
        ${(a.aussi || []).map(x => `<span class="plus" title="Aussi publié sur ${esc(x.source)}">+${esc(x.source)}</span>`).join("")}
        ${a.nouveau ? `<span class="newb">${ic("spark", "s")}NEW</span>` : ""}
        <span class="sp"></span>
        <span class="pill ${pillClass(a.score)}" title="Score de pertinence">${a.score}</span>
        ${etoile(a)}
      </div>
      <h3>${esc(a.titre)}</h3>
      <div class="who">${ic("building", "s")}<span>${esc(a.acheteur || "Acheteur non renseigné")}</span></div>
      <div class="meta">${metas.join("")}</div>
      ${th.length ? `<div class="themes">${th.slice(0, 3).map(chip).join("")}${th.length > 3 ? `<span class="plus">+${th.length - 3}</span>` : ""}</div>` : ""}
      <div class="foot">${pri}<button class="btn sec" data-open="${a._i}">${ic("info", "s")}Détails</button>${ext}</div>
    </article>`;
  }

  function render() {
    current = filtrer(DATA, read(), today);
    $("cards").innerHTML = current.map(carte).join("");
    $("empty").hidden = current.length > 0;
    $("count").textContent = `${current.length} annonce(s) sur ${DATA.length}`;
    const nbFav = DATA.filter(a => a.fav).length;
    $("fFavLbl").textContent = nbFav ? `Favoris seulement (${nbFav})` : "Favoris seulement";
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
    const item = (a, k, kcls = "") => `<li data-open="${a._i}"><span class="k ${kcls}">${k}</span><span class="t">${esc(a.titre)}</span></li>`;
    const liste = (arr, f, vide) => arr.length ? `<ul class="mini">${arr.map(f).join("")}</ul>` : `<p class="mini none">${vide}</p>`;
    $("synth").innerHTML = `
      <div class="box" style="--c:#7c3aed">
        <h2><span class="bi">${ic("spark")}</span>Nouvelles annonces ${nouv.length ? '<button data-act="new">Afficher</button>' : ""}</h2>
        <div class="big">${nouv.length}<small>repérées pour la première fois aujourd’hui</small></div>
      </div>
      <div class="box" style="--c:#ea580c">
        <h2><span class="bi">${ic("clock")}</span>Échéances cette semaine ${semaine.length ? '<button data-act="week">Afficher</button>' : ""}</h2>
        ${liste(semaine.slice(0, 5), a => { const d = daysBetween(today, a.echeance); return item(a, d === 0 ? "auj." : "J-" + d, d <= 3 ? "soon" : "mid"); }, "Aucune date limite dans les 7 jours.")}
      </div>
      <div class="box" style="--c:#16a34a">
        <h2><span class="bi">${ic("trophy")}</span>Top 5 des scores</h2>
        ${liste(top, a => item(a, `<span class="pill ${pillClass(a.score)}" style="height:22px;min-width:30px;font-size:12px">${a.score}</span>`), "Aucune annonce à ce niveau de pertinence.")}
      </div>`;
  }
  $("synth").addEventListener("click", e => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    if (b.dataset.act === "new") { $("fNew").checked = true; }
    if (b.dataset.act === "week") { $("fEch").value = "7"; $("fSort").value = "ech"; }
    render();
    $("cards").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // ---------- Fenetre de detail ----------
  function detailHTML(a) {
    const s = src(a.source);
    const L = (i, k) => `${ic(i, "s")}${k}`;
    const infos = [
      [L("pin", "Département"), a.departement],
      [L("grid", a.source === "SIGNAUX" ? "Source" : "Procédure"), a.procedure],
      [L("euro", "Montant estimé"), a.montant],
      [L("cal", "Publiée"), a.publie], [L("spark", "Repérée le"), a.vu_le],
      [L("clock", "Date limite"), a.echeance ? a.echeance + (daysBetween(today, a.echeance) >= 0 ? ` (J-${daysBetween(today, a.echeance)})` : "") : ""]
    ].filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")
      + ((a.aussi || []).length ? `<dt>${L("link", "Aussi publié sur")}</dt><dd>${a.aussi.map(x => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.source)}</a>`).join(", ")}</dd>` : "");
    const lignes = a.detail.map(([m, p, o]) => [o === "d" ? m + " (description)" : m, p, o === "d" ? "d" : ""]);
    if (a.bonus) lignes.push([a.source === "SITES" ? "consultation sur une page surveillée" : "annonce de compétition (presse)", a.bonus, "b"]);
    const max = Math.max(1, ...lignes.map(l => l[1]));
    const why = lignes.length
      ? lignes.map(([m, p, c]) => `<div class="why-row"><span>${esc(m)}</span><span class="gauge ${c}"><i style="width:${Math.round(p / max * 100)}%"></i></span><span>+${p}</span></div>`).join("")
      : `<p class="hint">Aucun mot-clé détaillé pour cette annonce.</p>`;
    const desc = a.description ? `<p class="desc">${esc(a.description.length > 700 ? a.description.slice(0, 700) + "…" : a.description)}</p>` : "";
    const c = a.contact || {};
    let actions = "", note = "";
    if (a.source === "SIGNAUX") {
      actions = `<a class="btn pri" href="${esc(a.url)}" target="_blank" rel="noopener">${ic("news", "s")}Lire l’article</a>`;
      note = `<p class="note">Signal d’actualité, pas un appel d’offres : une piste de prospection à creuser.</p>`;
    } else {
      if (a.depot) {
        actions += `<a class="btn pri" href="${esc(a.depot)}" target="_blank" rel="noopener">${ic("send", "s")}${a.source === "SITES" ? "Voir la consultation" : "Dossier et dépôt"} · ${esc(hote(a.depot))}</a>`;
        note = a.source === "SITES" ? "" : `<p class="note">Les questions sur le contenu se posent aussi sur cette plateforme.</p>`;
      } else if (a.source === "BOAMP" || a.source === "TED") {
        note = `<p class="note warn">Lien de dépôt non trouvé : cherchez « profil d’acheteur » ou « adresse de présentation » dans l’avis.</p>`;
      }
      if (!a.depot || a.depot !== a.url) actions += `<a class="btn sec" href="${esc(a.url)}" target="_blank" rel="noopener">${ic("ext", "s")}Voir l’avis complet</a>`;
    }
    const contact = (c.mail || c.tel || c.nom) ? `<div class="blk"><div class="blk-h">${ic("user")}Contact</div><dl>
        ${c.nom ? `<dt>${L("user", "Nom")}</dt><dd>${esc(c.nom)}</dd>` : ""}
        ${c.mail ? `<dt>${L("mail", "Mail")}</dt><dd><a href="mailto:${esc(c.mail)}">${esc(c.mail)}</a></dd>` : ""}
        ${c.tel ? `<dt>${L("phone", "Téléphone")}</dt><dd><a href="tel:${esc(c.tel.replace(/\s/g, ""))}">${esc(c.tel)}</a></dd>` : ""}
      </dl>${a.depot ? `<p class="hint">Pour les renseignements administratifs.</p>` : ""}</div>` : "";
    const lots = (a.lots || []).length ? `<div class="blk"><div class="blk-h">${ic("layers")}${a.lots.length} lot${a.lots.length > 1 ? "s" : ""}</div>
      ${a.lots.map(l => `<div class="lot"><b>Lot ${esc(l.num)}</b><span>${esc(l.titre)}</span><span>${l.max ? esc(l.max) + " max" : esc(l.estime || "")}</span></div>`).join("")}</div>` : "";
    const th = listeThemes(a);
    return `<h3 id="dlgTitle">${esc(a.titre)}</h3>
      <p class="who2">${ic("building", "s")}${esc(a.acheteur || "Acheteur non renseigné")}</p>
      <div class="acts2">${actions}</div>${note}
      ${th.length ? `<div class="themes" style="margin-bottom:14px">${th.map(chip).join("")}</div>` : ""}
      ${desc}${infos ? `<dl>${infos}</dl>` : ""}${lots}${contact}
      <div class="blk"><div class="blk-h">${ic("gauge")}Pourquoi ce score <span class="pill ${pillClass(a.score)}">${a.score}</span></div>${why}</div>`;
  }
  function ouvrir(i) {
    const a = DATA[i];
    if (!a) return;
    ouvert = i;
    const s = src(a.source);
    $("dlgTop").style.setProperty("--c", s.c);
    $("dlgTop").innerHTML = `<span class="src" style="--c:${s.c}">${ic(s.i, "s")}${esc(s.l)}</span>
      ${a.nouveau ? `<span class="newb">${ic("spark", "s")}NEW</span>` : ""}<span class="sp"></span>${etoile(a)}
      <button class="x" id="dlgClose" aria-label="Fermer">${ic("x")}</button>`;
    $("dlgBody").style.setProperty("--c", s.c);
    $("dlgBody").innerHTML = detailHTML(a);
    $("dlgBody").scrollTop = 0;
    const d = $("dlg");
    if (typeof d.showModal === "function") { if (!d.open) d.showModal(); } else d.setAttribute("open", "");
  }
  function fermer() { const d = $("dlg"); if (d.open) { typeof d.close === "function" ? d.close() : d.removeAttribute("open"); } }
  $("dlg").addEventListener("close", () => { ouvert = -1; });
  // clic sur le fond assombri : ferme
  $("dlg").addEventListener("click", e => { if (e.target === $("dlg")) fermer(); });

  document.addEventListener("click", e => {
    // favoris (cartes et fenetre)
    const f = e.target.closest("button[data-fav]");
    if (f) {
      const dansFenetre = $("dlg").contains(f);
      const a = DATA[+f.dataset.fav];
      a.fav = !a.fav;
      a.fav ? favoris.add(a.id) : favoris.delete(a.id);
      sauverFavoris();
      render();
      if (dansFenetre) f.outerHTML = etoile(a);
      return;
    }
    if (e.target.closest("#dlgClose")) { fermer(); return; }
    // ouverture du detail : bouton Details, clic sur la carte (hors liens) ou sur une ligne du bandeau
    const o = e.target.closest("[data-open]");
    if (!o || e.target.closest("a") || (e.target.closest("button") && !e.target.closest("button[data-open]"))) return;
    if ($("dlg").contains(o)) return;
    ouvrir(+o.dataset.open);
  });

  ids.forEach(id => $(id).addEventListener("input", render));
  $("reset").addEventListener("click", () => {
    $("fScore").value = "5"; $("fPub").value = "0"; $("fEch").value = "all"; $("fTheme").value = "";
    $("fSource").value = ""; $("fSort").value = "score"; $("fText").value = ""; $("fNew").checked = false; $("fFav").checked = false; render();
  });
  $("csv").addEventListener("click", () => {
    const cols = ["fav", "nouveau", "score", "explication", "themes", "source", "titre", "acheteur", "departement", "procedure", "montant", "publie", "echeance", "url", "depot", "contact_mail", "aussi_sur"];
    const q = v => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
    current.forEach(r => {
      r.explication = r.detail.map(([m, p, o]) => `${m} (+${p}${o === "d" ? ", description" : ""})`).concat(r.bonus ? [`bonus (+${r.bonus})`] : []).join(", ");
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
    p.add_argument("--nouveaux", action="store_true", help="ne garder que les annonces reperees pour la premiere fois aujourd hui")
    args = p.parse_args()

    toutes = []
    for nom, fn in [("BOAMP", source_boamp), ("TED", source_ted), ("PRESSE", source_presse),
                    ("SITES", source_sites), ("SIGNAUX", source_signaux)]:
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
    # Liens de sites sans date : on ne les garde que pendant la periode de veille
    seuil = (date.today() - timedelta(days=args.jours)).isoformat()
    retenues = [a for a in retenues if not (a["source"] == "SITES" and not a["publie"]
                                            and not a["echeance"] and a["vu_le"] < seuil)]
    for w in AVERTISSEMENTS:
        SOURCES_STATUS.append(w)
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
