// ═══════════════════════════════════════════════════════════════════════════
//  GexDash Levels — indicateur Quantower
//
//  Deux sources de niveaux, l'une repli de l'autre :
//    - Mode automatique (recommande) : colle ta cle API personnelle
//      (/profile sur gexdash) une seule fois. Un minuteur en arriere-plan
//      interroge /api/mylevels toutes les 5 min et choisit seul le marche
//      d'apres le symbole du graphique.
//    - Mode manuel (repli) : colle a la main la string generee par le
//      bouton "Pine string" du terminal gexdash. Utilise si le mode auto
//      est desactive, si aucune cle n'est saisie, ou tant qu'un appel auto
//      echoue (les derniers niveaux valides recus restent affiches).
//
//  Format d'une string de niveaux (identique a celui de TradingView) :
//      prix,libelle,code;prix,libelle,code;...
//  Exemple :
//      29500.0,Call Wall,res;29180.0,Gamma Flip,flip;28800.0,Put Wall,sup
//
//  Installation :
//    1. Documents\Quantower\Settings\Scripts\Indicators\GexDashLevels\
//    2. Y deposer ce fichier, puis redemarrer Quantower (compilation auto).
//    3. Sur un chart : Indicators -> Custom -> GexDash Levels.
//
//  Dependance System.Drawing (Font/Pen/SolidBrush/Graphics/FontStyle/
//  DashStyle) : NON evitable. La doc officielle Quantower
//  (indicator-with-custom-painting-gdi) montre PaintChartEventArgs.Graphics
//  comme un System.Drawing.Graphics standard -- c'est le SEUL mecanisme de
//  dessin sur chart documente par le SDK, il n'existe pas de type de dessin
//  "maison" Quantower alternatif. Si l'editeur integre de Quantower ne
//  resout pas ces types (SDK v1.146.18 observe), compiler via Visual Studio
//  + extension Quantower Algo (cf. README, methode deja documentee) : le
//  .csproj fourni met <UseWindowsForms>true</UseWindowsForms>, ce qui tire
//  automatiquement System.Drawing.Common sur les cibles .NET modernes. Voir
//  README pour le detail de ce qui a ete verifie vs. suppose sur ce point.
// ═══════════════════════════════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using TradingPlatform.BusinessLayer;

namespace GexDashLevels
{
    // Choix du marche pour le mode automatique. "Auto" detecte depuis le
    // symbole du graphique (meme logique que le champ "Market" du script
    // Pine) ; les autres valeurs forcent un marche donne quel que soit le
    // symbole affiche.
    public enum GexDashMarket { Auto, NQ, ES, SPX, GC, XAU }

    public class GexDashLevels : Indicator
    {
        // ───────────────────────── entrees : mode automatique ─────────────────────────
        [InputParameter("Cle API gexdash (voir /profile)", 1)]
        public string ApiKey = "";

        // Actif par defaut : des qu'une cle est collee, la recuperation
        // demarre sans reglage supplementaire. Decocher revient au champ
        // manuel ci-dessous. Sans cle saisie, ce reglage n'a aucun effet
        // (rien a interroger).
        [InputParameter("Mode automatique (recuperation via cle API)", 2)]
        public bool AutoMode = true;

        [InputParameter("Marche (Auto = detecte depuis le symbole du graphique)", 3)]
        public GexDashMarket Market = GexDashMarket.Auto;

        // ───────────────────────── entrees : repli manuel ─────────────────────────
        [InputParameter("Niveaux (repli manuel, string gexdash)", 10)]
        public string LevelsInput = "";

        [InputParameter("Afficher GEX (walls, flip, HGEX, MP)", 20)]
        public bool ShowGex = true;

        [InputParameter("Afficher Expected Move", 30)]
        public bool ShowEm = true;

        [InputParameter("Afficher grille Open (sigma)", 40)]
        public bool ShowOpen = true;

        [InputParameter("Afficher les bandes EM intermediaires", 50)]
        public bool ShowEmBands = false;

        [InputParameter("Afficher les murs de gamma par strike", 52)]
        public bool ShowGammaWalls = true;

