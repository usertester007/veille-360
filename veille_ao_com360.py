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
        "query": (f"classification-cpv IN (79340000 79341000 79341100 79341200 79341400 79342000 79822500 92111200 72413000 79416000) "
                  f"AND buyer-country=FRA AND publication-date>={depuis}"),
        "fields": ["publication-number", "notice-title", "buyer-name",
                   "publication-date", "deadline-receipt-tender-date-lot", "links"],
        "limit": 100, "page": 1, "scope": "ALL",
    }
    r = requests.post(TED_URL, json=corps, headers=HEADERS, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} - {r.text[:300]}")
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
            if not any(m in nt for m in MOTS_EXCLUS):
                a["score"] += 6  # une annonce de competition/pitch est deja pertinente en soi
            out[a["id"]] = a
    if erreurs == len(flux_urls):
        raise RuntimeError("aucun flux presse accessible")
    return list(out.values())


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


SOURCES_STATUS = []
COLS = ["nouveau", "score", "themes", "source", "titre", "acheteur", "publie", "echeance", "url"]


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
</style>
</head>
<body>
<main>
  <h1>Consultations agences 360</h1>
  <p class="sub">Mise à jour du __DATE__ &middot; __STATUS__</p>

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

  <div class="wrap">
    <table>
      <thead><tr><th></th><th>Score</th><th>Thèmes</th><th>Source</th><th>Objet</th><th>Acheteur</th><th>Publiée</th><th>Date limite</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div id="empty" class="empty" hidden>Aucune annonce ne correspond à ces filtres.</div>
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
    if (f.source && a.source !== f.source) return false;
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
      return `<tr class="${a.nouveau ? "new" : ""}">
        <td>${a.nouveau ? '<span class="tag">NEW</span>' : ""}</td>
        <td><span class="pill ${pillClass(a.score)}">${a.score}</span></td>
        <td>${tags}</td><td><span class="tag">${esc(a.source)}</span></td>
        <td><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.titre)}</a></td>
        <td>${esc(a.acheteur)}</td><td>${esc(a.publie)}</td><td class="${cls}">${ech}</td></tr>`;
    }).join("");
    $("empty").hidden = current.length > 0;
    $("count").textContent = `${current.length} annonce(s) sur ${DATA.length}`;
  }

  ids.forEach(id => $(id).addEventListener("input", render));
  $("reset").addEventListener("click", () => {
    $("fScore").value = "5"; $("fPub").value = "0"; $("fEch").value = "all"; $("fTheme").value = "";
    $("fSource").value = ""; $("fSort").value = "score"; $("fText").value = ""; $("fNew").checked = false; render();
  });
  $("csv").addEventListener("click", () => {
    const cols = ["nouveau", "score", "themes", "source", "titre", "acheteur", "publie", "echeance", "url"];
    const q = v => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
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
    with open("veille_com360.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(annonces)

    import json
    data = [{k: x.get(k, "") for k in ("nouveau", "score", "themes", "source", "titre",
                                       "acheteur", "publie", "echeance", "url")}
            for x in annonces]
    donnees = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = (HTML_TEMPLATE
            .replace("__DATE__", f"{date.today():%d/%m/%Y}")
            .replace("__STATUS__", html.escape("  |  ".join(SOURCES_STATUS)))
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
