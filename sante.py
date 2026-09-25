"""État des sources de SuiviMarché, vu depuis GitHub Actions (workflow « sante », chaque matin).

Une requête minimale par API interrogée par le navigateur (mêmes adresses que les valeurs par défaut de
Paramètres › Sources dans index.html) plus les sources propres au relais (energy-charts, DILA, BDNB).
Écrit sante.json {maj, n, n_ko, res: {clé: {ok, code, ms, msg}}} lu par l'application (Paramètres › Sources,
colonne « Vu du relais »). Sort en erreur (exit 1) quand une source essentielle est en défaut, ou une source secondaire
deux matins de suite (une panne d'un jour, fréquente sur Géorisques ou Overpass vus de GitHub, ne déclenche rien) :
GitHub envoie alors un courriel « Run failed » au propriétaire du dépôt, sans qu'il soit besoin d'ouvrir l'application.
Stdlib seulement, aucune clé.
"""
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import sys
import time
import urllib.error
import urllib.request

UA = 'SuiviMarche-sante/1.0 (+https://github.com/Cocothib/suivimarche-data)'
DELAI = 25
ODS_ODRE = 'https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/'
ODS_ENEDIS = 'https://opendata.enedis.fr/api/explore/v2.1/catalog/datasets/'
BDNB_MILLESIME = None  # lu dans bdnb.json (_meta.millesime_serveur) sinon repli

SONDES = [
    # clé (= clé de Paramètres › Sources dans l'application), libellé, adresse, type, essentielle
    ('odre', 'Registre national des installations (ODRÉ)', ODS_ODRE + 'registre-national-installation-production-stockage-electricite-agrege/records?limit=1', 'json', True),
    ('enedis', 'Open data Enedis', ODS_ENEDIS + 'parc-des-installations-de-production-raccordees-par-departement/records?limit=1', 'json', True),
    ('eco', 'éCO2mix régional (RTE)', ODS_ODRE + 'eco2mix-regional-cons-def/records?limit=1', 'json', False),
    ('parcReg', 'Parc régional annuel (RTE)', ODS_ODRE + 'parc-regional-annuel-prod-eolien-solaire/records?limit=1', 'json', False),
    ('dido', 'DiDo / Sitadel (SDES)', 'https://data.statistiques.developpement-durable.gouv.fr/dido/api/v1/datafiles/f8f0700f-806c-40a7-83b1-f21cf507e7c4', 'json', False),
    ('entApi', 'Recherche d’entreprises (Sirene)', 'https://recherche-entreprises.api.gouv.fr/search?q=agriwatt&per_page=1', 'json', False),
    ('begesApi', 'Bilans GES (ADEME)', 'https://data.ademe.fr/data-fair/api/v1/datasets/bilan-ges/lines?size=1', 'json', False),
    ('rgeApi', 'Entreprises RGE (ADEME)', 'https://data.ademe.fr/data-fair/api/v1/datasets/liste-des-entreprises-rge-2/lines?size=1', 'json', False),
    ('aidesApi', 'Aides de l’ADEME', 'https://data.ademe.fr/data-fair/api/v1/datasets/les-aides-financieres-de-l%27ademe/lines?size=1', 'json', False),
    ('annuaireApi', 'Annuaire de l’administration', 'https://api-lannuaire.service-public.fr/api/explore/v2.1/catalog/datasets/api-lannuaire-administration/records?limit=1', 'json', False),
    ('educApi', 'Annuaire de l’éducation', 'https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/fr-en-annuaire-education/records?limit=1', 'json', False),
    ('bodaccApi', 'BODACC', 'https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records?limit=1', 'json', False),
    ('boampApi', 'BOAMP', 'https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records?limit=1', 'json', False),
    ('decpApi', 'DECP', 'https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/decp-2022-marches-valides/records?limit=1', 'json', False),
    ('dataEsApi', 'Équipements sportifs (Data ES)', 'https://equipements.sports.gouv.fr/api/explore/v2.1/catalog/datasets/data-es/records?limit=1', 'json', False),
    ('wikidata', 'Wikidata', 'https://www.wikidata.org/w/api.php?action=wbsearchentities&search=Agriwatt&language=fr&format=json&limit=1', 'json', False),
    ('overpass', 'Overpass (OpenStreetMap)', 'https://overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A10%5D%3Bnode%281%29%3Bout%3B', 'json', False),
    ('overpass2', 'Overpass, serveur z (overpass-api.de)', 'https://z.overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A10%5D%3Bnode%281%29%3Bout%3B', 'json', False),
    ('overpass3', 'Overpass, serveur lz4 (overpass-api.de)', 'https://lz4.overpass-api.de/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A10%5D%3Bnode%281%29%3Bout%3B', 'json', False),
    ('overpass4', 'Overpass, miroir de secours (maps.mail.ru)', 'https://maps.mail.ru/osm/tools/overpass/api/interpreter?data=%5Bout%3Ajson%5D%5Btimeout%3A10%5D%3Bnode%281%29%3Bout%3B', 'json', False),
    ('ign', 'Photos aériennes IGN (Géoplateforme)', 'https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX=6&TILEROW=22&TILECOL=32&FORMAT=image%2Fjpeg', 'get', False),
    ('bdnbS3', 'Exports BDNB (CSTB)', 'https://open-data.s3.fr-par.scw.cloud/bdnb_millesime_{m}/millesime_{m}_dep90/open_data_millesime_{m}_dep90_csv.zip', 'head', False),
    # sources propres au relais (relais.py, prospects_bdnb.py)
    ('spot', 'Prix spot (energy-charts)', 'https://api.energy-charts.info/price?bzn=FR&start={hier}&end={hier}', 'json', False),
    ('dila', 'Journal officiel (open data DILA)', 'https://echanges.dila.gouv.fr/OPENDATA/JORF/', 'get', False),
    ('geoapi', 'Contours des communes (geo.api.gouv.fr)', 'https://geo.api.gouv.fr/departements/59/communes?fields=code&format=json', 'json', False),
    ('georisques', 'Installations classées (Géorisques)', 'https://georisques.gouv.fr/api/v1/installations_classees?departement=59&page_size=1&page=1', 'json', False),
    ('datagouv', 'data.gouv.fr (friches, IREP, FINESS, élus)', 'https://www.data.gouv.fr/api/1/datasets/?q=finess&page_size=1', 'json', False),
    ('agencebio', 'Agence Bio (annuaire des opérateurs)', 'https://opendata.agencebio.org/api/gouv/operateurs/?departement=59&nb=1', 'json', False),
]


