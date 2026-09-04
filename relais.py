#!/usr/bin/env python3
"""Relais de données pour SuiviMarché.

Produit des fichiers JSON statiques, lus par index.html depuis raw.githubusercontent.com :
- spot.json : prix spot day-ahead France (energy-charts.info, licence CC BY 4.0) agrégés par mois
  et par jour, avec le prix « capté » par le solaire (moyenne pondérée par la production solaire
  nationale) et les heures à prix négatif ;
- jorf.json : textes du Journal officiel (open data DILA, licence ouverte) dont le titre concerne
  l'électricité solaire ou renouvelable ;
- etat.json : état du relais (dernière archive JORF traitée, horodatages).

Aucune clé d'API. Bibliothèque standard uniquement (Python ≥ 3.9).

Variables d'environnement (facultatives) :
  RELAIS_FULL=1          recalcul complet du spot et relecture de toutes les archives JORF
  RELAIS_SPOT_DEBUT      premier mois du spot (AAAA-MM, défaut 2015-01)
  RELAIS_JORF_JOURS      profondeur initiale des archives JORF en jours (défaut 400)
"""
import datetime as dt
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

PARIS = ZoneInfo('Europe/Paris')
OUT = os.path.dirname(os.path.abspath(__file__))
EC = 'https://api.energy-charts.info'
DILA = 'https://echanges.dila.gouv.fr/OPENDATA/JORF/'
FULL = os.environ.get('RELAIS_FULL') == '1' or '--full' in sys.argv
SPOT_DEBUT = os.environ.get('RELAIS_SPOT_DEBUT', '2015-01')
JORF_JOURS = int(os.environ.get('RELAIS_JORF_JOURS', '400'))
JORF_CONSERVE_JOURS = 730
JOURS_DETAIL = 92
H_SOLAIRE = range(11, 15)  # fenêtre « heures solaires » : 11 h à 15 h, heure de Paris

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # noqa: BLE001
    pass
now = dt.datetime.now(dt.timezone.utc)
today = now.astimezone(PARIS).date()


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def get(url, retries=3, timeout=240):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'suivimarche-relais/1.0 (github.com/Cocothib/suivimarche-data)'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            log('  nouvel essai après erreur :', e)
            time.sleep(8 * (i + 1))


def charge_json(nom, defaut):
    p = os.path.join(OUT, nom)
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return defaut


