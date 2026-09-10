# -*- coding: utf-8 -*-
"""
prospects_bdnb.py — SuiviMarché : liste de prospects toiture PV à partir de l'export
départemental de la BDNB (CSTB, data.gouv.fr) et, si présent, du CSV Sitadel du département.

Usage :  python prospects_bdnb.py                -> traite tous les zips BDNB « *dep<XX>_csv.zip » du dossier du script
         python prospects_bdnb.py 59 75          -> seulement ces départements
         python prospects_bdnb.py chemin.zip [sitadel.csv]
Options : --telecharger        télécharge les zips manquants (data.gouv.fr / CSTB, 0,3 à 1,5 Go par département)
          --millesime 2026-02-a millésime BDNB à télécharger (défaut : BDNB_MILLESIME ; « auto » = le plus récent publié sur le serveur)
          --detecter           affiche le millésime le plus récent disponible sur le serveur et s'arrête
          --relais bdnb.json    ajoute le résumé du département au fichier JSON du relais SuiviMarché (créé si absent)
          --sans-excel          ne produit pas prospects_<dep>.xlsx (mode relais)
          --nettoyer            supprime le zip après traitement (mode relais, disque limité)
          --dossier chemin      dossier des zips (défaut : dossier du script)
Le CSV Sitadel de chaque département est reconnu par le contenu de sa colonne DEP_CODE (le nom du fichier
téléchargé n'a pas d'importance). Sortie : prospects_<dep>.xlsx à côté du zip
(feuilles Prospects, Propriétaires, Synthèse communes, Permis Sitadel, Méthode).
Ce fichier est identique dans le dépôt SuiviMarché (privé) et dans suivimarche-data (public, exécuté par
le workflow « bdnb » de GitHub Actions) : modifier les deux ensemble.

Hypothèses (modifiables ci-dessous) : part de toiture exploitable, rendement surfacique, productible,
seuils d'emprise au sol et de score. Aucune donnée n'est envoyée sur internet.
"""
import sys, os, re, io, glob, zipfile, datetime, json, argparse, urllib.request, urllib.parse, shutil
import pandas as pd
try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception: pass

# ---------------- paramètres ----------------
EMPRISE_MIN = 400          # m² d'emprise au sol minimale pour retenir un bâtiment
PART_TOITURE = 0.45        # part de l'emprise réellement équipable (édicules, ombres, sécurité, pente)
M2_PAR_KWC = 5.5           # m² de toiture par kWc (modules ~ 200 Wc/m² posés, avec espacements)
PRODUCTIBLE = 1050         # kWh par kWc et par an (moitié nord ; Paris ≈ 1 000-1 100)
KWC_MIN = 36               # on ne retient que les toitures permettant > 36 kWc
USAGES_PRO = ('tertiaire', 'primaire', 'industr', 'agric', 'commerc', 'sport', 'entrep', 'logist', 'bureau', 'service', 'religieux', 'enseign', 'sant')
USAGES_EXCLUS = ('résidentiel', 'residentiel', 'dépendance', 'dependance', 'secondaire', 'annexe', 'indifférencié', 'indifferencie')
BDNB_MILLESIME = '2026-02-a'
BDNB_URL = 'https://open-data.s3.fr-par.scw.cloud/bdnb_millesime_{m}/millesime_{m}_dep{d}/open_data_millesime_{m}_dep{d}_csv.zip'
SECTEURS = (('agricole', ('agric', 'primaire', 'élevage', 'elevage', 'serre')), ('industrie', ('industr', 'entrep', 'logist', 'usine', 'atelier')), ('tertiaire', ('tertiaire', 'commerc', 'bureau', 'service', 'sport', 'enseign', 'sant', 'religieux')))

def secteur(usage):
    u = str(usage or '').lower()
    for nom, mots in SECTEURS:
        if any(m in u for m in mots): return nom
    return 'autre'

def existe(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=30) as r:
            return r.status == 200
    except Exception:
        return False

def detecter_millesime(base=None, dep='90', horizon=2):
    """millésime BDNB le plus récent publié sur le serveur (dossiers bdnb_millesime_AAAA-MM-x), en sondant
    mois par mois à partir du millésime connu jusqu'à aujourd'hui + horizon mois (petit département = requête HEAD légère)"""
    base = BDNB_MILLESIME if not base or base == 'auto' else base
    y, mo = int(base[:4]), int(base[5:7]); trouve = base
    auj = datetime.date.today(); fy, fm = auj.year, auj.month + horizon
    while fm > 12: fm -= 12; fy += 1
    while (y, mo) <= (fy, fm):
        for lettre in 'abc':
            m = f'{y:04d}-{mo:02d}-{lettre}'
            if m != base and existe(BDNB_URL.format(m=m, d=dep)): trouve = m
            elif m != base: break
        mo += 1
        if mo > 12: mo = 1; y += 1
    return trouve

def telecharger(dep, dossier, millesime):
    """télécharge l'export départemental BDNB s'il est absent ; renvoie le chemin du zip"""
    url = BDNB_URL.format(m=millesime, d=dep); dest = os.path.join(dossier, os.path.basename(url))
    if os.path.exists(dest) and os.path.getsize(dest) > 1e6: return dest
    print(f'Téléchargement {url}')
    tmp = dest + '.part'
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=120) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, dest); print(f'  {os.path.getsize(dest)/1e6:,.0f} Mo')
    return dest

def resume_departement(dep, zpath, out, props, com, permis):
    """résumé compact d'un département pour le relais SuiviMarché (bdnb.json)"""
    m = re.search(r'millesime_([\w-]+?)_dep', os.path.basename(zpath)); millesime = m.group(1) if m else BDNB_MILLESIME
    sect = out['usage'].map(secteur)
    par_secteur = {k: int((sect == k).sum()) for k in ('agricole', 'industrie', 'tertiaire', 'autre')}
    kwc_secteur = {k: int(out.loc[sect == k, 'kwc_potentiel'].sum()) for k in ('agricole', 'industrie', 'tertiaire', 'autre')}
    r = {
        'millesime': millesime, 'calcule': datetime.date.today().isoformat(),
        'batiments': int(len(out)), 'score60': int((out['score'] >= 60).sum()), 'score80': int((out['score'] >= 80).sum()),
        'avec_proprietaire': int((out['siren'].fillna('') != '').sum()), 'proteges': int((out['contrainte'] != '').sum()),
        'hta': int((num(out['pdl_hta']).fillna(0) > 0).sum()), 'chantier_recent': int(out['permis_date'].notna().sum()),
        'emprise_m2': int(out['emprise_sol_m2'].sum()), 'kwc': int(out['kwc_potentiel'].sum()), 'mwh_an': int(out['production_mwh'].sum()),
        'conso_pro_mwh': int(out['conso_pro_mwh'].fillna(0).sum()),
        'secteurs': par_secteur, 'kwc_secteurs': kwc_secteur,
        'communes': [{'insee': str(c['code_commune_insee']), 'nom': str(c['libelle_commune_insee']), 'n': int(c['prospects']), 'kwc': int(c['kwc_potentiel'])} for _, c in com.head(5).iterrows()],
        'proprietaires': [{'siren': str(p['siren']), 'nom': str(p['proprietaire'])[:60], 'n': int(p['batiments']), 'kwc': int(p['kwc_potentiel']), 'sect': str(p['secteur']), 'com': str(p['commune_principale'])[:40], 'score': int(p['meilleur_score']), 'fj': ('' if pd.isna(p['forme_juridique']) else str(p['forme_juridique']))[:40]} for _, p in props.head(20).iterrows()],
    }
    if permis is not None:
        r['permis'] = {'n': int(len(permis)), 'm2_locaux': int(permis['m2_locaux_crees'].sum()), 'm2_agri': int(permis['m2_agri'].sum()), 'm2_indus': int(permis['m2_indus_entrepot'].sum())}
    cr = croiser_beges(dep, out, permis)
    try: pk = parkings_osm(dep)
    except Exception as e: print('  (parkings OSM non exploités :', e, ')'); pk = None
    # solaire existant (OSM) et installations classées (Géorisques), rattachés aux bâtiments des cibles (≤ 60 m) et aux SIREN
    try: so = solaire_osm(dep)
    except Exception as e: print('  (solaire OSM non exploité :', e, ')'); so = None
    try: ic = icpe_departement(dep)
    except Exception as e: print('  (ICPE non exploitées :', e, ')'); ic = None
    try: en = enseignes_osm(dep)
    except Exception as e: print('  (enseignes OSM non exploitées :', e, ')'); en = None
    if en: rattacher_enseignes(en, out)
    dossier = os.environ.get('BDNB_DOSSIER') or os.path.dirname(os.path.abspath(zpath))
    bd = bdappv_par_commune(dep, dossier)
    # contacts publics, friches, émissions par site, procédures collectives
    try: fi = finess_departement(dep, dossier)
    except Exception as e: print('  (FINESS non exploité :', e, ')'); fi = []
    try: el = elus_departement(dep, dossier)
    except Exception as e: print('  (élus non exploités :', e, ')'); el = {'maires': {}, 'epci': {}}
    try: co = contacts_osm(dep)
    except Exception as e: print('  (contacts OSM non exploités :', e, ')'); co = None
    try: fr = friches_departement(dep, dossier)
    except Exception as e: print('  (friches non exploitées :', e, ')'); fr = None
    try: ir = irep_departement(dep, dossier)
    except Exception as e: print('  (IREP non exploité :', e, ')'); ir = None
    try: pc = bodacc_collectives(dep)
    except Exception as e: print('  (BODACC non exploité :', e, ')'); pc = {}
    for p in r.get('proprietaires', []):
        if p.get('siren') in pc: p['pc'] = pc[p['siren']]
    if cr is not None:
        pos = {b: (la, lo) for b, la, lo in zip(out['batiment_groupe_id'], out['lat'], out['lon']) if la == la and la is not None}
        fi_par_siren = {}
        for x in fi: fi_par_siren.setdefault(x['siret'][:9], []).append(x)
        ir_par_siren = {}
        for x in (ir or []): ir_par_siren.setdefault(x['siret'][:9], []).append(x)
        gc = _grille(co) if co else None; gi2 = _grille([x for x in (ir or []) if x['lat']]) if ir else None; ir_pos = [x for x in (ir or []) if x['lat']]
        fr_cle = [(set(_mots(x.get('prop_nom', ''))), x) for x in (fr or []) if x.get('prop_nom')]
        for e in cr:
            sirens = set([e['siren']] + list(e.get('via') or []))
            if e['siren'] in pc: e['pc'] = pc[e['siren']]
            lst = [x for sn in sirens for x in fi_par_siren.get(sn, [])]
            if lst: e['finess'] = [{'nom': x['nom'], 'com': x['com'], 'tel': x['tel'], 'cat': x['cat']} for x in lst[:6]]
            lst = [x for sn in sirens for x in ir_par_siren.get(sn, [])]; vus = set(x['id'] for x in lst)
            if gi2:
                for b in e.get('bat_ids', []):
                    if b in pos:
                        for d_, i in _proches(gi2, ir_pos, pos[b][0], pos[b][1], 80):
                            if ir_pos[i]['id'] not in vus: lst.append(ir_pos[i]); vus.add(ir_pos[i]['id'])
            if lst: e['irep'] = {'n': len(lst), 'co2_t': int(sum(x['co2_t'] for x in lst)), 'annee': IREP_ANNEE, 'sites': [{'nom': x['nom'], 'com': x['com'], 'co2_t': x['co2_t'], 'polluants': x['polluants'][:3]} for x in sorted(lst, key=lambda x: -x['co2_t'])[:5]]}
            if gc:
                trouves = {}
                for b in e.get('bat_ids', []):
                    if b in pos:
                        for d_, i in _proches(gc, co, pos[b][0], pos[b][1], 80): trouves[i] = min(trouves.get(i, 1e9), d_)
                cle = _mots(e['nom'])
                for i, x in enumerate(co):
                    if i not in trouves and cle and len(set(x['cle']) & cle) >= min(2, len(cle)) and len(cle) >= 1 and (len(set(x['cle']) & cle) >= 2 or len(cle) == 1): trouves[i] = -1
                lst = [dict(co[i], d=int(d_) if d_ >= 0 else None) for i, d_ in sorted(trouves.items(), key=lambda kv: kv[1])][:4]
                if lst: e['osm_contacts'] = [{k: v for k, v in {'nom': x['nom'] or x['op'], 'tel': x['tel'], 'mail': x['mail'], 'site': x['site'], 'd': x['d'], 'id': x['id']}.items() if v not in (None, '')} for x in lst]
            cle = _mots(e['nom'])
            if cle and fr_cle:
                lst = [x for c_, x in fr_cle if len(c_ & cle) >= 2 or (len(c_ & cle) == 1 and len(cle) == 1 and len(c_) == 1)]
                if lst: e['friches'] = [{'nom': x.get('nom') or x.get('id', ''), 'com': x.get('com', ''), 'm2': x.get('m2'), 'statut': x.get('statut', '')} for x in lst[:3]]
    r['contacts'] = {'finess_n': len(fi), 'maires_n': len(el['maires']), 'epci_n': len(el['epci']), 'osm_n': len(co or [])}
    r['_contacts_complet'] = {'finess': fi, 'elus': el}   # les contacts OSM ne servent qu'au rattachement (osm_contacts des cibles)
    if fr is not None: r['friches'] = {'n': len(fr), 'ha': int(sum((x.get('m2') or 0) for x in fr) / 10000), 'sans_projet': sum(1 for x in fr if 'sans projet' in (x.get('statut') or '') or 'potentielle' in (x.get('statut') or ''))}; r['_friches_complet'] = fr
    if ir is not None: r['irep'] = {'n': len(ir), 'co2_t': int(sum(x['co2_t'] for x in ir)), 'annee': IREP_ANNEE}; r['_irep_complet'] = ir
    r['pc_n'] = len(pc)
    if cr is not None:
        if pk: rapprocher_parkings(cr, pk, props)
        pos = {b: (la, lo) for b, la, lo in zip(out['batiment_groupe_id'], out['lat'], out['lon']) if la == la and la is not None}
        if so:
            gs = _grille(so); noms_c = {e['siren']: (_mots(e['nom']) | set().union(*[_mots(p) for p in []])) for e in cr}
            for e in cr:
                trouves = {}
                for b in e.get('bat_ids', []):
                    if b in pos:
                        for d_, i in _proches(gs, so, pos[b][0], pos[b][1], 60): trouves[i] = min(trouves.get(i, 1e9), d_)
                cle = _mots(e['nom'])
                for i, x in enumerate(so):
                    if cle and set(x['cle']) & cle and i not in trouves: trouves[i] = -1   # rattachement par exploitant
                lst = [dict(so[i], d=int(d_) if d_ >= 0 else None) for i, d_ in sorted(trouves.items(), key=lambda kv: kv[1])][:8]
                if lst: e['pv'] = {'n': len(lst), 'kw': int(sum(x['kw'] or 0 for x in lst)), 'sites': [{'id': x['id'], 'nom': x['nom'] or x['op'], 'kw': x['kw'], 'lieu': x['lieu'], 'd': x['d'], 'lat': x['lat'], 'lon': x['lon']} for x in lst]}
        if ic:
            gi = _grille(ic)
            for e in cr:
                sirens = set([e['siren']] + list(e.get('via') or [])); lst = [x for x in ic if x['siret'][:9] in sirens]
                vus = set(x['aiot'] for x in lst)
                for b in e.get('bat_ids', []):
                    if b in pos:
                        for d_, i in _proches(gi, ic, pos[b][0], pos[b][1], 80):
                            if ic[i]['aiot'] not in vus: lst.append(dict(ic[i], d=int(d_))); vus.add(ic[i]['aiot'])
                if lst: e['icpe'] = resume_icpe(lst)
        if en:
            for e in cr:
                sirens = set([e['siren']] + list(e.get('via') or [])); lst = [x for x in en if x.get('siren') and x['siren'] in sirens]
                if lst: e['enseignes'] = {'n': len(lst), 'l': [{'b': x['brand'], 't': x['type'], 'com': x['com']} for x in lst[:6]]}
        for e in cr: e.pop('bat_ids', None)
        r['croisement'] = cr
    if en is not None:
        r['enseignes'] = resume_enseignes(en)
        r['_enseignes_complet'] = [{k: v for k, v in x.items() if k != 'cle' and v not in (None, '', [], False)} for x in en]
    if pk:
        r['_parkings_complet'] = pk.pop('complet', None); r['parkings'] = pk
    if so is not None or bd:
        r['_solaire_complet'] = {'osm': [{k: v for k, v in x.items() if k != 'cle' and v not in (None, '')} for x in (so or [])], 'bdappv': bd}
        r['solaire'] = {'osm_n': len(so or []), 'osm_kw': int(sum(x['kw'] or 0 for x in (so or []))), 'osm_sol': sum(1 for x in (so or []) if x['lieu'] == 'sol'), 'bdappv_n': (bd or {}).get('n', 0), 'bdappv_kwc': (bd or {}).get('kwc', 0)}
    if ic is not None:
        r['_icpe_complet'] = [{k: v for k, v in x.items() if v not in (None, '', [], False)} for x in ic]
        r['icpe'] = {'n': len(ic), 'seveso': sum(1 for x in ic if x['seveso'] and not x['seveso'].lower().startswith('non')), 'combustion': sum(1 for x in ic if any(y['n'].startswith('2910') or y['n'].startswith('3110') for y in x['rub'])), 'froid': sum(1 for x in ic if any(y['n'][:4] in ('4735', '1185', '4802', '2921') for y in x['rub'])), 'elevage': sum(1 for x in ic if x['elevage']), 'carriere': sum(1 for x in ic if x['carriere'])}
    return r

