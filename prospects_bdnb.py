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
import sys, os, re, io, glob, zipfile, datetime, json, argparse, urllib.request, shutil
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
        'proprietaires': [{'siren': str(p['siren']), 'nom': str(p['proprietaire'])[:60], 'n': int(p['batiments']), 'kwc': int(p['kwc_potentiel'])} for _, p in props.head(5).iterrows()],
    }
    if permis is not None:
        r['permis'] = {'n': int(len(permis)), 'm2_locaux': int(permis['m2_locaux_crees'].sum()), 'm2_agri': int(permis['m2_agri'].sum()), 'm2_indus': int(permis['m2_indus_entrepot'].sum())}
    return r

def ecrire_relais(path, dep, r):
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

def main():
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

    # ---------------- assemblage ----------------
    df = bg.merge(syn, on='batiment_groupe_id', how='left').merge(ffo, on='batiment_groupe_id', how='left').merge(topo, on='batiment_groupe_id', how='left') \
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

    cols = ['score', 'libelle_commune_insee', 'adresse', 'usage', 'nature_bdtopo', 'emprise_m2', 'hauteur_m', 'nb_niveau', 'annee_construction', 'mat_toit_txt',
            'kwc_potentiel', 'production_mwh', 'conso_pro_mwh', 'annee_conso', 'nb_pdl_pro', 'couverture_conso_pct', 'pdl_hta', 'contrainte', 'dist_monument_m',
            'proprietaire', 'siren', 'forme_juridique', 'adresse_proprietaire', 'lien_annuaire', 'permis_date', 'permis_etat', 'permis_demandeur', 'permis_m2_locaux', 'code_commune_insee', 'batiment_groupe_id']
    out = cand[cols].rename(columns={'libelle_commune_insee': 'commune', 'emprise_m2': 'emprise_sol_m2', 'mat_toit_txt': 'materiau_toit', 'annee_construction': 'annee_constr', 'code_commune_insee': 'insee'})

    # propriétaires multi-sites (comptes clés)
    props = cand[cand['siren'].fillna('') != ''].groupby('siren', dropna=False).agg(proprietaire=('proprietaire', 'first'), forme_juridique=('forme_juridique', 'first'), adresse_proprietaire=('adresse_proprietaire', 'first'),
        batiments=('batiment_groupe_id', 'count'), emprise_m2=('emprise_m2', 'sum'), kwc_potentiel=('kwc_potentiel', 'sum'), production_mwh=('production_mwh', 'sum'),
        conso_pro_mwh=('conso_pro_mwh', 'sum'), meilleur_score=('score', 'max'), communes=('libelle_commune_insee', lambda s: ', '.join(sorted(set(s.dropna()))[:6]))).reset_index()
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
