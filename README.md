# suivimarche-data — relais de données pour SuiviMarché

Dépôt public alimenté chaque matin par GitHub Actions (`relais.py`, bibliothèque standard Python, aucune clé d'API). L'outil [SuiviMarché](https://github.com/Cocothib/suivimarche) lit ces fichiers depuis `raw.githubusercontent.com/Cocothib/suivimarche-data/main/`.

| Fichier | Contenu | Source |
|---|---|---|
| `spot.json` | prix spot day-ahead France par mois depuis 2015 (prix moyen, prix 11 h-15 h, **prix capté solaire** pondéré par la production solaire nationale, heures à prix négatif, min/max, profil horaire) et par jour sur 92 jours | [energy-charts.info](https://energy-charts.info) (Fraunhofer ISE, données ENTSO-E / EPEX), CC BY 4.0 |
| `jorf.json` | textes du Journal officiel des 24 derniers mois dont le titre concerne le solaire, l'autoconsommation ou l'électricité renouvelable (`tag` = `pv` ou `energie`), avec lien Légifrance `https://www.legifrance.gouv.fr/jorf/id/<id>` | [DILA, open data JORF](https://echanges.dila.gouv.fr/OPENDATA/JORF/), licence ouverte |
| `etat.json` | horodatages, dernière archive JORF traitée, erreurs éventuelles | |

## Fonctionnement

- Spot : chaque exécution recalcule le mois courant et le mois précédent (les mois plus anciens sont conservés) et les 92 derniers jours. Prix horaires jusqu'en septembre 2025, puis au quart d'heure.
- JORF : les archives incrémentales publiées depuis la dernière exécution sont lues (`texte/version/JORF/*.xml`, champ `TITREFULL`) ; les nominations et actes de gestion sont exclus.
- Le workflow `relais` se lance à 05:40 UTC ; l'entrée `full` du lancement manuel force un recalcul complet.

Exécution locale : `python relais.py` (variables `RELAIS_FULL`, `RELAIS_SPOT_DEBUT`, `RELAIS_JORF_JOURS`).