def ecrit_json(nom, data):
    p = os.path.join(OUT, nom)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    log('écrit', nom, os.path.getsize(p) // 1024, 'Ko')


def add_months(s, n):
    y, m = map(int, s.split('-'))
    m0 = y * 12 + (m - 1) + n
    return f'{m0 // 12:04d}-{m0 % 12 + 1:02d}'


def r(v, nd=2):
    return None if v is None else round(v, nd)


# ------------------------------------------------------------------ prix spot
def spot():
    ancien = charge_json('spot.json', {})
    mois = {} if FULL else dict(ancien.get('mois', {}))
    ym_now = today.strftime('%Y-%m')
    fin = ym_now
    # mois à (re)calculer : les mois manquants depuis SPOT_DEBUT, plus le mois courant et le précédent
    a_faire = []
    m = SPOT_DEBUT
    while m <= fin:
        if m not in mois or m >= add_months(ym_now, -1):
            a_faire.append(m)
        m = add_months(m, 1)
    if not a_faire:
        log('spot : rien à faire')
        return
    # période de chargement : du premier mois à calculer jusqu'à aujourd'hui, découpée par année
    d0 = dt.date(int(a_faire[0][:4]), int(a_faire[0][5:]), 1)
    # les 92 derniers jours sont toujours rechargés pour la série journalière
    d_detail = today - dt.timedelta(days=JOURS_DETAIL)
    d0 = min(d0, d_detail)
    log(f'spot : {len(a_faire)} mois à calculer, chargement depuis {d0}')
    prix, solaire = {}, {}
    d = d0
    while d <= today:
        d1 = min(dt.date(d.year, 12, 31), today)
        log(f'  energy-charts {d} → {d1}')
        j = json.loads(get(f'{EC}/price?bzn=FR&start={d}&end={d1}'))
        for t, p in zip(j.get('unix_seconds', []), j.get('price', [])):
            if p is not None:
                prix[t] = p
        try:
            j = json.loads(get(f'{EC}/public_power?country=fr&start={d}&end={d1}'))
            sol = next((pt['data'] for pt in j.get('production_types', []) if pt['name'] == 'Solar'), [])
            for t, v in zip(j.get('unix_seconds', []), sol):
                if v is not None:
                    solaire[t] = v
        except Exception as e:  # noqa: BLE001
            log('  production solaire indisponible :', e)
        d = dt.date(d.year + 1, 1, 1)
        time.sleep(1)
    ts = sorted(prix)
    if not ts:
        log('spot : aucune donnée reçue')
        return
    # pas de temps de chaque point (1 h avant octobre 2025, 15 min ensuite)
    pas = {}
    for i, t in enumerate(ts):
        pas[t] = (ts[i + 1] - t) if i + 1 < len(ts) else (ts[i] - ts[i - 1] if i else 3600)
        if pas[t] <= 0 or pas[t] > 3600:
            pas[t] = 3600
    # prix associé à chaque point de production (aligné sur l'heure si le prix est horaire)
    def prix_de(t):
        if t in prix:
            return prix[t]
        return prix.get(t - t % 3600)

    agg_m, agg_j = {}, {}
    for t in ts:
        loc = dt.datetime.fromtimestamp(t, PARIS)
        km, kj, h = loc.strftime('%Y-%m'), loc.strftime('%Y-%m-%d'), loc.hour
        p, w = prix[t], pas[t] / 3600
        for key, agg in ((km, agg_m), (kj, agg_j)):
            a = agg.setdefault(key, {'sp': 0.0, 'sw': 0.0, 'ssp': 0.0, 'ssw': 0.0, 'neg': 0.0, 'min': p, 'max': p, 'prof': [[0.0, 0.0] for _ in range(24)]})
            a['sp'] += p * w
            a['sw'] += w
            if h in H_SOLAIRE:
                a['ssp'] += p * w
                a['ssw'] += w
            if p < 0:
                a['neg'] += w
            a['min'] = min(a['min'], p)
            a['max'] = max(a['max'], p)
            a['prof'][h][0] += p * w
            a['prof'][h][1] += w
    cap_m, cap_j = {}, {}
    for t, v in solaire.items():
        p = prix_de(t)
        if p is None or v <= 0:
            continue
        loc = dt.datetime.fromtimestamp(t, PARIS)
        for key, cap in ((loc.strftime('%Y-%m'), cap_m), (loc.strftime('%Y-%m-%d'), cap_j)):
            c = cap.setdefault(key, [0.0, 0.0])
            c[0] += p * v
            c[1] += v

    def sortie(a, cap, prof):
        o = {'p': r(a['sp'] / a['sw']), 'ps': r(a['ssp'] / a['ssw']) if a['ssw'] else None,
             'pc': r(cap[0] / cap[1]) if cap and cap[1] else None, 'hneg': r(a['neg'], 1),
             'min': r(a['min']), 'max': r(a['max']), 'h': r(a['sw'], 1)}
        if prof:
            o['prof'] = [r(s / w, 1) if w else None for s, w in a['prof']]
        return o

    for km in a_faire:
        if km in agg_m and agg_m[km]['sw'] >= 24:
            mois[km] = sortie(agg_m[km], cap_m.get(km), True)
    jours = {}
    for kj in sorted(agg_j):
        if kj >= d_detail.isoformat():
            jours[kj] = sortie(agg_j[kj], cap_j.get(kj), False)
    out = {'maj': now.isoformat(timespec='seconds'), 'zone': 'FR', 'unite': '€/MWh',
           'source': 'energy-charts.info (ENTSO-E / EPEX), CC BY 4.0',
           'champs': {'p': 'prix moyen', 'ps': 'prix moyen 11 h-15 h (heure de Paris)', 'pc': 'prix capté solaire (pondéré par la production solaire France)',
                      'hneg': 'heures à prix négatif', 'min': 'minimum', 'max': 'maximum', 'h': 'heures couvertes', 'prof': 'prix moyen par heure de la journée (0-23 h)'},
           'mois': dict(sorted(mois.items())), 'jours': jours}
    ecrit_json('spot.json', out)
    return out


# ---------------------------------------------------------------------- JORF
KW_PV = ['photovolta', 'solaire', 'radiative du soleil', 'agrivolta', 'autoconsommation']
KW_ENERGIE = ['énergie renouvelable', 'énergies renouvelables', "obligation d'achat", 'complément de rémunération',
              'raccordement', 'réseau public de distribution', 'réseau public de transport', "tarif d'utilisation",
              'programmation pluriannuelle', "appel d'offres", "production d'électricité", 'installations de production',
              'électricité', 'commission de régulation', 'stockage', 'contrat de vente']
EXCLUS = ['nomination', 'cessation de fonctions', 'délégation de signature', 'tableau d\'avancement', 'vacance', 'avis de concours',
          'admission à la retraite', 'mise en disponibilité', 'inscription au tableau']


def norm(s):
    return (s or '').replace('’', "'").replace(' ', ' ').lower()


def classe(titre):
    t = norm(titre)
    if any(x in t for x in EXCLUS):
        return None, []
    mots = [k for k in KW_PV if k in t]
    if mots:
        return 'pv', mots
    mots = [k for k in KW_ENERGIE if norm(k) in t]
    if mots:
        return 'energie', mots
    return None, []


def texte(el, chemin):
    x = el.find(chemin)
    return (x.text or '').strip() if x is not None and x.text else ''


def jorf():
    etat = charge_json('etat.json', {})
    ancien = charge_json('jorf.json', {})
    textes = {} if FULL else {t['id']: t for t in ancien.get('textes', [])}
    dernier = '' if FULL else etat.get('jorf_dernier', '')
    html = get(DILA).decode('utf-8', 'replace')
    noms = sorted(set(re.findall(r'JORF_\d{8}-\d{6}\.tar\.gz', html)))
    if dernier:
        noms = [n for n in noms if n > dernier]
    else:
        seuil = (today - dt.timedelta(days=JORF_JOURS)).strftime('JORF_%Y%m%d')
        noms = [n for n in noms if n >= seuil]
    log(f'jorf : {len(noms)} archive(s) à lire')
    lus, retenus = 0, 0
    for nom in noms:
        try:
            data = get(DILA + nom)
        except Exception as e:  # noqa: BLE001
            log('  archive illisible', nom, e)
            break
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
                for mb in tar:
                    if not mb.isfile() or '/texte/version/' not in mb.name or not mb.name.endswith('.xml'):
                        continue
                    lus += 1
                    try:
                        root = ET.fromstring(tar.extractfile(mb).read())
                    except ET.ParseError:
                        continue
                    titre = texte(root, './/META_TEXTE_VERSION/TITREFULL') or texte(root, './/META_TEXTE_VERSION/TITRE')
                    tag, mots = classe(titre)
                    if not tag:
                        continue
                    tid = texte(root, './/META_COMMUN/ID')
                    if not tid:
                        continue
                    retenus += 1
                    textes[tid] = {'id': tid, 'd': texte(root, './/META_TEXTE_CHRONICLE/DATE_PUBLI'), 'dt': texte(root, './/META_TEXTE_CHRONICLE/DATE_TEXTE'),
                                   'n': texte(root, './/META_COMMUN/NATURE'), 'nor': texte(root, './/META_TEXTE_CHRONICLE/NOR'),
                                   'jo': texte(root, './/META_TEXTE_CHRONICLE/ORIGINE_PUBLI'), 't': titre,
                                   'm': texte(root, './/META_TEXTE_VERSION/MINISTERE'), 'tag': tag, 'mots': mots}
        except tarfile.TarError as e:
            log('  archive corrompue', nom, e)
        dernier = nom
    seuil = (today - dt.timedelta(days=JORF_CONSERVE_JOURS)).isoformat()
    liste = sorted((t for t in textes.values() if t.get('d', '') >= seuil), key=lambda t: (t['d'], t['id']), reverse=True)
    log(f'jorf : {lus} textes lus, {retenus} retenus dans cette passe, {len(liste)} conservés')
    ecrit_json('jorf.json', {'maj': now.isoformat(timespec='seconds'), 'source': 'DILA, open data JORF (echanges.dila.gouv.fr), licence ouverte',
                             'depuis': seuil, 'n': len(liste), 'textes': liste})
    etat['jorf_dernier'] = dernier
    etat['jorf_maj'] = now.isoformat(timespec='seconds')
    return etat


if __name__ == '__main__':
    etat = charge_json('etat.json', {})
    erreurs = []
    try:
        if spot():
            etat['spot_maj'] = now.isoformat(timespec='seconds')
    except Exception as e:  # noqa: BLE001
        log('ERREUR spot :', e)
        erreurs.append('spot : ' + str(e))
    try:
        etat.update(jorf())
    except Exception as e:  # noqa: BLE001
        log('ERREUR jorf :', e)
        erreurs.append('jorf : ' + str(e))
    etat['maj'] = now.isoformat(timespec='seconds')
    etat['erreurs'] = erreurs
    ecrit_json('etat.json', etat)
    sys.exit(1 if len(erreurs) == 2 else 0)