def millesime():
    try:
        with open('bdnb.json', encoding='utf-8') as f:
            meta = json.load(f).get('_meta', {})
        return meta.get('millesime_serveur') or meta.get('millesime') or '2026-02-a'
    except Exception:  # noqa: BLE001
        return '2026-02-a'


def sonder(s):
    k, lbl, url, kind, vital = s
    hier = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    url = url.format(m=millesime(), hier=hier)
    r = {'lbl': lbl, 'url': url, 'ok': False, 'code': None, 'ms': 0, 'msg': '', 'vital': vital}
    t0 = time.time()
    req = urllib.request.Request(url, method='HEAD' if kind == 'head' else 'GET', headers={'User-Agent': UA, 'Accept': 'application/json, */*'})
    try:
        with urllib.request.urlopen(req, timeout=DELAI) as resp:
            r['code'] = resp.status
            if kind == 'json':
                brut = resp.read(400000)
                if (resp.headers.get('Content-Encoding') or '').lower() == 'gzip' or brut[:2] == bytes([0x1f, 0x8b]):
                    brut = gzip.decompress(brut)   # certains portails opendatasoft compressent sans qu'on le demande
                j = json.loads(brut.decode('utf-8', 'replace'))
                if isinstance(j, dict) and (j.get('error_code') or (isinstance(j.get('error'), str) and j.get('error'))):
                    raise ValueError(str(j.get('message') or j.get('error'))[:120])
            else:
                resp.read(2000)
            r['ok'] = True
            r['msg'] = 'HTTP %d' % resp.status
    except urllib.error.HTTPError as e:
        r['code'] = e.code
        if e.code == 429:
            r['ok'] = 'partiel'
            r['msg'] = 'quota de requêtes atteint (HTTP 429)'
        else:
            corps = ''
            try:
                corps = e.read(300).decode('utf-8', 'replace').strip()
            except Exception:  # noqa: BLE001
                pass
            if corps.startswith('<'):
                corps = 'page HTML'
            r['msg'] = 'HTTP %d%s' % (e.code, (' : ' + corps[:120]) if corps else '')
    except Exception as e:  # noqa: BLE001
        r['msg'] = ('délai dépassé (%d s)' % DELAI) if 'timed out' in str(e) else str(e)[:160]
    r['ms'] = int((time.time() - t0) * 1000)
    return k, r


if __name__ == '__main__':
    now = dt.datetime.now(dt.timezone.utc)
    try:
        with open('sante.json', encoding='utf-8') as f:
            avant = json.load(f).get('res') or {}
    except Exception:  # noqa: BLE001
        avant = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = dict(ex.map(sonder, SONDES))
    # second essai 20 s plus tard pour les sources en défaut : un 504 passager ne doit pas déclencher un courriel
    encore = [s for s in SONDES if res[s[0]]['ok'] is False]
    if encore:
        time.sleep(20)
        for k, r in map(sonder, encore):
            r['essais'] = 2
            res[k] = r
    # les serveurs Overpass se compensent (504 aléatoires, l'appli bascule seule) : OK dès que l'un répond, erreur des autres notée ; défaut de tous = en défaut
    ovp = [k for k in res if k.startswith('overpass')]
    if any(res[k]['ok'] is True for k in ovp):
        for k in ovp:
            if res[k]['ok'] is not True:
                res[k]['ok'] = True
                res[k]['msg'] = 'indisponible à cet instant, un autre serveur Overpass répond : ' + res[k]['msg']
    # jours de défaut consécutifs (lus dans le sante.json de la veille)
    for k, r in res.items():
        if r['ok'] is False:
            r['ko_jours'] = int((avant.get(k) or {}).get('ko_jours') or (1 if (avant.get(k) or {}).get('ok') is False else 0)) + 1
    ko = [k for k, r in res.items() if r['ok'] is not True]
    for k, r in res.items():
        print('%-12s %-8s %6d ms  %s' % (k, 'OK' if r['ok'] is True else ('PARTIEL' if r['ok'] == 'partiel' else 'KO'), r['ms'], r['msg']))
    out = {'maj': now.isoformat(timespec='seconds'), 'n': len(res), 'n_ko': len([k for k in ko if res[k]['ok'] is False]), 'n_partiel': len([k for k in ko if res[k]['ok'] == 'partiel']), 'res': res}
    with open('sante.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('sante.json : %d sources, %d en défaut, %d à surveiller' % (out['n'], out['n_ko'], out['n_partiel']))
    # exit 1 : GitHub avertit le propriétaire du dépôt (« Run failed ») ; un simple 429 ne compte pas, une source secondaire seulement au 2e jour
    alerte = [k for k, r in res.items() if r['ok'] is False and (r['vital'] or r.get('ko_jours', 1) >= 2)]
    if alerte:
        print('ALERTE : ' + ', '.join('%s (%d j)' % (k, res[k].get('ko_jours', 1)) for k in alerte))
    sys.exit(1 if alerte else 0)