# ---------------- projection Lambert-93 (EPSG:2154) → WGS84 ----------------
def lambert93_vers_wgs84(x, y):
    import math
    n, c, xs, ys, e = 0.7256077650532670, 11754255.426096, 700000.0, 12655612.049876, 0.0818191910428158
    r = math.hypot(x - xs, ys - y); gamma = math.atan2(x - xs, ys - y)
    lon = math.radians(3) + gamma / n; latiso = -math.log(r / c) / n
    phi = 2 * math.atan(math.exp(latiso)) - math.pi / 2
    for _ in range(6): phi = 2 * math.atan(((1 + e * math.sin(phi)) / (1 - e * math.sin(phi))) ** (e / 2) * math.exp(latiso)) - math.pi / 2
    return (math.degrees(phi), math.degrees(lon))

def _grille(rows, cle_lat='lat', cle_lon='lon', pas=0.004):
    """index spatial simple : cellule (lat, lon) → liste d'indices"""
    g = {}
    for i, r in enumerate(rows):
        la, lo = r.get(cle_lat), r.get(cle_lon)
        if la is None or lo is None or la != la or lo != lo: continue
        g.setdefault((int(la / pas), int(lo / pas)), []).append(i)
    return g

def _proches(g, rows, lat, lon, dmax_m, pas=0.004, cle_lat='lat', cle_lon='lon'):
    import math
    out = []; ci, cj = int(lat / pas), int(lon / pas); k = math.cos(math.radians(lat)) * 111320
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for i in g.get((ci + di, cj + dj), []):
                r = rows[i]; d = math.hypot((r[cle_lon] - lon) * k, (r[cle_lat] - lat) * 111320)
                if d <= dmax_m: out.append((d, i))
    return sorted(out)

def _kw(txt):
    """'7.19 kWc' / '60.5 MW' / '36 kW' / '2 MWp' → kW"""
    m = re.search(r'([\d.,]+)\s*([kMG]?)W', str(txt or ''), re.I)
    if not m: return None
    v = float(m.group(1).replace(',', '.')); u = m.group(2).upper()
    return round(v * (1000 if u == 'M' else 1e6 if u == 'G' else 1), 1)

# ---------------- solaire existant : OpenStreetMap (générateurs et centrales solaires) et BDAPPV (installations déclarées, par commune) ----------------
def solaire_osm(dep):
    import time
    q = f'[out:json][timeout:240];area["ref:INSEE"="{dep}"]["admin_level"="6"]->.a;(nwr["generator:source"="solar"](area.a);nwr["plant:source"="solar"](area.a););out tags center;'
    data = None
    for essai in range(3):
        for base in OVERPASS:
            try:
                with urllib.request.urlopen(urllib.request.Request(base, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=300) as r: data = json.load(r)
                break
            except Exception as e: print('  (Overpass solaire', base.split('/')[2], ':', e, ')')
        if data is not None: break
        time.sleep(30)
    if data is None: return None
    rows = []
    for el in data.get('elements', []):
        t = el.get('tags') or {}; c = el.get('center') or {'lat': el.get('lat'), 'lon': el.get('lon')}
        if c.get('lat') is None: continue
        kw = _kw(t.get('generator:output:electricity') or t.get('plant:output:electricity'))
        rows.append({'id': f"{el.get('type', 'w')[0]}{el.get('id')}", 'nom': (t.get('name') or '')[:60], 'op': (t.get('operator') or t.get('owner') or '')[:60], 'kw': kw, 'lieu': 'sol' if t.get('power') == 'plant' or t.get('location') in ('ground', 'surface') or t.get('generator:place') == 'surface' else 'toiture' if t.get('location') == 'roof' or t.get('building') else '', 'lat': round(c['lat'], 5), 'lon': round(c['lon'], 5), 'debut': (t.get('start_date') or '')[:10], 'cle': sorted(_mots(t.get('operator', '')) | _mots(t.get('owner', '')) | _mots(t.get('name', '')))})
    print(f'  Solaire OSM : {len(rows):,} installations ({sum(1 for r in rows if r["kw"])} avec puissance, {sum(1 for r in rows if r["op"])} avec exploitant)')
    return rows

BDAPPV_URL = 'https://zenodo.org/api/records/7358126/files/data.zip/content'
def bdappv_par_commune(dep, dossier):
    """BDAPPV (Kasmi et al., Zenodo 7358126) : installations PV déclarées par leurs propriétaires (BDPV), sans coordonnées → comptage par commune"""
    try:
        zp = os.path.join(dossier or '.', 'bdappv_data.zip')
        if not os.path.exists(zp): urllib.request.urlretrieve(BDAPPV_URL, zp)
        with zipfile.ZipFile(zp) as zz:
            with zz.open('data/raw/raw-metadata.csv') as f: m = pd.read_csv(f, usecols=['departement', 'city', 'kWp', 'surface', 'dateInstalled', 'selfConsumption'])
        m = m[m['departement'].astype(str).str.zfill(2) == dep]
        if not len(m): return {'n': 0, 'kwc': 0, 'communes': {}}
        m['kWp'] = pd.to_numeric(m['kWp'], errors='coerce'); m['city'] = m['city'].fillna('').astype(str).str.strip()
        par = m.groupby('city').agg(n=('kWp', 'size'), kwc=('kWp', 'sum')).sort_values('n', ascending=False)
        return {'n': int(len(m)), 'kwc': int(m['kWp'].fillna(0).sum() / 1000), 'mediane_kwc': round(float(m['kWp'].median() / 1000), 1) if m['kWp'].notna().any() else None, 'communes': {c: {'n': int(r['n']), 'kwc': int(r['kwc'] / 1000)} for c, r in par.head(300).iterrows()}}
    except Exception as e:
        print('  (BDAPPV non exploité :', e, ')'); return None

# ---------------- installations classées (Géorisques) ----------------
def icpe_departement(dep):
    rows = []; page = 1
    while page <= 30:
        url = f'https://georisques.gouv.fr/api/v1/installations_classees?departement={dep}&page_size=1000&page={page}'
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=120) as r: j = json.load(r)
        for x in j.get('data', []):
            if (x.get('regime') or '') == 'Non ICPE': continue
            rub = [{'n': str(y.get('numeroRubrique') or ''), 'nature': str(y.get('nature') or '')[:80], 'q': y.get('quantiteTotale'), 'u': y.get('unite') or ''} for y in (x.get('rubriques') or [])[:8]]
            rows.append({'aiot': x.get('codeAIOT'), 'nom': str(x.get('raisonSociale') or '')[:60], 'siret': str(x.get('siret') or ''), 'naf': x.get('codeNaf') or '', 'insee': x.get('codeInsee') or '', 'com': x.get('commune') or '', 'lat': x.get('latitude'), 'lon': x.get('longitude'), 'regime': x.get('regime') or '', 'etat': x.get('etatActivite') or '', 'seveso': x.get('statutSeveso') or '', 'ied': bool(x.get('ied')), 'industrie': bool(x.get('industrie')), 'elevage': bool(x.get('bovins') or x.get('porcs') or x.get('volailles')), 'carriere': bool(x.get('carriere')), 'rub': rub})
        if page >= int(j.get('total_pages') or 1): break
        page += 1
    rows = [r for r in rows if not r['etat'].lower().startswith('cess')]
    print(f'  ICPE Géorisques : {len(rows):,} installations en activité ({sum(1 for r in rows if r["siret"])} avec SIRET)')
    return rows

# ---------------- contacts publics : FINESS (santé), élus (RNE), contacts OpenStreetMap ----------------
DATAGOUV = 'https://www.data.gouv.fr/api/1/datasets/'
def _ressource(dataset, motif, fmt=None):
    """URL de la ressource d'un jeu data.gouv dont le titre contient motif (les URL sont datées)."""
    with urllib.request.urlopen(urllib.request.Request(DATAGOUV + dataset + '/', headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=60) as r: j = json.load(r)
    for x in j.get('resources', []):
        if motif.lower() in (x.get('title') or '').lower() and (not fmt or (x.get('format') or '').lower() == fmt): return x['url']
    return None
def _fichier(url, dossier, nom):
    """Télécharge une fois par passage (cache dans le dossier de travail)."""
    os.makedirs(dossier, exist_ok=True); f = os.path.join(dossier, nom)
    if not os.path.exists(f) or os.path.getsize(f) < 1000:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=600) as r, open(f + '.tmp', 'wb') as o: shutil.copyfileobj(r, o)
        os.replace(f + '.tmp', f)
    return f