        [InputParameter("Afficher les bornes IV 1 jour", 54)]
        public bool ShowIvBounds = false;

        [InputParameter("Afficher le prix dans l'etiquette", 60)]
        public bool ShowPrice = true;

        [InputParameter("Taille du texte", 70, 7, 20, 1, 0)]
        public int FontSize = 10;

        [InputParameter("Epaisseur des lignes", 80, 1, 4, 1, 0)]
        public int LineWidth = 1;

        [InputParameter("Marge etiquette (px depuis la droite)", 90, 0, 400, 5, 0)]
        public int LabelMargin = 70;

        // OR : les niveaux sont calcules a l'echelle du future GC. Sur un
        // graphique d'or SPOT (XAUUSD), le prix est plus bas — le future cote
        // au-dessus du comptant a cause du portage. Sans ce decalage, tous les
        // niveaux seraient trop hauts du meme montant.
        [InputParameter("Decalage global des niveaux ($)", 95, -10000, 10000, 0.1, 2)]
        public double PriceOffset = 0.0;

        [InputParameter("Couleur GEX", 100)]
        public Color GexColor = Color.FromArgb(240, 185, 11);      // or

        [InputParameter("Couleur resistance / Call Wall", 110)]
        public Color ResColor = Color.FromArgb(38, 166, 154);      // teal

        [InputParameter("Couleur support / Put Wall", 120)]
        public Color SupColor = Color.FromArgb(239, 83, 80);       // rouge

        [InputParameter("Couleur Expected Move", 130)]
        public Color EmColor = Color.FromArgb(236, 234, 228);      // blanc casse

        [InputParameter("Couleur grille Open", 140)]
        public Color OpenColor = Color.FromArgb(139, 92, 246);     // violet

        // ───────────────────────── etat interne : niveaux decodes ─────────────────────────
        private sealed class Level
        {
            public double Price;
            public string Label;
            public string Kind;
        }

        private readonly List<Level> levels = new List<Level>();
        private string parsedFrom = null;   // string deja decodee (evite de reparser a chaque frame)
        private string resolvedMarketCode = "NQ";   // marche effectif (apres resolution Auto), fige a OnInit

        // ───────────────────────── etat interne : recuperation automatique ─────────────────────────
        // Domaine de production gexdash — meme base que l'extension Chrome
        // "TheHub GEX Levels Autofill" (extension/background.js), qui suit
        // exactement le meme contrat d'API.
        private const string API_BASE = "https://gexdash.wealthbuilders.group";
        private const double POLL_MINUTES = 5.0;
        private const double BACKOFF_CAP_MINUTES = 60.0;

        // Un seul HttpClient partage par toutes les instances de l'indicateur
        // (evite l'epuisement de sockets si plusieurs charts portent
        // l'indicateur en meme temps) — jamais appele depuis OnPaintChart,
        // uniquement depuis le minuteur en arriere-plan.
        private static readonly HttpClient Http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };

        // Snapshot immuable : chaque cycle de fetch remplace la reference en
        // bloc plutot que de muter ses champs en place, pour qu'OnPaintChart
        // (thread de rendu) ne puisse jamais lire un etat partiellement mis
        // a jour depuis le thread du minuteur.
        private sealed class AutoState
        {
            public Dictionary<string, string> Pine = new Dictionary<string, string>();
            public DateTime? LastSyncUtc;   // dernier succes (niveaux recus, changes ou non)
            public bool LastOk;             // resultat du DERNIER essai (peut etre un echec avec cache encore valide)
            public string LastError;        // motif lisible du dernier echec, null si LastOk
            public int FailCount;
        }

        private volatile AutoState autoState = new AutoState();

        private readonly object timerLock = new object();
        private System.Threading.Timer pollTimer;
        private volatile bool fetchInFlight = false;

        public GexDashLevels()
        {
            this.Name = "GexDash Levels";
            this.Description = "Niveaux GEX / EM / Open issus du terminal gexdash";
            this.SeparateWindow = false;    // superpose au prix
        }

