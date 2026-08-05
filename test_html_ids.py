"""Garde anti-doublon sur les id= des pages HTML.

Un id dupliqué casse silencieusement tout getElementById() qui le cible :
JS ne voit jamais que le PREMIER élément du DOM, jamais le second. C'est
exactement le bug qui a rendu la carte #emCard muette (deux éléments
"emCard" dans index.html, l'un caché en permanence, l'autre jamais peuplé).

Usage : python test_html_ids.py
Sortie non-vide + code de sortie 1 si un doublon est trouvé.
"""
import sys
from html.parser import HTMLParser
from pathlib import Path

PAGES = ["index.html", "dash.html", "heatmap.html", "news.html", "doc.html", "admin.html"]


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []  # [(id, line)]

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append((value, self.getpos()[0]))


def find_duplicates(path):
    parser = IdCollector()
    parser.feed(Path(path).read_text(encoding="utf-8"))
    seen = {}
    dups = {}
    for id_, line in parser.ids:
        seen.setdefault(id_, []).append(line)
    for id_, lines in seen.items():
        if len(lines) > 1:
            dups[id_] = lines
    return dups


def main():
    root = Path(__file__).parent
    failed = False
    for page in PAGES:
        path = root / page
        if not path.exists():
            continue
        dups = find_duplicates(path)
        if dups:
            failed = True
            print(f"{page}: {len(dups)} id(s) dupliqué(s)")
            for id_, lines in sorted(dups.items()):
                print(f"  id=\"{id_}\"  -> lignes {', '.join(map(str, lines))}")
        else:
            print(f"{page}: OK")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
