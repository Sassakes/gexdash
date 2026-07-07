# NQ GEX Terminal

Niveaux gamma (call/put walls, gamma flip, top strikes, expected move straddle ATM)
depuis la chaîne d'options NDX (CBOE delayed, gratuit), convertis à l'échelle NQ,
avec dashboard + bouton **Refresh live** à la demande.

## Architecture

```
GitHub Actions (cron pré-open, jours ouvrés)
  └─ gex_levels.py → nq_levels.json + history.json commités au repo
       └─ chaque commit déclenche un redéploiement Vercel automatique

Vercel
  ├─ index.html (statique)         → le dashboard
  ├─ nq_levels.json / history.json → snapshot quotidien + historique 120 jours
  └─ /api/gex (Python serverless)  → recalcul LIVE à la demande (bouton refresh)
```

Le calcul est partagé : `api/_gex_core.py` est importé à la fois par la fonction
serverless et par le CLI `gex_levels.py`. La basis passe par l'API Yahoo en HTTP
direct (plus de dépendance yfinance).

## Déploiement (une fois)

1. Crée un repo GitHub et pousse ces fichiers.
2. **GitHub → Settings → Actions → General → Workflow permissions** :
   coche **Read and write permissions** (le bot commit les JSON).
3. **Vercel → Add New Project → Import** ce repo.
   Framework preset : **Other**. Build command : *(vide)*. Output directory : *(vide)*.
   Deploy.
4. **Sous-domaine** : Vercel → ton projet → **Settings → Domains** →
   ajoute `gex.tondomaine.com`. Si le domaine est déjà géré par Vercel
   (autre projet), c'est instantané ; sinon ajoute chez ton registrar un
   CNAME `gex` → `cname.vercel-dns.com`.
5. GitHub → onglet **Actions** → *GEX daily levels* → **Run workflow**
   pour le premier snapshot (basis manuelle optionnelle en input).

Ensuite : deux crons (12:45 & 13:45 UTC, lun–ven, couvrent été/hiver) commitent
le snapshot ~45 min avant l'open US → Vercel redéploie → dashboard à jour.
Le bouton **⟳ Refresh live** recalcule tout à n'importe quel moment via
`/api/gex` (champ basis optionnel, ex: `145.5`, sinon basis Yahoo live).

## Local

```bash
pip install numpy requests
python gex_levels.py --selftest              # test hors réseau
python gex_levels.py --out .                 # snapshot réel
python gex_levels.py --basis 145.5 --out .   # basis calibrée RTH open
python -m http.server 8000                   # dashboard (sans /api/gex)
# ou : npm i -g vercel && vercel dev         # dashboard AVEC /api/gex en local
```

## Supabase (optionnel)

`--push-supabase` + secrets `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.
La table `gex_levels` doit avoir une contrainte UNIQUE sur `date`.

## Notes

- CBOE delayed ≈ 15 min : même le « live » est un snapshot retardé, pas du tick.
- Le gamma flip peut légitimement être absent (régime gamma fortement positif).
- Walls convention SpotGamma : max/min de GEX net sur **toutes** les strikes.
- `/api/gex` : maxDuration 60 s (vercel.json) — la chaîne NDX complète est lourde,
  le premier appel à froid peut prendre quelques secondes.
- Usage éducatif, pas un conseil financier.
