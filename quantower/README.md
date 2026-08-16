# GexDash Levels — indicateur Quantower

Trace sur un chart Quantower les niveaux du terminal gexdash (walls, gamma
flip, HGEX, Max Pain, Expected Move, grille sigma ancrée sur le Daily Open).

Récupère les niveaux **automatiquement** via une clé API personnelle (une
fois collée dans les réglages, plus rien à faire), avec un champ manuel en
repli qui décode **exactement la même string** que l'indicateur TradingView
— le bouton « ⧉ Pine string » du terminal reste utilisable pour les deux
plateformes.

---

## Compilation

Quantower charge des indicateurs **compilés** (`.dll`). Ce dossier contient
les sources ; il faut les construire une fois.

### Méthode recommandée — extension Quantower Algo

C'est la voie la plus sûre : l'extension règle seule le framework cible, les
références et la copie du binaire.

1. Installer **Visual Studio Community** (charge de travail « Développement
   .NET Desktop ») puis l'extension **Quantower Algo** depuis
   *Extensions → Gérer les extensions*.
2. *Fichier → Nouveau projet* → taper « Indicator » → choisir le modèle
   d'indicateur Quantower. Nommer le projet **GexDashLevels**.
3. Remplacer le contenu du fichier `.cs` généré par celui de
   **`GexDashLevels.cs`** fourni ici.
4. **Générer → Générer la solution** (F6). L'extension copie l'indicateur
   dans Quantower automatiquement.
5. Sur un chart : *Indicators → Custom → GexDash Levels*.

### Méthode alternative — ligne de commande

Si tu préfères compiler sans Visual Studio :

1. Installer le **SDK .NET** correspondant à ta version de Quantower.
2. Ouvrir `GexDashLevels.csproj` et **adapter deux valeurs** :
   - `QuantowerDir` → ton dossier d'installation de Quantower
   - `TargetFramework` → `net8.0` (versions récentes) ou `net472` (anciennes)
