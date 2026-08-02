# GexDash Levels — indicateur Quantower

Trace sur un chart Quantower les niveaux du terminal gexdash (walls, gamma
flip, HGEX, Max Pain, Expected Move, grille sigma ancrée sur le Daily Open).

Il décode **exactement la même string** que l'indicateur TradingView : le
bouton « ⧉ Pine string » du terminal sert donc aux deux plateformes.

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

---

## Utilisation

1. Sur le terminal gexdash, sélectionner le marché (NQ / ES / SPX / GOLD).
2. Cliquer **⧉ Pine string** — la string est copiée dans le presse-papier.
3. Dans Quantower, ouvrir les réglages de l'indicateur et coller la string
   dans le champ **Niveaux (string gexdash)**.

À refaire une fois par jour, idéalement après la publication de 15h25.

---

## Réglages

| Réglage | Effet |
|---|---|
| Afficher GEX | Call/Put Wall, walls 0DTE, Gamma Flip, HGEX, Max Pain |
| Afficher Expected Move | Bornes EM haute et basse |
| Afficher grille Open | Daily Open et multiples de sigma |
| Afficher les bandes EM intermédiaires | Bandes fractionnaires (±50 %, ±150 %) |
| Afficher les murs de gamma par strike | Concentrations positives et négatives |
| Afficher les bornes IV 1 jour | Amplitude implicite haute et basse |
| Afficher le prix dans l'étiquette | Ajoute la valeur numérique au libellé |
| Taille du texte / Épaisseur / Marge | Confort de lecture |
| Couleurs | Une par famille, préréglées sur la palette du terminal |

Les niveaux structurants sont en trait plein, les repères secondaires en
pointillés. Les étiquettes se décalent automatiquement lorsque plusieurs
niveaux sont proches.

---

## Codes reconnus

`res` `sup` `res0` `sup0` `flip` `hgex` `mpain` · `emh` `eml` `emb` ·
`opo` `opu` `opd` · `gpos` `gneg` · `ivh` `ivl`

Soit les 17 codes que le moteur peut produire.