FINESS_CAT_EXCLUES = ('pharmacie', 'laboratoire', 'ambulance', 'cabinet', 'centre de sant', 'maison de sant', 'transport', 'pmi', 'planification', 'protection maternelle')
def finess_departement(dep, dossier):
    """Établissements sanitaires et sociaux du département (FINESS, extraction data.gouv) : nom, commune, téléphone, SIRET, catégorie."""
    url = _ressource('finess-extraction-du-fichier-des-etablissements', 'Extraction Finess des Etablissements au')
    if not url: return []
    f = _fichier(url, dossier, 'finess.csv'); rows = []
    with open(f, encoding='utf-8', errors='replace') as fh:
        for l in fh:
            if not l.startswith('structureet;'): continue
            c = l.rstrip('\n').split(';')
            if len(c) < 24 or c[13] != dep: continue
            cat = c[21] or c[19]
            if any(x in cat.lower() for x in FINESS_CAT_EXCLUES) or not c[16]: continue
            rows.append({'finess': c[1], 'nom': (c[4] or c[3])[:70], 'com': c[15].split(' ', 1)[1] if ' ' in c[15] else c[15], 'cp': c[15].split(' ')[0], 'insee': (dep + c[12]) if c[12] else '', 'tel': c[16], 'siret': c[22], 'cat': cat[:50], 'ej': c[2]})
    print(f'  FINESS : {len(rows):,} établissements avec téléphone')
    return rows
COG_COMMUNES_URL = 'https://www.insee.fr/fr/statistiques/fichier/8377162/v_commune_2025.csv'   # code officiel géographique INSEE : commune → canton (millésime à suivre chaque année)
def elus_departement(dep, dossier):
    """Maires par commune, présidents d'intercommunalité et conseillers départementaux (répertoire national des élus)."""
    import csv
    out = {'maires': {}, 'epci': {}}
    try:
        f = _fichier(_ressource('repertoire-national-des-elus-1', 'elus-maires'), dossier, 'elus-maires.csv')
        with open(f, encoding='utf-8', newline='') as fh:
            for r in csv.DictReader(fh, delimiter=';'):
                if r.get('Code du département') != dep: continue
                out['maires'][r['Code de la commune']] = {'nom': r["Nom de l'élu"], 'prenom': r["Prénom de l'élu"], 'com': r['Libellé de la commune'], 'depuis': r.get('Date de début de la fonction') or ''}
    except Exception as e: print('  (maires RNE non exploités :', e, ')')
    try:
        f = _fichier(_ressource('repertoire-national-des-elus-1', 'elus-conseillers-communautaires'), dossier, 'elus-epci.csv')
        with open(f, encoding='utf-8', newline='') as fh:
            for r in csv.DictReader(fh, delimiter=';'):
                fo = (r.get('Libellé de la fonction') or '').lower()
                if r.get('Code du département') != dep or not fo.startswith('président'): continue
                out['epci'][r['N° SIREN']] = {'epci': r["Libellé de l'EPCI"], 'nom': r["Nom de l'élu"], 'prenom': r["Prénom de l'élu"], 'com': r['Libellé de la commune de rattachement'], 'depuis': r.get('Date de début de la fonction') or ''}
    except Exception as e: print('  (présidents EPCI non exploités :', e, ')')
    # conseillers départementaux : président, vice-présidents (rang) et conseillers par canton (fiche des Départements, comptes clés publics)
    out['cd'] = []
    try:
        f = _fichier(_ressource('repertoire-national-des-elus-1', 'elus-conseillers-departementaux'), dossier, 'elus-cd.csv')
        with open(f, encoding='utf-8', newline='') as fh:
            for r in csv.DictReader(fh, delimiter=';'):
                if r.get('Code du département') != dep: continue
                fo = r.get('Libellé de la fonction') or ''; m = re.match(r'(\d+)(?:er|ère|ème)?\s+Vice', fo, re.I)
                out['cd'].append({'nom': r["Nom de l'élu"], 'prenom': r["Prénom de l'élu"], 'canton': r.get('Libellé du canton') or '', 'code_canton': r.get('Code du canton') or '', 'fonction': fo, 'rang': int(m.group(1)) if m else (0 if fo.lower().startswith('président') else 99), 'depuis': r.get('Date de début de la fonction') or r.get('Date de début du mandat') or ''})
        out['cd'].sort(key=lambda x: (x['rang'], x['canton'], x['nom']))
    except Exception as e: print('  (conseillers départementaux RNE non exploités :', e, ')')
    # canton de chaque commune (COG INSEE) → conseillers du canton dans la fiche d'une commune ; les villes découpées en plusieurs
    # cantons portent un pseudo-code dans le COG (Lille 5997) : on les rattache par le libellé des cantons du RNE (« Lille-1 »… « Lille-6 »)
    out['cantons'] = {}
    try:
        if out['cd']:
            import unicodedata
            nrm = lambda s: unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode().lower().strip()
            codes = {x['code_canton'] for x in out['cd'] if x['code_canton']}
            par_lib = {}
            for x in out['cd']:
                m = re.match(r'^(.*?)(?:-\d+)?$', nrm(x['canton'])); par_lib.setdefault(m.group(1), set()).add(x['code_canton'])
            f = _fichier(COG_COMMUNES_URL, dossier, 'cog_communes.csv')
            with open(f, encoding='utf-8-sig', newline='') as fh:
                for r in csv.DictReader(fh):
                    if r.get('TYPECOM') != 'COM' or r.get('DEP') != dep: continue
                    can = r.get('CAN') or ''
                    if can in codes: out['cantons'][r['COM']] = [can]
                    else:
                        l = par_lib.get(nrm(r.get('LIBELLE')))
                        if l: out['cantons'][r['COM']] = sorted(l)
            print(f"  Cantons COG : {len(out['cantons']):,} communes rattachées à leurs conseillers départementaux")
    except Exception as e: print('  (cantons des communes non exploités :', e, ')')
    print(f"  Élus RNE : {len(out['maires']):,} maires, {len(out['epci'])} présidents d'EPCI, {len(out.get('cd', []))} conseillers départementaux")
    return out
def contacts_osm(dep):
    """Objets nommés d'OpenStreetMap portant un téléphone, un courriel ou un site (sites industriels, commerciaux, bureaux, entrepôts, fermes)."""
    import time
    sel = '["name"][~"^(phone|contact:phone|email|contact:email|website|contact:website)$"~"."]'
    q = (f'[out:json][timeout:240];area["ref:INSEE"="{dep}"]["admin_level"="6"]->.a;(nwr{sel}["landuse"~"industrial|commercial|retail|farmyard"](area.a);nwr{sel}["building"~"industrial|warehouse|commercial|retail|office|farm"](area.a);'
         f'nwr{sel}["industrial"](area.a);nwr{sel}["office"~"company|industrial|energy_supplier"](area.a);nwr{sel}["man_made"~"works"](area.a);nwr{sel}["craft"](area.a);nwr{sel}["amenity"~"hospital|clinic|school|college|university|townhall|social_facility"](area.a););out tags center;')
    data = None
    for essai in range(3):
        for base in OVERPASS:
            try:
                with urllib.request.urlopen(urllib.request.Request(base, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=300) as r: data = json.load(r)
                break
            except Exception as e: print('  (Overpass contacts', base.split('/')[2], ':', e, ')')
        if data is not None: break
        time.sleep(30)
    if data is None: return None
    rows = []
    for el in data.get('elements', []):
        t = el.get('tags') or {}; c = el.get('center') or {'lat': el.get('lat'), 'lon': el.get('lon')}
        if c.get('lat') is None: continue
        nom = (t.get('name') or '')[:60]; op = (t.get('operator') or t.get('brand') or '')[:60]
        rows.append({'id': f"{el.get('type', 'w')[0]}{el.get('id')}", 'nom': nom, 'op': op, 'tel': (t.get('phone') or t.get('contact:phone') or '')[:40], 'mail': (t.get('email') or t.get('contact:email') or '')[:80], 'site': (t.get('website') or t.get('contact:website') or '')[:120], 'lat': round(c['lat'], 5), 'lon': round(c['lon'], 5), 'cle': sorted(_mots(nom) | _mots(op))})
    print(f'  Contacts OSM : {len(rows):,} objets nommés ({sum(1 for r in rows if r["tel"])} téléphones, {sum(1 for r in rows if r["mail"])} courriels, {sum(1 for r in rows if r["site"])} sites)')
    return rows
# ---------------- friches (Cartofriches, Cerema) ----------------
def friches_departement(dep, dossier):
    url = _ressource('sites-references-dans-cartofriches', 'friches-standard', 'csv')
    if not url: return None
    f = _fichier(url, dossier, 'friches.csv')
    df = pd.read_csv(f, sep=';', quotechar='"', dtype=str, na_values=['NA'], keep_default_na=False, low_memory=False)
    df = df[df['comm_insee'].fillna('').str.startswith(dep)].fillna('')
    rows = []
    for _, r in df.iterrows():
        m = re.match(r'POINT \(([-\d.]+) ([-\d.]+)\)', str(r.get('geompoint') or ''))
        surf = num(pd.Series([r.get('unite_fonciere_surface')])).iloc[0] if r.get('unite_fonciere_surface') else None
        rows.append({k: v for k, v in {'id': r['site_id'], 'nom': (r.get('site_nom') or '')[:70], 'type': r.get('site_type') or '', 'com': r.get('comm_nom') or '', 'insee': r.get('comm_insee') or '', 'statut': r.get('site_statut') or '', 'm2': int(surf) if surf == surf and surf else None,
                                        'act': (r.get('activite_libelle') or '')[:60], 'fin': r.get('activite_fin_annee') or '', 'prop': r.get('proprio_type') or '', 'prop_nom': (r.get('proprio_nom') or '')[:60] if (r.get('proprio_nom') or '') != 'Nom anonymisé' else '', 'poll': r.get('sol_pollution_existe') or '', 'zone': r.get('urba_zone_type') or '', 'bati': r.get('bati_etat') or '',
                                        'lat': float(m.group(2)) if m else None, 'lon': float(m.group(1)) if m else None, 'src': (r.get('source_nom') or '')[:40], 'url': r.get('site_url') or ''}.items() if v not in (None, '')})
    print(f'  Friches Cartofriches : {len(rows):,} sites ({sum((x.get("m2") or 0) for x in rows) / 10000:,.0f} ha)')
    return rows
# ---------------- émissions déclarées par établissement (IREP, Géorisques) ----------------
IREP_ANNEE = 2024
def irep_departement(dep, dossier):
    import zipfile, csv, io
    f = _fichier(f'https://files.georisques.fr/irep/{IREP_ANNEE}.zip', dossier, f'irep{IREP_ANNEE}.zip')
    zf = zipfile.ZipFile(f); etab = {}
    def lire_csv(nom):
        n = [x for x in zf.namelist() if x.lower().endswith(nom)][0]
        return csv.DictReader(io.TextIOWrapper(zf.open(n), encoding='utf-8', errors='replace'), delimiter=';')
    for r in lire_csv('etablissements.csv'):
        if (r.get('code_departement') or '').strip() != dep: continue
        try: lat, lon = float(r.get('coordonnees_y') or 'nan'), float(r.get('coordonnees_x') or 'nan')
        except ValueError: lat = lon = float('nan')
        etab[r['identifiant']] = {'id': r['identifiant'], 'nom': (r.get('nom_etablissement') or '')[:60], 'siret': r.get('numero_siret') or '', 'com': r.get('commune') or '', 'insee': r.get('code_insee') or '', 'lat': round(lat, 5) if lat == lat else None, 'lon': round(lon, 5) if lon == lon else None, 'ape': r.get('code_ape') or '', 'co2_t': 0, 'polluants': []}
    for r in lire_csv('emissions.csv'):
        e = etab.get(r.get('identifiant'))
        if not e or (r.get('milieu') or '') != 'Air': continue
        pol = r.get('polluant') or ''; qv = (r.get('quantite') or '').replace(',', '.')
        try: qte = float(qv)
        except ValueError: continue
        u = (r.get('unite') or '').lower(); kg = qte * (1000 if u.startswith('t') else 1)
        pl = pol.lower()
        if 'co2' in pl and 'non biomasse' in pl and 'total' not in pl: e['co2_t'] += kg / 1000; e['annee'] = r.get('annee_emission'); e['co2_src'] = 'fossile'
        elif 'co2' in pl and 'total' in pl: e['co2_tot'] = e.get('co2_tot', 0) + kg / 1000; e['annee'] = e.get('annee') or r.get('annee_emission')
        elif 'co2' in pl: pass
        elif len(e['polluants']) < 4: e['polluants'].append(pol.split('(')[0].strip()[:30])
    for x in etab.values():
        if not x['co2_t'] and x.get('co2_tot'): x['co2_t'] = x['co2_tot']; x['co2_src'] = 'total (dont biomasse)'
        x.pop('co2_tot', None)
    rows = [dict(x, co2_t=int(x['co2_t'])) for x in etab.values() if x['co2_t'] > 0 or x['polluants']]
    rows.sort(key=lambda x: -x['co2_t'])
    print(f'  IREP {IREP_ANNEE} : {len(rows):,} établissements déclarants, {sum(x["co2_t"] for x in rows) / 1e6:,.1f} MtCO2')
    return rows
# ---------------- procédures collectives (BODACC, DILA) ----------------
def bodacc_collectives(dep, annees=2):
    """SIREN du département ayant fait l'objet d'une annonce de procédure collective récente : {siren: (date, nature)}."""
    depuis = (datetime.date.today() - datetime.timedelta(days=365 * annees)).isoformat()
    url = 'https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/exports/json?' + urllib.parse.urlencode({'where': f'numerodepartement="{dep}" and familleavis="collective" and dateparution>="{depuis}"', 'select': 'registre,dateparution,jugement'})
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=600) as r: data = json.load(r)
    out = {}
    for x in data:
        reg = x.get('registre') or []; sn = (reg[0] if isinstance(reg, list) and reg else str(reg)).replace(' ', '')
        if len(sn) != 9: continue
        j = x.get('jugement'); nat = ''
        if isinstance(j, str):
            try: j = json.loads(j)
            except Exception: j = None
        if isinstance(j, dict): nat = (j.get('nature') or j.get('famille') or '')[:80]
        d = x.get('dateparution') or ''
        if sn not in out or d > out[sn]['date']: out[sn] = {'date': d, 'nature': nat}
    print(f'  BODACC : {len(out):,} SIREN en procédure collective depuis {depuis}')
    return out