3. Dans le dossier : `dotnet build -c Release`
4. La DLL est copiée automatiquement vers
   `Documents\Quantower\Settings\Scripts\Indicators\GexDashLevels\`.
5. Redémarrer Quantower.

> Si la compilation échoue sur une référence manquante, c'est que
> `QuantowerDir` ne pointe pas au bon endroit : cherche
> `TradingPlatform.BusinessLayer.dll` dans ton installation et utilise le
> dossier qui le contient.

### Éditeur intégré de Quantower (sans Visual Studio)

Ce fichier utilise `Font`/`Pen`/`SolidBrush`/`Graphics`/`FontStyle`/
`DashStyle` (`System.Drawing`) pour dessiner sur le chart — c'est le
mécanisme **officiel et unique** documenté par Quantower pour le dessin
personnalisé sur chart (`OnPaintChart` reçoit un `PaintChartEventArgs.Graphics`
qui est un `System.Drawing.Graphics` standard dans tous les exemples
officiels — pas de type de dessin « maison » Quantower alternatif). Ce n'est
donc pas quelque chose que ce fichier pourrait éviter.

Sur au moins la version **1.146.18**, l'éditeur intégré (celui qui compile
directement le `.cs` collé dans Quantower, sans passer par le `.csproj`) ne
résout pas ces types par défaut — contrairement à Visual Studio, où le
`.csproj` fourni ici règle `<UseWindowsForms>true</UseWindowsForms>`, ce qui
tire automatiquement `System.Drawing.Common` sur les cibles .NET modernes.
Je n'ai pas trouvé, dans la documentation Quantower publique, de mécanisme
documenté pour ajouter une référence d'assembly supplémentaire depuis
l'éditeur intégré lui-même (pas de directive `#r`, pas de panneau
« Références » mentionné). Si ton édition de l'éditeur intégré en propose
un (clic droit sur le script, menu, ou fichier de configuration à côté du
`.cs` dans le dossier `Indicators\GexDashLevels\`), c'est la piste à
essayer en premier. À défaut, compiler via une des deux méthodes
ci-dessus (Quantower Algo ou `dotnet build`) reste la voie qui fonctionne
de façon vérifiée pour ce fichier.

---

## Utilisation

### Mode automatique (recommandé)

Plus rien à coller chaque jour : l'indicateur récupère lui-même les niveaux
en arrière-plan.

1. Sur gexdash, ouvrir **/profile** → section **Clé API personnelle** →
   **Copier** (ou **Régénérer** si tu n'en as pas encore).
2. Dans Quantower, ouvrir les réglages de l'indicateur et coller la clé dans
   le champ **Clé API gexdash (voir /profile)**.
3. Laisser **Mode automatique** coché (c'est le réglage par défaut). Laisser
   **Marché** sur `Auto` — l'indicateur détecte NQ/ES/SPX/GC/XAU depuis le
   symbole du graphique, ou choisir une valeur pour le forcer.

C'est tout : un minuteur en arrière-plan interroge l'API toutes les 5
minutes et met à jour les niveaux affichés sans jamais bloquer le
graphique. Un petit texte en bas à gauche du chart indique l'heure de la
dernière synchronisation réussie, ou le motif d'un échec (clé invalide,
réseau injoignable, trop de requêtes…) — en cas d'échec, les derniers
niveaux valides reçus restent affichés, rien n'est jamais effacé.

### Mode manuel (repli)

Utile si le mode auto est désactivé, si aucune clé n'est saisie, ou tant
qu'un appel échoue et que le cache est encore vide.

1. Sur le terminal gexdash, sélectionner le marché (NQ / ES / SPX / GOLD).
2. Cliquer **⧉ Pine string** — la string est copiée dans le presse-papier.
3. Dans Quantower, ouvrir les réglages de l'indicateur et coller la string
   dans le champ **Niveaux (repli manuel, string gexdash)**.

À refaire une fois par jour, idéalement après la publication de 15h25.

---

## Réglages

| Réglage | Effet |
|---|---|
| Clé API gexdash | Active la récupération automatique — cf. `/profile` |
| Mode automatique | Coché = récupération auto (si une clé est saisie) ; décoché = champ manuel uniquement |
| Marché | `Auto` = détection depuis le symbole du graphique, ou valeur forcée |
| Afficher GEX | Call/Put Wall, walls 0DTE, Gamma Flip, HGEX, Max Pain |
| Afficher Expected Move | Bornes EM haute et basse |
| Afficher grille Open | Daily Open et multiples de sigma |
| Afficher les bandes EM intermédiaires | Bandes fractionnaires (±50 %, ±150 %) |
| Afficher les murs de gamma par strike | Concentrations positives et négatives |
| Afficher les bornes IV 1 jour | Amplitude implicite haute et basse |
| Afficher le prix dans l'étiquette | Ajoute la valeur numérique au libellé |
| Taille du texte / Épaisseur / Marge | Confort de lecture |
| Couleurs | Une par famille, préréglées sur la palette du terminal |

### Robustesse du mode automatique

- **401 / 403 / 429** affichés distinctement (clé invalide, clé
  bloquée/révoquée, trop de requêtes) plutôt qu'une exception.
- **Échec réseau** : les derniers niveaux valides reçus restent affichés,
  jamais d'écran vide.
- **Recul progressif** après échecs répétés (5 min → 10 → 20 … plafonné à
  60 min), remis à zéro au premier succès — jamais de boucle serrée sur une
  erreur persistante.
- L'appel réseau tourne sur un minuteur dédié, jamais dans le rendu du
  graphique.

Les niveaux structurants sont en trait plein, les repères secondaires en
pointillés. Les étiquettes se décalent automatiquement lorsque plusieurs
niveaux sont proches.

---

## Codes reconnus

`res` `sup` `res0` `sup0` `flip` `hgex` `mpain` · `emh` `eml` `emb` ·
`opo` `opu` `opd` · `gpos` `gneg` · `ivh` `ivl`

Soit les 17 codes que le moteur peut produire.