        protected override void OnInit()
        {
            this.parsedFrom = null;         // force un decodage au premier rendu
            this.resolvedMarketCode = this.ResolveMarket();

            this.StopTimer();
            if (this.AutoMode && !string.IsNullOrWhiteSpace(this.ApiKey))
                this.StartTimer();
        }

        // Hook de nettoyage confirme par la doc officielle de la classe
        // Indicator (api.quantower.com/docs/TradingPlatform.BusinessLayer.Indicator.html) :
        // "protected virtual void OnClear()". Appele au retrait de
        // l'indicateur du chart -- c'est ici, et seulement ici, que le
        // minuteur doit s'arreter pour ne jamais continuer a interroger
        // l'API apres suppression.
        protected override void OnClear()
        {
            this.StopTimer();
            base.OnClear();
        }

        // ───────────────────────── selection du marche ─────────────────────────
        // Meme ordre de priorite que le champ "Market" du script Pine
        // (gex_levels.pine) : or futures d'abord (GC/MGC), puis or comptant,
        // puis NQ, SPX, ES ; NQ par defaut si rien ne correspond.
        private string ResolveMarket()
        {
            if (this.Market != GexDashMarket.Auto)
                return this.Market.ToString();

            string sym;
            try
            {
                // this.Symbol est fige a la pose de l'indicateur sur le chart —
                // resolu une seule fois ici, jamais depuis le thread du
                // minuteur en arriere-plan.
                sym = ((this.Symbol?.Name ?? "") + " " + (this.Symbol?.Id ?? "")).ToUpperInvariant();
            }
            catch
            {
                sym = "";
            }

            if (sym.Contains("GC")) return "GC";               // GC / MGC futures
            if (sym.Contains("XAU") || sym.Contains("GOLD")) return "XAU";
            if (sym.Contains("NQ") || sym.Contains("NDX") || sym.Contains("US100") || sym.Contains("USTEC")) return "NQ";
            if (sym.Contains("SPX") || sym.Contains("SP500")) return "SPX";
            if (sym.Contains("ES") || sym.Contains("US500")) return "ES";
            return "NQ";
        }

        // ───────────────────────── minuteur en arriere-plan ─────────────────────────
        // JAMAIS d'appel reseau dans OnPaintChart (appele plusieurs fois par
        // seconde) : le minuteur tourne independamment et ne fait que remplir
        // un cache (autoState) qu'OnPaintChart se contente de lire.
        private void StartTimer()
        {
            lock (this.timerLock)
            {
                if (this.pollTimer != null)
                    return;
                // Premier tir immediat (delai zero), les suivants sont
                // replanifies par ScheduleNext a la fin de chaque cycle —
                // timer non-recurrent pour pouvoir appliquer le recul
                // progressif apres un echec.
                this.pollTimer = new System.Threading.Timer(this.OnTimerTick, null,
                    TimeSpan.Zero, Timeout.InfiniteTimeSpan);
            }
        }

        private void StopTimer()
        {
            lock (this.timerLock)
            {
                this.pollTimer?.Dispose();
                this.pollTimer = null;
            }
        }

        private void ScheduleNext(double minutes)
        {
            lock (this.timerLock)
            {
                if (this.pollTimer == null)
                    return;             // minuteur arrete entre-temps (indicateur retire)
                try { this.pollTimer.Change(TimeSpan.FromMinutes(minutes), Timeout.InfiniteTimeSpan); }
                catch (ObjectDisposedException) { /* course avec StopTimer : sans consequence */ }
            }
        }

        private void OnTimerTick(object _)
        {
            if (this.fetchInFlight)
            {
                // Un appel precedent traine encore (reseau lent) : on ne
                // l'empile pas, on se contente de reprogrammer normalement.
                this.ScheduleNext(POLL_MINUTES);
                return;
            }
            this.fetchInFlight = true;
            _ = this.FetchOnceAsync();
        }