RUBRIQUES_ENERGIE = {'2910': 'combustion', '2921': 'refroidissement (tours)', '4735': 'ammoniac (froid)', '1185': 'fluides frigorigènes', '4802': 'gaz fluorés (froid)', '3110': 'combustion > 50 MW', '2260': 'broyage', '2515': 'concassage', '2661': 'plastiques', '2450': 'imprimerie', '2560': 'travail des métaux', '2210': 'abattoir', '2221': 'alimentaire', '2230': 'lait', '2140': 'volailles', '2101': 'bovins', '2102': 'porcs', '2111': 'volailles', '2510': 'carrière', '3610': 'papier', '2410': 'bois'}

def resume_icpe(lst):
    if not lst: return None
    rubs = {}
    for r in lst:
        for y in r['rub']:
            k = y['n'][:4]; rubs[k] = rubs.get(k, 0) + 1
    return {'n': len(lst), 'seveso': sum(1 for r in lst if r['seveso'] and not r['seveso'].lower().startswith('non')), 'ied': sum(1 for r in lst if r['ied']), 'elevage': sum(1 for r in lst if r['elevage']), 'carriere': sum(1 for r in lst if r['carriere']), 'combustion': sum(1 for r in lst if any(y['n'].startswith('2910') or y['n'].startswith('3110') for y in r['rub'])), 'froid': sum(1 for r in lst if any(y['n'][:4] in ('4735', '1185', '4802', '2921') for y in r['rub'])),
            'rubriques': sorted(({'n': k, 'lib': RUBRIQUES_ENERGIE.get(k, ''), 'x': v} for k, v in rubs.items()), key=lambda d: -d['x'])[:6], 'sites': [{'nom': r['nom'], 'com': r['com'], 'siret': r['siret'], 'regime': r['regime'], 'rub': ', '.join(y['n'] for y in r['rub'][:5])} for r in lst[:6]]}

# ---------------- croisement avec les bilans GES de l'ADEME (gros consommateurs) ----------------
BEGES_API = 'https://data.ademe.fr/data-fair/api/v1/datasets/bilan-ges/lines'
_BEGES = None

def charger_beges():
    """tous les bilans GES (national, ~10 000 lignes, 2 pages) : SIREN principal → dernier bilan ; SIREN consolidés → SIREN principal"""
    global _BEGES
    if _BEGES is not None: return _BEGES
    url = BEGES_API + '?size=10000&select=siren_principal,raison_sociale,annee_de_reporting,emissions_publication_p21,siren_des_entites_consolidees,code_departement,apenaf_associe'
    principal, filiales = {}, {}
    while url:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=120) as r: j = json.load(r)
        for x in j.get('results', []):
            sp = str(x.get('siren_principal') or '').zfill(9); t = x.get('emissions_publication_p21') or 0
            if sp == '000000000' or not t: continue
            an = int(x.get('annee_de_reporting') or 0); cur = principal.get(sp)
            if cur and cur['annee'] >= an: continue
            principal[sp] = {'siren': sp, 'nom': str(x.get('raison_sociale') or '')[:60], 'annee': an, 't': float(t), 'dep': str(x.get('code_departement') or ''), 'naf': str(x.get('apenaf_associe') or '').replace('.', '')}
            for f in re.findall(r'\d{9}', str(x.get('siren_des_entites_consolidees') or '')): filiales.setdefault(f, sp)
        url = j.get('next') if len(j.get('results', [])) == 10000 else None
    _BEGES = (principal, filiales); print(f'  bilans GES ADEME : {len(principal):,} structures, {len(filiales):,} SIREN consolidés')
    return _BEGES

def croiser_beges(dep, out, permis):
    """structures ayant déposé un bilan GES (siège n'importe où) qui possèdent des grandes toitures ou ont un permis récent dans le département"""
    try: principal, filiales = charger_beges()
    except Exception as e: print('  (bilans GES non interrogés :', e, ')'); return None
    cible = lambda s: principal.get(s) and s or filiales.get(s)                              # SIREN propriétaire → SIREN principal du bilan
    agg = {}
    def entree(sp):
        if sp not in agg: b = principal[sp]; agg[sp] = {'siren': sp, 'nom': b['nom'], 'dep': b['dep'], 'annee': b['annee'], 't': int(b['t']), 'naf': b.get('naf', ''), 'n': 0, 'kwc': 0, 'score': 0, 'com': {}, 'permis': 0, 'permis_m2': 0, 'via': set(),
                                                        'conso': 0, 'prod': 0, 'hta': 0, 'agri': 0, 'emprise': 0, 'sol': 0, 'parcelles': set(), 'gaz': 0, 'gaz_n': 0, 'dpe': {}, 'dpe_chauf': {}, 'sites': {}, 'bat_ids': []}
        return agg[sp]
    own = out[out['siren'].fillna('') != '']
    for s_, g in own.groupby('siren'):
        sp = cible(str(s_))
        if not sp: continue
        e = entree(sp); e['n'] += int(len(g)); e['kwc'] += int(g['kwc_potentiel'].sum()); e['score'] = max(e['score'], int(g['score'].max())); e['via'].add(str(s_))
        for c, k in g['commune'].value_counts().head(3).items(): e['com'][c] = e['com'].get(c, 0) + int(k)
        # besoins : consommation Enedis des bâtiments (MWh/an), production possible, raccordements HTA, part agricole, foncier libre sur les parcelles
        e['conso'] += int(num(g['conso_pro_mwh']).fillna(0).sum()); e['prod'] += int(num(g['production_mwh']).fillna(0).sum())
        e['hta'] += int((num(g['pdl_hta']).fillna(0) > 0).sum()); e['agri'] += int((g['usage'].map(secteur) == 'agricole').sum()); e['emprise'] += int(num(g['emprise_sol_m2']).fillna(0).sum())
        for pid, pm2, em in zip(g['parcelle_id'].fillna(''), num(g['parcelle_m2']).fillna(0), num(g['emprise_sol_m2']).fillna(0)):
            if pid and pid not in e['parcelles'] and pm2 - em >= 3000: e['parcelles'].add(pid); e['sol'] += int(pm2 - em)
        # gaz, DPE tertiaire, sites par commune, bâtiments (pour le solaire existant et les ICPE à proximité)
        gzv = num(g['conso_gaz_mwh']).fillna(0); e['gaz'] += int(gzv.sum()); e['gaz_n'] += int((gzv > 0).sum())
        for cl, ch in zip(g['dpe_classe'].fillna(''), g['dpe_chauffage'].fillna('')):
            if cl: e['dpe'][cl] = e['dpe'].get(cl, 0) + 1
            if ch: k = 'gaz' if 'gaz' in ch.lower() else 'fioul' if 'fioul' in ch.lower() else 'électricité' if 'lectri' in ch.lower() else 'réseau' if 'seau' in ch.lower() else 'bois' if 'bois' in ch.lower() else 'autre'; e['dpe_chauf'][k] = e['dpe_chauf'].get(k, 0) + 1
        # sites par commune, avec la position de chaque bâtiment (mini-carte de la fiche prospect : bâtiments BDNB sur photo aérienne)
        for com_, ins, kwc_, la, lo, adr, em, cp in zip(g['commune'].fillna(''), g['insee'].fillna(''), num(g['kwc_potentiel']).fillna(0), g['lat'], g['lon'], g['adresse'].fillna(''), num(g['emprise_sol_m2']).fillna(0), num(g['corps']).fillna(1) if 'corps' in g.columns else [1] * len(g)):
            st = e['sites'].setdefault(ins or com_, {'com': com_, 'insee': ins, 'n': 0, 'kwc': 0, 'lat': [], 'lon': [], 'adr': '', 'bats': []})
            st['n'] += 1; st['kwc'] += int(kwc_); st['adr'] = st['adr'] or adr[:60]
            if la == la and lo == lo and la is not None: st['lat'].append(la); st['lon'].append(lo); b_ = {'lat': round(float(la), 5), 'lon': round(float(lo), 5), 'm2': int(em), 'kwc': int(kwc_), 'adr': adr[:60]}; (int(cp) > 1) and b_.update({'corps': int(cp)}); st['bats'].append(b_)
        e['bat_ids'].extend(list(g['batiment_groupe_id']))
    # permis : ceux rattachés aux bâtiments (zip BDNB) et le CSV Sitadel du département s'il est fourni
    pl = [(str(a), float(b or 0)) for a, b in zip(out['permis_siren'].fillna(''), out['permis_m2_locaux'].fillna(0)) if a]
    if permis is not None: pl += [(str(a), float(b or 0)) for a, b in zip(permis['siren'].fillna(''), permis['m2_locaux_crees'].fillna(0)) if a]
    for s_, m2 in pl:
        sp = cible(s_)
        if not sp: continue
        e = entree(sp); e['permis'] += 1; e['permis_m2'] += int(m2); e['via'].add(s_)
    rows = sorted(agg.values(), key=lambda e: (e['kwc'], e['permis_m2']), reverse=True)[:40]
    for e in rows:
        e['com'] = ', '.join(c for c, _ in sorted(e['com'].items(), key=lambda kv: -kv[1])[:2]); e['via'] = sorted(e['via'])[:6]; e['parcelles'] = len(e['parcelles'])
        e['sites'] = sorted(({'com': v['com'], 'insee': v['insee'], 'n': v['n'], 'kwc': v['kwc'], 'adr': v['adr'], 'lat': round(sum(v['lat']) / len(v['lat']), 5) if v['lat'] else None, 'lon': round(sum(v['lon']) / len(v['lon']), 5) if v['lon'] else None, 'bats': sorted(v['bats'], key=lambda x: -x['kwc'])[:12]} for v in e['sites'].values()), key=lambda d: -d['kwc'])[:8]
        if not e['dpe']: e.pop('dpe'); e.pop('dpe_chauf')
    for e in agg.values():
        if e not in rows: e.pop('bat_ids', None)
    print(f'  Croisement bilans GES : {len(agg):,} structures avec toitures ou permis dans le {dep} (publiées : {len(rows)})')
    return rows

