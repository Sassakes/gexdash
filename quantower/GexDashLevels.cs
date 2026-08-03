// ═══════════════════════════════════════════════════════════════════════════
//  GexDash Levels — indicateur Quantower
//
//  Colle la string generee par le bouton "Pine string" du terminal gexdash :
//  l'indicateur la decode et trace les niveaux du jour (walls, flip, HGEX,
//  Max Pain, Expected Move et grille sigma ancree sur le Daily Open).
//
//  Format attendu (identique a celui de TradingView) :
//      prix,libelle,code;prix,libelle,code;...
//  Exemple :
//      29500.0,Call Wall,res;29180.0,Gamma Flip,flip;28800.0,Put Wall,sup
//
//  Installation :
//    1. Documents\Quantower\Settings\Scripts\Indicators\GexDashLevels\
//    2. Y deposer ce fichier, puis redemarrer Quantower (compilation auto).
//    3. Sur un chart : Indicators -> Custom -> GexDash Levels.
// ═══════════════════════════════════════════════════════════════════════════

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using TradingPlatform.BusinessLayer;

namespace GexDashLevels
{
    public class GexDashLevels : Indicator
    {
        // ───────────────────────── entrees ─────────────────────────
        [InputParameter("Niveaux (string gexdash)", 10)]
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

        // ───────────────────────── etat interne ─────────────────────────
        private sealed class Level
        {
            public double Price;
            public string Label;
            public string Kind;
        }

        private readonly List<Level> levels = new List<Level>();
        private string parsedFrom = null;   // string deja decodee (evite de reparser a chaque frame)

        public GexDashLevels()
        {
            this.Name = "GexDash Levels";
            this.Description = "Niveaux GEX / EM / Open issus du terminal gexdash";
            this.SeparateWindow = false;    // superpose au prix
        }

        protected override void OnInit()
        {
            this.parsedFrom = null;         // force un decodage au premier rendu
        }

        // ───────────────────────── decodage ─────────────────────────
        private void ParseIfNeeded()
        {
            var raw = this.LevelsInput ?? "";
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

            this.ParseIfNeeded();
            if (this.levels.Count == 0)
                return;

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
            catch (Exception ex)
            {
                Core.Instance.Loggers.Log("GexDash Levels : " + ex.Message);
            }
            finally
            {
                gr.Clip = oldClip;
            }
        }
    }
}