        private async Task FetchOnceAsync()
        {
            try
            {
                string url = API_BASE + "/api/mylevels?key=" + Uri.EscapeDataString(this.ApiKey ?? "");
                HttpResponseMessage resp;
                string body;
                try
                {
                    resp = await Http.GetAsync(url).ConfigureAwait(false);
                    body = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    this.OnFetchFailed(ErrorLabel(0, ex.Message));
                    return;
                }

                if (!resp.IsSuccessStatusCode)
                {
                    string apiErr = TryReadError(body);
                    this.OnFetchFailed(ErrorLabel((int)resp.StatusCode, apiErr));
                    return;
                }

                if (!TryParseLevels(body, out var pine))
                {
                    this.OnFetchFailed("reponse illisible");
                    return;
                }

                this.OnFetchSucceeded(pine);
            }
            catch (Exception ex)
            {
                // Filet de securite : une exception non prevue ici ne doit
                // jamais faire mourir le minuteur silencieusement.
                this.OnFetchFailed(ErrorLabel(-1, ex.Message));
            }
            finally
            {
                this.fetchInFlight = false;
            }
        }

        private static string ErrorLabel(int status, string detail)
        {
            switch (status)
            {
                case 401: return "cle API invalide ou manquante";
                case 403: return "acces bloque (cle desactivee ou revoquee)";
                case 429: return "trop de requetes, patience";
                case 0: return "reseau injoignable" + (string.IsNullOrEmpty(detail) ? "" : " (" + detail + ")");
                default: return string.IsNullOrEmpty(detail) ? ("erreur " + status) : detail;
            }
        }

        private static string TryReadError(string body)
        {
            try
            {
                using (var doc = JsonDocument.Parse(body))
                {
                    if (doc.RootElement.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String)
                        return e.GetString();
                }
            }
            catch { /* corps non-JSON (ex: page d'erreur brute) */ }
            return null;
        }

        // Contrat attendu (/api/mylevels) :
        //   {"levels": {"NQ": {"pine": "...", "published_utc": "..."}, "ES": {...}, ...},
        //    "hash": "..."}
        // Le hash n'est pas utilise cote indicateur : la comparaison utile
        // ici est deja faite par ParseIfNeeded (compare la string du marche
        // choisi, pas le lot entier des 5 marches).
        private static bool TryParseLevels(string body, out Dictionary<string, string> pine)
        {
            pine = new Dictionary<string, string>();
            try
            {
                using (var doc = JsonDocument.Parse(body))
                {
                    if (doc.RootElement.TryGetProperty("levels", out var levelsEl) &&
                        levelsEl.ValueKind == JsonValueKind.Object)
                    {
                        foreach (var prop in levelsEl.EnumerateObject())
                        {
                            if (prop.Value.ValueKind != JsonValueKind.Object)
                                continue;
                            if (prop.Value.TryGetProperty("pine", out var p) && p.ValueKind == JsonValueKind.String)
                                pine[prop.Name.ToUpperInvariant()] = p.GetString() ?? "";
                        }
                    }
                }
                return pine.Count > 0;
            }
            catch
            {
                return false;
            }
        }

        private void OnFetchSucceeded(Dictionary<string, string> pine)
        {
            this.autoState = new AutoState
            {
                Pine = pine,
                LastSyncUtc = DateTime.UtcNow,
                LastOk = true,
                LastError = null,
                FailCount = 0,
            };
            this.ScheduleNext(POLL_MINUTES);
        }

        private void OnFetchFailed(string reason)
        {
            var prev = this.autoState;
            int failCount = prev.FailCount + 1;
            this.autoState = new AutoState
            {
                Pine = prev.Pine,               // derniers niveaux valides conserves, jamais efface
                LastSyncUtc = prev.LastSyncUtc,
                LastOk = false,
                LastError = reason,
                FailCount = failCount,
            };
            double backoffMin = Math.Min(POLL_MINUTES * Math.Pow(2, failCount - 1), BACKOFF_CAP_MINUTES);
            this.ScheduleNext(backoffMin);
        }

        // ───────────────────────── source effective des niveaux ─────────────────────────
        private string EffectiveLevelsString()
        {
            if (this.AutoMode && !string.IsNullOrWhiteSpace(this.ApiKey))
            {
                var snap = this.autoState;
                if (snap.Pine.TryGetValue(this.resolvedMarketCode, out var pine) && !string.IsNullOrWhiteSpace(pine))
                    return pine;
            }
            return this.LevelsInput ?? "";
        }