# ---------------- parkings extérieurs (OpenStreetMap) : obligation d'ombrières, loi APER art. 40 ----------------
OVERPASS = ('https://overpass-api.de/api/interpreter', 'https://maps.mail.ru/osm/tools/overpass/api/interpreter')   # kumi.systems et private.coffee répondent 504 sur les requêtes par area
PARKING_MIN = 1500          # m² : seuil de l'obligation d'ombrières (loi APER)
MOTS_VIDES = {'france', 'parking', 'centre', 'commercial', 'zone', 'societe', 'groupe', 'hypermarche', 'supermarche', 'magasin', 'sas', 'sarl', 'entreprise', 'entreprises', 'nord', 'paris', 'lille', 'departement', 'communaute', 'commune', 'ville', 'region', 'nationale', 'general', 'generale', 'industrie', 'industries', 'services', 'service', 'retail', 'europe', 'holding', 'immobilier', 'immobiliere', 'site', 'usine', 'agence', 'client', 'clients', 'personnel', 'visiteurs', 'salaries', 'employes', 'public', 'prive', 'gare', 'stationnement', 'aire', 'covoiturage', 'relais',
              'grand', 'grande', 'place', 'saint', 'sainte', 'port', 'maritime', 'charles', 'gaulle', 'jean', 'pierre', 'marie', 'louis', 'avenue', 'boulevard', 'route', 'chemin', 'allee', 'espace', 'parc', 'plateau', 'halle', 'halles', 'hotel', 'mairie', 'ecole', 'college', 'lycee', 'stade', 'salle', 'sport', 'sports', 'piscine', 'cimetiere', 'eglise', 'marche', 'maison', 'complexe', 'terrain', 'terrains', 'poids', 'lourds', 'camions', 'voitures', 'velos', 'bus', 'cars', 'nouveau', 'nouvelle', 'ancien', 'ancienne', 'petit', 'petite', 'haut', 'haute', 'basse', 'vieux', 'vieille', 'metropole', 'europeenne', 'urbaine', 'agglomeration', 'hauts', 'flandre', 'flandres', 'artois', 'picardie', 'normandie', 'bretagne', 'loire', 'atlantique', 'pays', 'social', 'sociale', 'habitat', 'logement', 'office', 'universite', 'hopital', 'hospitalier', 'clinique', 'polyclinique', 'medical', 'sante', 'transports', 'transport', 'logistique', 'distribution', 'developpement', 'gestion', 'exploitation', 'production', 'energie', 'energies', 'electricite', 'solaire', 'company', 'international', 'invest', 'investissement', 'participations', 'financiere', 'capital', 'partners', 'group', 'formation', 'faculte', 'medecine', 'pole', 'professionnelle', 'nationale', 'agence', 'adultes', 'institut', 'ecoles', 'campus', 'technique', 'technologie', 'technopole', 'atelier', 'ateliers', 'depot', 'entrepot', 'plateforme'}
def _cle_parking(t):
    """mots-clés d'un parking : nom d'enseigne/exploitant (brand, operator) en entier, sinon seulement le premier mot significatif du nom"""
    m = _mots(t.get('brand', '')) | _mots(t.get('operator', ''))
    if not m:
        for w in re.split(r'[^a-z0-9]+', __import__('unicodedata').normalize('NFKD', str(t.get('name') or '')).encode('ascii', 'ignore').decode().lower()):
            if len(w) >= 4 and w not in MOTS_VIDES and not w.isdigit(): m = {w}; break
    return m

def _mots(t):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(t or '')).encode('ascii', 'ignore').decode().lower()
    return {m for m in re.split(r'[^a-z0-9]+', t) if len(m) >= 4 and m not in MOTS_VIDES and not m.isdigit()}

def _aire_m2(g):
    import math
    if len(g) < 3: return 0.0
    lat0 = math.radians(sum(p['lat'] for p in g) / len(g)); k = 111320.0
    pts = [(p['lon'] * k * math.cos(lat0), p['lat'] * k) for p in g]
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))) / 2

def _dans(pt, ring):
    x, y = pt; n = len(ring); inside = False; j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi: inside = not inside
        j = i
    return inside

def communes_contours(dep):
    """contours des communes du département (geo.api.gouv.fr) -> liste (insee, nom, bbox, anneaux)"""
    url = f'https://geo.api.gouv.fr/departements/{dep}/communes?format=geojson&geometry=contour'
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=120) as r: gj = json.load(r)
    out = []
    for f in gj.get('features', []):
        geom = f.get('geometry') or {}; polys = [geom['coordinates']] if geom.get('type') == 'Polygon' else geom.get('coordinates', [])
        rings = [[(x, y) for x, y in poly[0]] for poly in polys if poly]
        if not rings: continue
        xs = [x for rg in rings for x, _ in rg]; ys = [y for rg in rings for _, y in rg]
        out.append((f['properties'].get('code', ''), f['properties'].get('nom', ''), (min(xs), min(ys), max(xs), max(ys)), rings))
    return out

def parkings_osm(dep):
    """parkings extérieurs de plus de PARKING_MIN m² (OSM, périmètre > 160 m) avec commune, nom/exploitant, surface"""
    q = f'[out:json][timeout:280];area["ref:INSEE"="{dep}"]["admin_level"="6"]->.a;way["amenity"="parking"]["parking"!~"underground|multi-storey|rooftop"](area.a)(if:length()>160);out geom;'
    data = None
    import time
    for essai in range(3):
        for base in OVERPASS:
            try:
                with urllib.request.urlopen(urllib.request.Request(base, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=300) as r: data = json.load(r)
                break
            except Exception as e: print('  (Overpass', base.split('/')[2], ':', e, ')')
        if data is not None: break
        time.sleep(30)
    if data is None: return None
    try: coms = communes_contours(dep)
    except Exception as e: print('  (contours des communes indisponibles :', e, ')'); coms = []
    rows = []
    for el in data.get('elements', []):
        g = el.get('geometry') or []
        if el.get('type') != 'way' or len(g) < 4: continue
        a = _aire_m2(g)
        if a < PARKING_MIN: continue
        t = el.get('tags') or {}; lon = sum(p['lon'] for p in g) / len(g); lat = sum(p['lat'] for p in g) / len(g)
        com = ''
        for insee, nom, (x0, y0, x1, y1), rings in coms:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and any(_dans((lon, lat), rg) for rg in rings): com = nom; break
        rows.append({'id': int(el['id']), 'nom': (t.get('name') or t.get('operator') or t.get('brand') or '')[:60], 'op': (t.get('operator') or '')[:60], 'brand': (t.get('brand') or '')[:40], 'com': com, 'm2': int(a), 'lat': round(lat, 5), 'lon': round(lon, 5), 'acces': t.get('access', ''), 'places': t.get('capacity', ''), 'cle': sorted(_cle_parking(t))})
    rows.sort(key=lambda r: -r['m2'])
    geo = set()                                                     # noms de communes du département : jamais des clés d'enseigne (« Calais », « Nantes »…)
    for _, nom, _, _ in coms: geo |= {m for m in re.split(r'[^a-z0-9]+', __import__('unicodedata').normalize('NFKD', nom).encode('ascii', 'ignore').decode().lower()) if len(m) >= 4}
    for r in rows: r['cle'] = [m for m in r['cle'] if m not in geo]
    n10 = sum(1 for r in rows if r['m2'] >= 10000)
    top = [r for r in rows if r['nom']][:30] + [r for r in rows if not r['nom'] and r['m2'] >= 10000][:20]
    print(f'  Parkings OSM ≥ {PARKING_MIN} m² : {len(rows):,} ({n10} ≥ 10 000 m², {sum(1 for r in rows if r["nom"])} nommés, {sum(1 for r in rows if r["op"] or r["brand"])} avec exploitant ou enseigne)')
    return {'n1500': len(rows), 'n10000': n10, 'm2': int(sum(r['m2'] for r in rows)), 'm2_10000': int(sum(r['m2'] for r in rows if r['m2'] >= 10000)), 'nommes': sum(1 for r in rows if r['nom']), 'top': top,
            'tous': [(r['nom'], r['com'], r['m2'], set(r['cle'])) for r in rows if r['cle']], 'geo': geo,   # 'tous' et 'geo' servent au rapprochement puis sont retirés
            'complet': [{k: r[k] for k in ('id', 'nom', 'op', 'brand', 'com', 'm2', 'lat', 'lon', 'acces', 'places') if r.get(k)} for r in rows], 'exploitants': sum(1 for r in rows if r['op'] or r['brand'])}   # liste complète → parkings/<dep>.json

def _overpass(q, essais=3):
    """requête Overpass avec bascule de serveur et nouvel essai ; None si tout échoue"""
    import time
    for essai in range(essais):
        for base in OVERPASS:
            try:
                with urllib.request.urlopen(urllib.request.Request(base, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': 'SuiviMarche-bdnb/1.0'}), timeout=300) as r: return json.load(r)
            except Exception as e: print('  (Overpass', base.split('/')[2], ':', e, ')')
        time.sleep(30)
    return None

# ---------------- enseignes sous marque (OSM) : magasins, restauration, stations, banques, salles de sport, hôtels ----------------
# Le propriétaire des murs (bâtiment BDNB ≥ 400 m² le plus proche) est souvent une SCI, un franchisé ou une foncière locale,
# distinct de l'enseigne : c'est lui qui décide d'une toiture solaire ou d'une ombrière.
ENSEIGNE_AMENITY = {'fast_food': 'restauration rapide', 'restaurant': 'restaurant', 'fuel': 'station-service', 'bank': 'banque', 'car_wash': 'lavage auto', 'cinema': 'cinéma', 'pharmacy': 'pharmacie'}
ENSEIGNE_LEISURE = {'fitness_centre': 'salle de sport', 'sports_centre': 'centre sportif', 'bowling_alley': 'bowling'}
ENSEIGNE_DMAX = 60          # m : distance maximale entre le point OSM et le bâtiment BDNB rattaché
def enseignes_osm(dep):
    """points d'intérêt portant une marque (brand) dans OpenStreetMap, avec commune, type, position"""
    q = (f'[out:json][timeout:280];area["ref:INSEE"="{dep}"]["admin_level"="6"]->.a;(nwr["brand"]["shop"](area.a);'
         f'nwr["brand"]["amenity"~"^({"|".join(ENSEIGNE_AMENITY)})$"](area.a);nwr["brand"]["leisure"~"^({"|".join(ENSEIGNE_LEISURE)})$"](area.a);nwr["brand"]["tourism"="hotel"](area.a););out center tags;')
    data = _overpass(q)
    if data is None: return None
    try: coms = communes_contours(dep)
    except Exception as e: print('  (contours des communes indisponibles :', e, ')'); coms = []
    rows = []
    for el in data.get('elements', []):
        t = el.get('tags') or {}; c = el.get('center') or {'lat': el.get('lat'), 'lon': el.get('lon')}
        if c.get('lat') is None or not t.get('brand'): continue
        lat, lon = float(c['lat']), float(c['lon'])
        if t.get('shop'): typ = 'commerce · ' + str(t['shop']).replace('_', ' ').replace(';', ', ')[:30]
        elif t.get('amenity') in ENSEIGNE_AMENITY: typ = ENSEIGNE_AMENITY[t['amenity']]
        elif t.get('leisure') in ENSEIGNE_LEISURE: typ = ENSEIGNE_LEISURE[t['leisure']]
        elif t.get('tourism') == 'hotel': typ = 'hôtel'
        else: continue
        com = ''
        for insee, nom, (x0, y0, x1, y1), rings in coms:
            if x0 <= lon <= x1 and y0 <= lat <= y1 and any(_dans((lon, lat), rg) for rg in rings): com = nom; break
        rows.append({'id': f"{el.get('type', 'n')[0]}{el.get('id')}", 'brand': str(t.get('brand'))[:40], 'nom': (t.get('name') or '')[:60], 'op': (t.get('operator') or '')[:60], 'type': typ, 'com': com,
                     'lat': round(lat, 5), 'lon': round(lon, 5), 'cle': _mots(t.get('brand', '')) | _mots(t.get('operator', ''))})
    print(f'  Enseignes OSM : {len(rows):,} points sous marque ({len(set(r["brand"] for r in rows))} marques)')
    return rows

def rattacher_enseignes(en, out):
    """bâtiment BDNB le plus proche (≤ ENSEIGNE_DMAX m) de chaque enseigne : propriétaire des murs (SIREN, nom), toiture, emprise.
       murs_tiers = le propriétaire ne porte pas le nom de la marque (franchisé, SCI, foncière, investisseur)"""
    if not en or out is None or 'lat' not in out.columns: return
    cols = {c: (c in out.columns) for c in ('batiment_groupe_id', 'lat', 'lon', 'siren', 'proprietaire', 'kwc_potentiel', 'emprise_sol_m2', 'usage', 'adresse')}
    g = lambda c, d=None: (out[c] if cols.get(c) else pd.Series([d] * len(out), index=out.index))
    bats = [{'id': str(b), 'lat': float(la), 'lon': float(lo), 'siren': str(sn or ''), 'prop': str(pn or '')[:60], 'kwc': int(k or 0), 'm2': int(m or 0), 'usage': str(u or '')[:30], 'adr': str(a or '')[:60]}
            for b, la, lo, sn, pn, k, m, u, a in zip(g('batiment_groupe_id', ''), out['lat'], out['lon'], g('siren', '').fillna(''), g('proprietaire', '').fillna(''), g('kwc_potentiel', 0).fillna(0), g('emprise_sol_m2', 0).fillna(0), g('usage', '').fillna(''), g('adresse', '').fillna(''))
            if la == la and lo == lo and la is not None]
    if not bats: return
    gb = _grille(bats); n = 0; nt = 0
    for x in en:
        pr = _proches(gb, bats, x['lat'], x['lon'], ENSEIGNE_DMAX)
        if not pr: continue
        d_, i = pr[0]; b = bats[i]; n += 1
        x['bat'] = b['id']; x['d'] = int(d_); x['kwc'] = b['kwc']; x['m2'] = b['m2']; x['usage'] = b['usage']; x['adr'] = b['adr']
        if b['siren']:
            x['siren'] = b['siren']; x['prop'] = b['prop']
            x['murs_tiers'] = not (x['cle'] & _mots(b['prop']))
            if x['murs_tiers']: nt += 1
    print(f'  Enseignes rattachées à un bâtiment BDNB : {n:,} ({nt:,} dont les murs appartiennent à un tiers de la marque)')

def resume_enseignes(en):
    """résumé publié dans bdnb.json : comptes et propriétaires de murs les plus présents (plusieurs enseignes ou grande toiture)"""
    par = {}
    for x in en:
        if not x.get('siren'): continue
        p = par.setdefault(x['siren'], {'siren': x['siren'], 'nom': x['prop'], 'n': 0, 'kwc': 0, 'brands': [], 'tiers': 0, 'com': x['com']})
        p['n'] += 1; p['kwc'] += x.get('kwc', 0); p['tiers'] += 1 if x.get('murs_tiers') else 0
        if x['brand'] not in p['brands'] and len(p['brands']) < 6: p['brands'].append(x['brand'])
    top = sorted(par.values(), key=lambda p: (-p['n'], -p['kwc']))
    top = [p for p in top if p['n'] >= 2 or p['kwc'] >= 100][:25]
    return {'n': len(en), 'marques': len(set(x['brand'] for x in en)), 'rattachees': sum(1 for x in en if x.get('bat')), 'avec_prop': sum(1 for x in en if x.get('siren')), 'murs_tiers': sum(1 for x in en if x.get('murs_tiers')),
            'kwc_tiers': int(sum(x.get('kwc', 0) for x in en if x.get('murs_tiers'))), 'proprietaires': top}

def rapprocher_parkings(cibles, parkings, props):
    """rattache les parkings nommés aux cibles (mots significatifs du nom OSM présents dans la raison sociale ou le nom du propriétaire BDNB)"""
    if not parkings: return
    noms = {}; geo = parkings.get('geo', set())
    for e in cibles: noms[e['siren']] = _mots(e['nom']) - geo
    for _, p in props.iterrows():
        sp = None
        for e in cibles:
            if str(p['siren']) in (e.get('via') or []): sp = e['siren']; break
        if sp: noms[sp] |= (_mots(p['proprietaire']) - geo)
    for e in cibles:
        m = noms.get(e['siren'], set()); n = 0; m2 = 0; ex = ''
        if m:
            for nom, com, a, cle in parkings['tous']:
                if cle & m: n += 1; m2 += a; ex = ex or f'{nom} ({com}, {a:,} m²)'.replace(',', ' ')
        e['parkings'] = n; e['parkings_m2'] = m2; e['parking_ex'] = ex[:80]
    del parkings['tous']; parkings.pop('geo', None)
    for r in parkings['top']: r.pop('cle', None)

def ecrire_relais(path, dep, r):
    for cle, dossier, meta in (('_contacts_complet', 'contacts', 'contacts publics : FINESS (établissements sanitaires et sociaux, data.gouv.fr), répertoire national des élus (ministère de l’Intérieur), OpenStreetMap (© contributeurs OSM, ODbL)'), ('_friches_complet', 'friches', 'friches : Cartofriches (Cerema, sites référencés, data.gouv.fr)'), ('_irep_complet', 'irep', f'émissions déclarées par établissement : registre des émissions polluantes IREP {IREP_ANNEE} (Géorisques)'), ('_solaire_complet', 'solaire', 'solaire existant : OpenStreetMap (© contributeurs OSM, ODbL) et BDAPPV (Kasmi et al. 2023, Zenodo 7358126, comptage par commune)'), ('_icpe_complet', 'icpe', 'installations classées en activité : Géorisques (ministère de la Transition écologique), API installations_classees')):
        val = r.pop(cle, None)
        if val is not None:
            d = os.path.join(os.path.dirname(os.path.abspath(path)), dossier); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, dep + '.json'), 'w', encoding='utf-8') as f: json.dump({'dep': dep, 'maj': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'source': meta, 'donnees': val}, f, ensure_ascii=False, separators=(',', ':'))
            print(f'  → {dossier}/{dep}.json')
    ens = r.pop('_enseignes_complet', None)
    if ens is not None:   # enseignes sous marque : publié dans parkings/ (dossier déjà versionné par le workflow) sous le nom enseignes-<dep>.json
        d = os.path.join(os.path.dirname(os.path.abspath(path)), 'parkings'); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'enseignes-' + dep + '.json'), 'w', encoding='utf-8') as f: json.dump({'dep': dep, 'maj': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'n': len(ens), 'dmax_m': ENSEIGNE_DMAX, 'source': 'enseignes sous marque : OpenStreetMap (© contributeurs OSM, ODbL) ; propriétaire des murs = bâtiment BDNB ≥ 400 m² le plus proche, personnes morales seulement', 'donnees': ens}, f, ensure_ascii=False, separators=(',', ':'))
        print(f'  → parkings/enseignes-{dep}.json : {len(ens):,} enseignes')
    complet = r.pop('_parkings_complet', None)
    if complet is not None:   # liste complète des parkings du département, fichier séparé chargé à la demande par SuiviMarché
        d = os.path.join(os.path.dirname(os.path.abspath(path)), 'parkings'); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, dep + '.json'), 'w', encoding='utf-8') as f: json.dump({'dep': dep, 'maj': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'seuil_m2': PARKING_MIN, 'n': len(complet), 'source': 'OpenStreetMap (Overpass), © contributeurs OSM, ODbL', 'parkings': complet}, f, ensure_ascii=False, separators=(',', ':'))
        print(f'  → parkings/{dep}.json : {len(complet):,} parkings')
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f: data = json.load(f)
        except Exception as e: print('  (relais illisible, recréé :', e, ')'); data = {}
    deps = data.get('deps') if isinstance(data.get('deps'), dict) else {}
    deps[dep] = r
    meta = dict(data.get('_meta') or {})   # conserve millesime_serveur / verifie posés par le workflow
    meta.update({'app': 'SuiviMarché', 'source': 'BDNB (CSTB, data.gouv.fr) — prospects_bdnb.py', 'maj': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                 'millesime': r['millesime'], 'hypotheses': {'emprise_min_m2': EMPRISE_MIN, 'part_toiture': PART_TOITURE, 'm2_par_kwc': M2_PAR_KWC, 'productible_kwh_kwc': PRODUCTIBLE, 'kwc_min': KWC_MIN},
                 'departements': len(deps)})
    data = {'_meta': meta, 'deps': dict(sorted(deps.items()))}
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path); print(f'→ relais {path} : {len(deps)} département(s)')