        // ───────────────────────── decodage ─────────────────────────
        private void ParseIfNeeded()
        {
            var raw = this.EffectiveLevelsString();
            if (raw == this.parsedFrom)
                return;

            this.parsedFrom = raw;
            this.levels.Clear();

            foreach (var chunk in raw.Split(';'))
            {
                var part = chunk.Trim();
                if (part.Length == 0)
                    continue;

                // prix,libelle,code — le libelle peut contenir des espaces
                var f = part.Split(',');
                if (f.Length < 3)
                    continue;

                // Le point decimal est TOUJOURS un point dans la string :
                // on impose la culture invariante, sinon une machine en
                // francais lirait "29500.0" comme 295000.
                if (!double.TryParse(f[0].Trim(), NumberStyles.Float,
                                     CultureInfo.InvariantCulture, out double price))
                    continue;

                this.levels.Add(new Level
                {
                    Price = price,
                    Label = f[1].Trim(),
                    Kind = f[2].Trim().ToLowerInvariant(),
                });
            }
        }

        private bool IsVisible(string kind)
        {
            switch (kind)
            {
                case "res":
                case "sup":
                case "res0":
                case "sup0":
                case "flip":
                case "hgex":
                case "mpain":
                    return this.ShowGex;

                case "emh":
                case "eml":
                    return this.ShowEm;

                case "emb":
                    return this.ShowEm && this.ShowEmBands;

                case "opo":
                case "opu":
                case "opd":
                    return this.ShowOpen;

                // murs de gamma issus du profil par strike
                case "gpos":
                case "gneg":
                    return this.ShowGammaWalls;

                // amplitude 1 jour implicite (borne haute / basse)
                case "ivh":
                case "ivl":
                    return this.ShowIvBounds;

                default:
                    return true;
            }
        }

        private Color ColorFor(string kind)
        {
            switch (kind)
            {
                case "res":
                case "res0":
                    return this.ResColor;

                case "sup":
                case "sup0":
                    return this.SupColor;

                case "flip":
                case "hgex":
                case "mpain":
                    return this.GexColor;

                case "emh":
                case "eml":
                case "emb":
                    return this.EmColor;

                case "opo":
                    return this.GexColor;      // l'ancre se distingue de sa grille

                case "opu":
                case "opd":
                    return this.OpenColor;

                case "gpos":
                    return this.ResColor;      // gamma positif : amortisseur
                case "gneg":
                    return this.SupColor;      // gamma negatif : accelerateur

                case "ivh":
                case "ivl":
                    return Color.FromArgb(138, 138, 148);

                default:
                    return Color.Gray;
            }
        }

        // Traits pleins pour les niveaux structurants, pointilles pour les
        // reperes secondaires : la hierarchie se lit sans avoir a comparer
        // les couleurs.
        private static bool IsDashed(string kind)
        {
            return kind == "emb" || kind == "opu" || kind == "opd"
                || kind == "res0" || kind == "sup0"
                || kind == "ivh" || kind == "ivl";
        }

        // ───────────────────────── rendu ─────────────────────────
        public override void OnPaintChart(PaintChartEventArgs args)
        {
            base.OnPaintChart(args);

            this.ParseIfNeeded();   // lit uniquement le cache deja rempli par le minuteur

            var chart = this.CurrentChart;
            if (chart == null)
                return;

            var wnd = chart.MainWindow;
            if (wnd == null)
                return;

            var gr = args.Graphics;
            var rect = wnd.ClientRectangle;

            // On restreint le dessin a la zone du graphique : sans cela les
            // lignes deborderaient sur l'echelle de prix et l'axe du temps.
            var oldClip = gr.Clip;
            gr.SetClip(rect);

            try
            {
                if (this.levels.Count > 0)
                {
                    using (var font = new Font("Segoe UI", this.FontSize, FontStyle.Regular))
                    {
                        float labelX = rect.Right - this.LabelMargin;
                        var taken = new List<float>();      // anti-chevauchement

                        foreach (var lv in this.levels)
                        {
                            if (!IsVisible(lv.Kind))
                                continue;

                            double price = lv.Price - this.PriceOffset;
                            double yd = wnd.CoordinatesConverter.GetChartY(price);
                            if (double.IsNaN(yd) || double.IsInfinity(yd))
                                continue;

                            float y = (float)yd;
                            if (y < rect.Top - 2 || y > rect.Bottom + 2)
                                continue;                   // hors ecran : rien a tracer

                            var color = ColorFor(lv.Kind);

                            using (var pen = new Pen(color, this.LineWidth))
                            {
                                if (IsDashed(lv.Kind))
                                {
                                    pen.DashStyle = DashStyle.Dash;
                                    pen.DashPattern = new float[] { 4f, 4f };
                                }
                                gr.DrawLine(pen, rect.Left, y, rect.Right, y);
                            }

                            // Etiquette : on la decale si une autre occupe deja la
                            // place, plutot que de superposer deux textes illisibles.
                            string text = this.ShowPrice
                                ? string.Format(CultureInfo.InvariantCulture, "{0}  {1:F1}",
                                                lv.Label, price)
                                : lv.Label;

                            float ty = y - font.Height - 1f;
                            int guard = 0;
                            while (guard++ < 20)
                            {
                                bool clash = false;
                                foreach (var t in taken)
                                {
                                    if (Math.Abs(t - ty) < font.Height)
                                    {
                                        clash = true;
                                        break;
                                    }
                                }
                                if (!clash)
                                    break;
                                ty -= font.Height;
                            }
                            taken.Add(ty);

                            var size = gr.MeasureString(text, font);
                            float tx = labelX - size.Width;
                            if (tx < rect.Left + 2)
                                tx = rect.Left + 2;

                            // Fond semi-opaque : le texte reste lisible par-dessus
                            // les bougies, quelle que soit la densite du chart.
                            using (var bg = new SolidBrush(Color.FromArgb(170, 10, 10, 12)))
                                gr.FillRectangle(bg, tx - 3, ty, size.Width + 6, size.Height);

                            using (var brush = new SolidBrush(color))
                                gr.DrawString(text, font, brush, tx, ty);
                        }
                    }
                }

                this.PaintAutoStatus(gr, rect);
            }
            catch (Exception ex)
            {
                Core.Instance.Loggers.Log("GexDash Levels : " + ex.Message);
            }
            finally
            {
                gr.Clip = oldClip;
            }
        }

        // Discret, coin bas-gauche : heure de derniere synchro reussie, ou
        // motif d'echec si aucun niveau n'a encore pu etre recupere. Pas
        // affiche en mode manuel pur (rien a synchroniser).
        private void PaintAutoStatus(Graphics gr, Rectangle rect)
        {
            if (!this.AutoMode || string.IsNullOrWhiteSpace(this.ApiKey))
                return;

            var snap = this.autoState;
            string status;
            Color color;

            if (snap.LastSyncUtc.HasValue)
            {
                var local = snap.LastSyncUtc.Value.ToLocalTime();
                status = string.Format(CultureInfo.InvariantCulture, "GexDash {0} — sync {1:HH:mm}",
                    this.resolvedMarketCode, local);
                if (!snap.LastOk)
                {
                    status += " (derniers niveaux valides conserves — echec : " + snap.LastError + ")";
                    color = Color.FromArgb(220, 170, 60);
                }
                else
                {
                    color = Color.FromArgb(140, 140, 150);
                }
            }
            else
            {
                status = "GexDash " + this.resolvedMarketCode + " — "
                    + (snap.LastError ?? "synchronisation en cours…");
                color = Color.FromArgb(220, 90, 90);
            }

            using (var font = new Font("Segoe UI", Math.Max(7, this.FontSize - 2), FontStyle.Italic))
            using (var brush = new SolidBrush(color))
            {
                gr.DrawString(status, font, brush, rect.Left + 4, rect.Bottom - font.Height - 4);
            }
        }
    }
}