def lire(z, nom, usecols=None, dtype=str):
    with z.open('csv/' + nom) as f:
        return pd.read_csv(io.TextIOWrapper(f, encoding='utf-8'), sep=';', usecols=usecols, dtype=dtype, na_filter=True, low_memory=False)

def num(s):
    return pd.to_numeric(s, errors='coerce')

def liste_txt(v):
    """champs BD TOPO du type '[ "Agricole", "Industriel" ]' -> 'Agricole, Industriel'"""
    if not isinstance(v, str): return ''
    return ', '.join(re.findall(r'"([^"]+)"', v)) or v.strip('[] ')

def dep_du_zip(zpath):
    m = re.search(r'dep(\w+?)_', os.path.basename(zpath)); return m.group(1) if m else '??'

def dep_du_csv(path):
    """département d'un CSV Sitadel d'après sa colonne DEP_CODE (première ligne de données)"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            head = f.readline().replace('"', '').strip().split(';'); first = f.readline().replace('"', '').strip().split(';')
        return first[head.index('DEP_CODE')] if 'DEP_CODE' in head and len(first) > head.index('DEP_CODE') else None
    except Exception: return None

HYPOTHESES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hypotheses.json')

def charger_hypotheses(path=HYPOTHESES_JSON):
    """hypothèses de sélection et de potentiel écrites par SuiviMarché (page Paramètres) ou à la main : remplacent les constantes"""
    global EMPRISE_MIN, PART_TOITURE, M2_PAR_KWC, PRODUCTIBLE, KWC_MIN
    if not os.path.exists(path): return None
    try:
        with open(path, encoding='utf-8') as f: h = json.load(f)
    except Exception as e: print('  (hypotheses.json illisible, constantes conservées :', e, ')'); return None
    def val(k, cur, lo, hi):
        v = h.get(k)
        try: v = float(v)
        except Exception: return cur
        return v if lo <= v <= hi else cur
    EMPRISE_MIN = val('emprise_min_m2', EMPRISE_MIN, 100, 5000); PART_TOITURE = val('part_toiture', PART_TOITURE, 0.1, 0.9)
    M2_PAR_KWC = val('m2_par_kwc', M2_PAR_KWC, 3, 10); PRODUCTIBLE = val('productible_kwh_kwc', PRODUCTIBLE, 700, 1500); KWC_MIN = val('kwc_min', KWC_MIN, 0, 500)
    print(f'  hypothèses ({os.path.basename(path)}) : emprise ≥ {EMPRISE_MIN:g} m², toiture {PART_TOITURE:.0%}, {M2_PAR_KWC:g} m²/kWc, {PRODUCTIBLE:g} kWh/kWc, potentiel > {KWC_MIN:g} kWc' + (f" — {h.get('par', '')} le {h.get('date', '')[:10]}" if h.get('par') else ''))
    return h

def main():
    charger_hypotheses()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('cibles', nargs='*'); ap.add_argument('--telecharger', action='store_true'); ap.add_argument('--millesime', default=BDNB_MILLESIME)
    ap.add_argument('--relais'); ap.add_argument('--sans-excel', action='store_true'); ap.add_argument('--nettoyer', action='store_true'); ap.add_argument('--dossier')
    ap.add_argument('-h', '--help', action='store_true'); ap.add_argument('--detecter', action='store_true')
    a = ap.parse_args()
    if a.help: print(__doc__); return
    if a.detecter: print(detecter_millesime('auto')); return
    if a.millesime == 'auto':
        a.millesime = detecter_millesime('auto'); print('Millésime BDNB détecté :', a.millesime)
    dossier = a.dossier or os.path.dirname(os.path.abspath(__file__)); args = a.cibles
    sitadels = {}
    for c in glob.glob(os.path.join(dossier, '*.csv')):
        d = dep_du_csv(c)
        if d and (d not in sitadels or os.path.getmtime(c) > os.path.getmtime(sitadels[d])): sitadels[d] = c
    if args and args[0].lower().endswith('.zip'):
        jobs = [(args[0], args[1] if len(args) > 1 else sitadels.get(dep_du_zip(args[0])))]
    else:
        voulus = [x.zfill(2) for x in args]
        if a.telecharger and voulus:
            jobs = []
            for d in voulus:
                try: jobs.append((telecharger(d, dossier, a.millesime), sitadels.get(d)))
                except Exception as e: print(f'Département {d} : téléchargement impossible ({e})')
        else:
            zips = sorted(glob.glob(os.path.join(dossier, '*dep*_csv.zip')))
            jobs = [(zp, sitadels.get(dep_du_zip(zp))) for zp in zips if not voulus or dep_du_zip(zp) in voulus]
    if not jobs:
        print(__doc__); print('Aucun zip BDNB trouvé dans', dossier); sys.exit(1)
    print('Zips BDNB :', ', '.join(dep_du_zip(z) for z, _ in jobs), '| CSV Sitadel disponibles :', ', '.join(sorted(sitadels)) or 'aucun')
    erreurs = 0
    for zpath, sit_path in jobs:
        try:
            traiter(zpath, sit_path, relais=a.relais, excel=not a.sans_excel)
        except Exception as e:
            erreurs += 1; print(f'Département {dep_du_zip(zpath)} : ERREUR {e!r}')
        if a.nettoyer:
            try: os.remove(zpath); print('  zip supprimé')
            except OSError: pass
    if erreurs: sys.exit(2)

def traiter(zpath, sit_path, relais=None, excel=True):
    dep = dep_du_zip(zpath)
    z = zipfile.ZipFile(zpath)
    print(f'Département {dep} — lecture de {os.path.basename(zpath)}')

    # 1. bâtiments : emprise au sol, commune
    bg = lire(z, 'batiment_groupe.csv', usecols=['batiment_groupe_id', 'code_commune_insee', 'libelle_commune_insee', 's_geom_groupe'])
    bg['emprise_m2'] = num(bg['s_geom_groupe']); bg = bg.drop(columns=['s_geom_groupe'])
    print(f'  {len(bg):,} bâtiments (groupes), emprise médiane {bg.emprise_m2.median():.0f} m²')

    # 2. usages et caractéristiques
    ffo = lire(z, 'batiment_groupe_ffo_bat.csv', usecols=['batiment_groupe_id', 'usage_niveau_1_txt', 'mat_toit_txt', 'annee_construction', 'nb_niveau'])
    syn = lire(z, 'batiment_groupe_synthese_propriete_usage.csv', usecols=['batiment_groupe_id', 'usage_principal_bdnb_open'])
    topo = lire(z, 'batiment_groupe_bdtopo_bat.csv', usecols=['batiment_groupe_id', 'l_usage_1', 'l_nature', 'hauteur_mean'])
    topo['usage_bdtopo'] = topo['l_usage_1'].map(liste_txt); topo['nature_bdtopo'] = topo['l_nature'].map(liste_txt)
    topo['hauteur_m'] = num(topo['hauteur_mean']); topo = topo.drop(columns=['l_usage_1', 'l_nature', 'hauteur_mean'])
    print('  usages BDNB :', syn['usage_principal_bdnb_open'].value_counts().head(8).to_dict())
    print('  usages BD TOPO :', topo['usage_bdtopo'].value_counts().head(8).to_dict())

    # 3. consommation électrique (Enedis DLE, kWh/an dans l'export) — dernier millésime disponible par bâtiment
    dle = lire(z, 'batiment_groupe_dle_elec_multimillesime.csv', usecols=['batiment_groupe_id', 'millesime', 'nb_pdl_pro', 'conso_pro', 'conso_tot'])
    for c in ['nb_pdl_pro', 'conso_pro', 'conso_tot']: dle[c] = num(dle[c])
    dle = dle.sort_values('millesime').groupby('batiment_groupe_id').tail(1).rename(columns={'millesime': 'annee_conso'})
    print(f'  consommation : millésime {dle.annee_conso.max()}, conso pro médiane des bâtiments avec PDL pro {dle[dle.nb_pdl_pro > 0].conso_pro.median()/1000:.1f} MWh')

    # 4. adresse
    adr = lire(z, 'batiment_groupe_adresse.csv', usecols=['batiment_groupe_id', 'libelle_adr_principale_ban']).rename(columns={'libelle_adr_principale_ban': 'adresse'})

    # 5. propriétaires (personnes morales uniquement dans la BDNB ouverte)
    prop = lire(z, 'proprietaire.csv', usecols=['personne_id', 'siren', 'forme_juridique', 'denomination', 'code_postal', 'libelle_commune'])
    rel = lire(z, 'rel_batiment_groupe_proprietaire.csv', usecols=['batiment_groupe_id', 'personne_id', 'nb_locaux_open'])
    rel['nb_locaux_open'] = num(rel['nb_locaux_open'])
    rel = rel.sort_values('nb_locaux_open', ascending=False).drop_duplicates('batiment_groupe_id')   # propriétaire principal
    rel = rel.merge(prop, on='personne_id', how='left')
    rel['proprietaire'] = rel['denomination'].fillna('').str.strip(); rel['siren'] = rel['siren'].fillna('').str.strip()
    rel['adresse_proprietaire'] = (rel['code_postal'].fillna('') + ' ' + rel['libelle_commune'].fillna('')).str.strip()
    rel = rel[['batiment_groupe_id', 'proprietaire', 'siren', 'forme_juridique', 'adresse_proprietaire']]

    # 6. contraintes patrimoniales (ABF, monuments) et raccordement HTA
    urb = lire(z, 'batiment_groupe_urbanisme.csv', usecols=['batiment_groupe_id', 'contrainte_urbanisme_ac1', 'zone_plu_bati_patrimonial', 'monument_historique', 'distance_monument_historique'])
    urb['abf'] = (num(urb['contrainte_urbanisme_ac1']).fillna(0) > 0) | (num(urb['zone_plu_bati_patrimonial']).fillna(0) > 0)
    urb['monument'] = num(urb['monument_historique']).fillna(0) > 0
    urb['dist_monument_m'] = num(urb['distance_monument_historique'])
    urb = urb[['batiment_groupe_id', 'abf', 'monument', 'dist_monument_m']]
    try:
        hthd = lire(z, 'batiment_groupe_hthd.csv', usecols=['batiment_groupe_id', 'nb_pdl']); hthd['pdl_hta'] = num(hthd['nb_pdl']); hthd = hthd[['batiment_groupe_id', 'pdl_hta']]
    except Exception: hthd = pd.DataFrame(columns=['batiment_groupe_id', 'pdl_hta'])

    # 7. permis Sitadel rattachés aux parcelles (BDNB) : chantiers récents sur le bâtiment
    try:
        sit = lire(z, 'sitadel.csv', usecols=['type_numero_dau', 'etat_avancement_projet', 'date_reelle_autorisation', 'denomination_demandeur', 'siren_demandeur', 's_loc_creee', 'indicateur_extension'])
        rps = lire(z, 'rel_parcelle_sitadel.csv', usecols=['type_numero_dau', 'parcelle_id'])
        rbp = lire(z, 'rel_batiment_groupe_parcelle.csv', usecols=['batiment_groupe_id', 'parcelle_id'])
        sit = sit.merge(rps, on='type_numero_dau').merge(rbp, on='parcelle_id')
        sit['s_loc_creee'] = num(sit['s_loc_creee']); sit = sit[sit['date_reelle_autorisation'] >= str(datetime.date.today().year - 3)]
        sit = sit.sort_values('date_reelle_autorisation').groupby('batiment_groupe_id').tail(1)
        sit = sit.rename(columns={'date_reelle_autorisation': 'permis_date', 'denomination_demandeur': 'permis_demandeur', 'siren_demandeur': 'permis_siren', 's_loc_creee': 'permis_m2_locaux', 'etat_avancement_projet': 'permis_etat'})[['batiment_groupe_id', 'permis_date', 'permis_etat', 'permis_demandeur', 'permis_siren', 'permis_m2_locaux']]
    except Exception as e:
        print('  (permis BDNB non exploités :', e, ')'); sit = pd.DataFrame(columns=['batiment_groupe_id'])

    # 8a. gaz (données locales de l'énergie GRDF/Enedis agrégées par la BDNB) : dernier millésime, consommation professionnelle en kWh → MWh
    try:
        gz = lire(z, 'batiment_groupe_dle_gaz_multimillesime.csv', usecols=['batiment_groupe_id', 'millesime', 'conso_pro', 'nb_pdl_pro'])
        gz['conso_pro'] = num(gz['conso_pro']); gz = gz[gz['conso_pro'] > 0].sort_values('millesime').groupby('batiment_groupe_id').tail(1)
        gz['conso_gaz_mwh'] = (gz['conso_pro'] / 1000).round(1); gz['annee_gaz'] = gz['millesime']; gz = gz[['batiment_groupe_id', 'conso_gaz_mwh', 'annee_gaz']]
    except Exception as e:
        print('  (gaz non exploité :', e, ')'); gz = pd.DataFrame(columns=['batiment_groupe_id', 'conso_gaz_mwh', 'annee_gaz'])
    # 8b. DPE tertiaire (ADEME via BDNB) : dernier DPE par bâtiment
    try:
        dp = lire(z, 'batiment_groupe_dpe_tertiaire.csv', usecols=['batiment_groupe_id', 'date_etablissement_dpe', 'classe_conso_energie_dpe_tertiaire', 'classe_emission_ges_dpe_tertiaire', 'conso_dpe_tertiaire_ep_m2', 'type_energie_chauffage', 'surface_utile'])
        dp = dp.sort_values('date_etablissement_dpe').groupby('batiment_groupe_id').tail(1)
        dp = dp.rename(columns={'date_etablissement_dpe': 'dpe_date', 'classe_conso_energie_dpe_tertiaire': 'dpe_classe', 'classe_emission_ges_dpe_tertiaire': 'dpe_ges', 'conso_dpe_tertiaire_ep_m2': 'dpe_kwh_m2', 'type_energie_chauffage': 'dpe_chauffage', 'surface_utile': 'dpe_surface_m2'})
        dp['dpe_kwh_m2'] = num(dp['dpe_kwh_m2']); dp['dpe_surface_m2'] = num(dp['dpe_surface_m2'])
    except Exception as e:
        print('  (DPE tertiaire non exploité :', e, ')'); dp = pd.DataFrame(columns=['batiment_groupe_id', 'dpe_date', 'dpe_classe', 'dpe_ges', 'dpe_kwh_m2', 'dpe_chauffage', 'dpe_surface_m2'])

    # 8. parcelle principale de chaque bâtiment : foncier libre autour (PV au sol, ombrières)
    try:
        rbp2 = lire(z, 'rel_batiment_groupe_parcelle.csv', usecols=['batiment_groupe_id', 'parcelle_id', 'parcelle_principale'])
        rbp2 = rbp2[rbp2['parcelle_principale'].astype(str) == '1'][['batiment_groupe_id', 'parcelle_id']]
        par = lire(z, 'parcelle.csv', usecols=['parcelle_id', 's_geom_parcelle']); par['parcelle_m2'] = num(par['s_geom_parcelle'])
        parc = rbp2.merge(par[['parcelle_id', 'parcelle_m2']], on='parcelle_id').sort_values('parcelle_m2', ascending=False).groupby('batiment_groupe_id').head(1)[['batiment_groupe_id', 'parcelle_id', 'parcelle_m2']]
    except Exception as e:
        print('  (parcelles non exploitées :', e, ')'); parc = pd.DataFrame(columns=['batiment_groupe_id', 'parcelle_id', 'parcelle_m2'])

    # ---------------- assemblage ----------------
    df = bg.merge(syn, on='batiment_groupe_id', how='left').merge(parc, on='batiment_groupe_id', how='left').merge(gz, on='batiment_groupe_id', how='left').merge(dp, on='batiment_groupe_id', how='left').merge(ffo, on='batiment_groupe_id', how='left').merge(topo, on='batiment_groupe_id', how='left') \
        .merge(dle, on='batiment_groupe_id', how='left').merge(adr, on='batiment_groupe_id', how='left').merge(rel, on='batiment_groupe_id', how='left') \
        .merge(urb, on='batiment_groupe_id', how='left').merge(hthd, on='batiment_groupe_id', how='left').merge(sit, on='batiment_groupe_id', how='left')
    usage = (df['usage_principal_bdnb_open'].fillna('') + ' | ' + df['usage_niveau_1_txt'].fillna('') + ' | ' + df['usage_bdtopo'].fillna('')).str.lower()
    pro = usage.apply(lambda u: any(k in u for k in USAGES_PRO))
    df['usage'] = df['usage_principal_bdnb_open'].fillna(df['usage_niveau_1_txt']).fillna(df['usage_bdtopo'])
    df['kwc_potentiel'] = (df['emprise_m2'] * PART_TOITURE / M2_PAR_KWC).round(0)
    df['production_mwh'] = (df['kwc_potentiel'] * PRODUCTIBLE / 1000).round(1)
    df['conso_pro_mwh'] = (df['conso_pro'] / 1000).round(1)
    df['couverture_conso_pct'] = (df['production_mwh'] / df['conso_pro_mwh'] * 100).where(df['conso_pro_mwh'] > 0).round(0)
    cand = df[pro & (df['emprise_m2'] >= EMPRISE_MIN) & (df['kwc_potentiel'] > KWC_MIN)].copy()
    print(f'  {len(cand):,} bâtiments professionnels d’emprise ≥ {EMPRISE_MIN} m² (potentiel > {KWC_MIN} kWc)')

    # ---------------- score ----------------
    s_toit = (cand['kwc_potentiel'].clip(upper=500) / 500 * 40)                                   # 0-40 : taille de toiture
    cov = cand['couverture_conso_pct']
    s_conso = pd.Series(0.0, index=cand.index)
    s_conso[cov.notna() & (cov <= 60)] = 30                                                      # gros consommateur : tout est autoconsommé
    s_conso[cov.notna() & (cov > 60) & (cov <= 120)] = 22
    s_conso[cov.notna() & (cov > 120)] = 10
    s_conso[cov.isna() & (cand['nb_pdl_pro'].fillna(0) > 0)] = 8
    s_prop = pd.Series(0.0, index=cand.index); s_prop[cand['siren'].fillna('') != ''] = 20      # propriétaire identifié (SIREN)
    s_bonus = pd.Series(0.0, index=cand.index)
    s_bonus[num(cand['pdl_hta']).fillna(0) > 0] += 5                                                  # raccordé en HTA
    s_bonus[cand['permis_date'].notna()] += 5                                                    # chantier récent
    s_malus = pd.Series(0.0, index=cand.index)
    abf = cand['abf'].fillna(False).astype(bool); mon = cand['monument'].fillna(False).astype(bool)
    s_malus[abf & ~mon] += 15; s_malus[mon] += 30                                                # contraintes patrimoniales
    cand['score'] = (s_toit + s_conso + s_prop + s_bonus - s_malus).clip(0, 100).round(0)
    cand['lien_annuaire'] = cand['siren'].apply(lambda s: f'https://annuaire-entreprises.data.gouv.fr/entreprise/{s}' if s else '')
    cand['contrainte'] = ['monument historique' if m else ('secteur protégé (ABF)' if a else '') for a, m in zip(abf, mon)]
    cand = cand.sort_values(['score', 'kwc_potentiel'], ascending=False)

    # 9. position des bâtiments retenus : premier anneau de la géométrie (Lambert-93) → centroïde → WGS84 ; lu par morceaux pour ne garder que les candidats
    try:
        ids = set(cand['batiment_groupe_id']); pos = {}; parts = {}
        with z.open('csv/batiment_groupe.csv') as f:
            for chunk in pd.read_csv(io.TextIOWrapper(f, encoding='utf-8'), sep=';', usecols=['batiment_groupe_id', 'geom_groupe'], dtype=str, chunksize=200000):
                ch = chunk[chunk['batiment_groupe_id'].isin(ids)]
                for bid, g in zip(ch['batiment_groupe_id'], ch['geom_groupe'].fillna('')):
                    # un groupe peut être un MULTIPOLYGON (plusieurs corps de bâtiment) : le repère est le centroïde (formule des trapèzes) du plus grand
                    # anneau extérieur, et non la moyenne des sommets du premier polygone, qui plaçait l'emprise totale sur une annexe (Alstom Petite-Forêt)
                    meilleur = None
                    for ring in re.findall(r'\(\(([^()]+)\)', g):
                        pts = [tuple(map(float, q.split()[:2])) for q in ring.split(',') if len(q.split()) >= 2]
                        if len(pts) < 3: continue
                        a2 = 0.0; cx = 0.0; cy = 0.0
                        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
                            w = x0 * y1 - x1 * y0; a2 += w; cx += (x0 + x1) * w; cy += (y0 + y1) * w
                        if abs(a2) < 1e-6: continue
                        aire = abs(a2) / 2; c = (cx / (3 * a2), cy / (3 * a2))
                        if meilleur is None or aire > meilleur[0]: meilleur = (aire, c, len(re.findall(r'\(\(', g)))
                    if meilleur is None: continue
                    pos[bid] = lambert93_vers_wgs84(*meilleur[1]); parts[bid] = meilleur[2]
        cand['lat'] = cand['batiment_groupe_id'].map(lambda b: round(pos[b][0], 5) if b in pos else None); cand['lon'] = cand['batiment_groupe_id'].map(lambda b: round(pos[b][1], 5) if b in pos else None)
        cand['corps'] = cand['batiment_groupe_id'].map(lambda b: parts.get(b, 1))
        print(f'  Positions : {len(pos):,} bâtiments géolocalisés')
    except Exception as e:
        print('  (géométrie non exploitée :', e, ')'); cand['lat'] = None; cand['lon'] = None; cand['corps'] = 1

    cols = ['score', 'libelle_commune_insee', 'adresse', 'usage', 'nature_bdtopo', 'emprise_m2', 'corps', 'hauteur_m', 'nb_niveau', 'annee_construction', 'mat_toit_txt',
            'kwc_potentiel', 'production_mwh', 'conso_pro_mwh', 'annee_conso', 'nb_pdl_pro', 'couverture_conso_pct', 'pdl_hta', 'contrainte', 'dist_monument_m',
            'proprietaire', 'siren', 'forme_juridique', 'adresse_proprietaire', 'lien_annuaire', 'permis_date', 'permis_etat', 'permis_demandeur', 'permis_siren', 'permis_m2_locaux', 'parcelle_id', 'parcelle_m2', 'conso_gaz_mwh', 'annee_gaz', 'dpe_classe', 'dpe_ges', 'dpe_kwh_m2', 'dpe_chauffage', 'dpe_surface_m2', 'dpe_date', 'lat', 'lon', 'code_commune_insee', 'batiment_groupe_id']
    out = cand[cols].rename(columns={'libelle_commune_insee': 'commune', 'emprise_m2': 'emprise_sol_m2', 'mat_toit_txt': 'materiau_toit', 'annee_construction': 'annee_constr', 'code_commune_insee': 'insee'})

    # propriétaires multi-sites (comptes clés)
    cand['secteur'] = cand['usage'].map(secteur)
    mode1 = lambda s: (s.dropna().mode().iat[0] if s.notna().any() else '')                      # valeur la plus fréquente
    props = cand[cand['siren'].fillna('') != ''].groupby('siren', dropna=False).agg(proprietaire=('proprietaire', 'first'), forme_juridique=('forme_juridique', 'first'), adresse_proprietaire=('adresse_proprietaire', 'first'),
        batiments=('batiment_groupe_id', 'count'), emprise_m2=('emprise_m2', 'sum'), kwc_potentiel=('kwc_potentiel', 'sum'), production_mwh=('production_mwh', 'sum'),
        conso_pro_mwh=('conso_pro_mwh', 'sum'), meilleur_score=('score', 'max'), secteur=('secteur', mode1), commune_principale=('libelle_commune_insee', mode1),
        communes=('libelle_commune_insee', lambda s: ', '.join(sorted(set(s.dropna()))[:6]))).reset_index()
    props['lien_annuaire'] = 'https://annuaire-entreprises.data.gouv.fr/entreprise/' + props['siren']
    props = props.sort_values(['kwc_potentiel'], ascending=False)

    # synthèse par commune
    com = cand.groupby(['code_commune_insee', 'libelle_commune_insee']).agg(prospects=('batiment_groupe_id', 'count'), kwc_potentiel=('kwc_potentiel', 'sum'),
        production_mwh=('production_mwh', 'sum'), conso_pro_mwh=('conso_pro_mwh', 'sum'), avec_proprietaire=('siren', lambda s: int((s.fillna('') != '').sum())),
        score_moyen=('score', 'mean')).reset_index().sort_values('kwc_potentiel', ascending=False)
    com['score_moyen'] = com['score_moyen'].round(0)

    # permis Sitadel du département (CSV téléchargé depuis SuiviMarché)
    permis = None
    if sit_path and os.path.exists(sit_path):
        print('  permis Sitadel :', os.path.basename(sit_path))
        p = pd.read_csv(sit_path, sep=';', dtype=str, encoding='utf-8')
        for c in [c for c in p.columns if c.startswith('SURF_') or c == 'SUPERFICIE_TERRAIN']: p[c] = num(p[c])
        p['m2_locaux_crees'] = p['SURF_LOC_CREEE'].fillna(0)
        p['m2_agri'] = p['SURF_AGR_CREEE'].fillna(0); p['m2_indus_entrepot'] = p['SURF_IND_CREEE'].fillna(0) + p['SURF_ENT_CREEE'].fillna(0)
        p['kwc_potentiel'] = (p['m2_locaux_crees'] * PART_TOITURE / M2_PAR_KWC).round(0)
        p['adresse'] = (p['ADR_NUM_TER'].fillna('') + ' ' + p['ADR_LIBVOIE_TER'].fillna('') + ' ' + p['ADR_LIEUDIT_TER'].fillna('')).str.strip()
        p['lien_annuaire'] = p['SIREN_DEM'].fillna('').apply(lambda s: f'https://annuaire-entreprises.data.gouv.fr/entreprise/{s}' if s else '')
        etat = {'2': 'autorisé', '4': 'annulé', '5': 'commencé', '6': 'terminé'}
        p['etat'] = p['ETAT_DAU'].map(lambda v: etat.get(str(v), str(v)))
        permis = p[p['m2_locaux_crees'] > 0][['DATE_REELLE_AUTORISATION', 'etat', 'TYPE_DAU', 'DENOM_DEM', 'SIREN_DEM', 'APE_DEM', 'ADR_LOCALITE_TER', 'adresse', 'm2_locaux_crees', 'm2_agri', 'm2_indus_entrepot', 'kwc_potentiel', 'I_EXTENSION', 'SUPERFICIE_TERRAIN', 'lien_annuaire', 'NUM_DAU']] \
            .rename(columns={'DATE_REELLE_AUTORISATION': 'autorisation', 'DENOM_DEM': 'petitionnaire', 'SIREN_DEM': 'siren', 'APE_DEM': 'ape', 'ADR_LOCALITE_TER': 'commune', 'I_EXTENSION': 'extension', 'SUPERFICIE_TERRAIN': 'terrain_m2'}).sort_values('m2_locaux_crees', ascending=False)

    methode = pd.DataFrame({'Élément': [
        'Source bâtiments', 'Source consommation', 'Source propriétaires', 'Sélection', 'Potentiel kWc', 'Production', 'Couverture conso', 'Score (0-100)', 'Contraintes', 'Contact', 'Permis Sitadel', 'Limites'],
        'Détail': [
        f'BDNB {os.path.basename(zpath)} (CSTB) : emprise au sol, usage (synthèse BDNB, fichiers fonciers, BD TOPO), hauteur, matériau de toiture, année.',
        'Enedis « données locales de l’énergie » agrégées par bâtiment (conso_pro convertie en MWh/an, nombre de points de livraison professionnels, dernier millésime).',
        'Fichiers fonciers (MAJIC) : personnes morales propriétaires (dénomination, SIREN, forme juridique). Les particuliers ne sont pas publiés.',
        f'Bâtiments à usage professionnel (mots-clés {", ".join(USAGES_PRO)}) d’emprise ≥ {EMPRISE_MIN} m² et potentiel > {KWC_MIN} kWc.',
        f'emprise au sol × {PART_TOITURE:.0%} de toiture équipable ÷ {M2_PAR_KWC} m² par kWc. Ordre de grandeur à confirmer sur photo aérienne (pente, orientation, édicules).',
        f'{PRODUCTIBLE} kWh/kWc/an.',
        'production ÷ consommation professionnelle du bâtiment : ≤ 60 % = autoconsommation totale probable ; > 120 % = vente de surplus nécessaire.',
        'toiture 0-40 (500 kWc = 40) + adéquation consommation 0-30 + propriétaire identifié 20 + HTA 5 + chantier récent 5 − secteur protégé 15 − monument historique 30.',
        'BDNB urbanisme : périmètre de protection (AC1) ou zone PLU patrimoniale → avis ABF ; monument historique → quasi rédhibitoire.',
        'SIREN du propriétaire → annuaire-entreprises.data.gouv.fr (dirigeants, adresse, établissements). Pour l’exploitant, croiser avec le pétitionnaire du permis ou la raison sociale sur place.',
        'Feuille « Permis Sitadel » : autorisations créant des locaux non résidentiels (SDES), potentiel calculé sur la surface de plancher créée : les toitures neuves sont les meilleurs prospects (pose en construction).',
        'La BDNB ne connaît pas les installations PV existantes : croiser avec « Mes centrales » / la veille de SuiviMarché (registre RTE) avant contact. Emprise = surface au sol, pas surface de toiture.']})

    if relais:
        ecrire_relais(relais, dep, resume_departement(dep, zpath, out, props, com, permis))
    xlsx = os.path.join(os.path.dirname(os.path.abspath(zpath)), f'prospects_{dep}.xlsx')
    if not excel: pass
    else:
      try:
        with pd.ExcelWriter(xlsx, engine='openpyxl') as w:
            out.to_excel(w, sheet_name='Prospects', index=False)
            props.to_excel(w, sheet_name='Propriétaires', index=False)
            com.to_excel(w, sheet_name='Synthèse communes', index=False)
            if permis is not None: permis.to_excel(w, sheet_name='Permis Sitadel', index=False)
            methode.to_excel(w, sheet_name='Méthode', index=False)
            for ws in w.sheets.values():
                ws.freeze_panes = 'A2'; ws.auto_filter.ref = ws.dimensions
                for col in ws.columns:
                    width = max(10, min(45, max(len(str(c.value)) if c.value is not None else 0 for c in col[:200]) + 2)); ws.column_dimensions[col[0].column_letter].width = width
        print(f'→ {xlsx}')
      except ImportError:
        base = xlsx[:-5]; out.to_csv(base + '_prospects.csv', sep=';', index=False, encoding='utf-8-sig'); props.to_csv(base + '_proprietaires.csv', sep=';', index=False, encoding='utf-8-sig')
        print('openpyxl absent : fichiers CSV écrits à la place →', base + '_*.csv')

    tot_kwc = out['kwc_potentiel'].sum()
    print(f'  Prospects : {len(out):,} bâtiments · {tot_kwc/1000:,.1f} MWc de potentiel · {out["production_mwh"].sum()/1000:,.1f} GWh/an')
    print(f'  Avec propriétaire identifié : {(out["siren"].fillna("") != "").sum():,} · score ≥ 60 : {(out["score"] >= 60).sum():,} · secteur protégé : {(out["contrainte"] != "").sum():,}')
    print(f'  Propriétaires multi-sites (≥ 3 bâtiments) : {(props["batiments"] >= 3).sum():,}')
    if permis is not None: print(f'  Permis Sitadel : {len(permis):,} autorisations, {permis["m2_locaux_crees"].sum()/1e3:,.0f} milliers de m² de locaux créés')

if __name__ == '__main__':
    main()
