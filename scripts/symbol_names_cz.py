"""České názvy symbolů OCAD sad (ISOM 2017-2, ISSprOM 2019).

Základ vychází z oficiálních českých překladů specifikací ČSOS. Podsymboly,
které ve specifikaci nejsou (minimální velikosti, varianty linií, tagy,
obrysy, multilevel), mají odvozený název a příznak ``R`` (= ke kontrole).

Formát řádku: ``kód|název`` nebo ``kód|název|R``.
Zdroj pravdy za běhu je ``configs/oom/symbol_catalog.yaml``; tento soubor
slouží jen jako počáteční naplnění katalogu skriptem ``build_symbol_sets.py``.
"""

from __future__ import annotations

_ISOM = """
101.0|Základní vrstevnice
101.1|Základní vrstevnice – spádnice|R
101.2|Základní vrstevnice (kupa, minimální velikost)|R
101.3|Základní vrstevnice (prohlubeň, minimální velikost)|R
102.0|Zdůrazněná vrstevnice
102.1|Zdůrazněná vrstevnice (kupa, minimální velikost)|R
102.2|Zdůrazněná vrstevnice (prohlubeň, minimální velikost)|R
102.3|Popisek zdůrazněné vrstevnice|R
103.0|Doplňková vrstevnice
103.1|Doplňková vrstevnice – spádnice|R
103.2|Doplňková vrstevnice (kupa, minimální velikost)|R
103.3|Doplňková vrstevnice (prohlubeň, minimální velikost)|R
104.0|Zemní sráz
104.1|Zemní sráz (hlavní linie)|R
104.2|Zemní sráz (minimální velikost, s koncovými čárkami)|R
104.3|Zemní sráz (minimální velikost, bez koncových čárek)|R
104.4|Zemní sráz (čárka)|R
105.100|Zemní val
105.101|Zemní val (minimální velikost)|R
105.200|Opěrný zemní val
106.0|Rozpadlý zemní val
106.1|Rozpadlý zemní val (minimální velikost)|R
107.0|Erozní rýha
108.0|Malá erozní rýha
109.0|Malá kupka
110.0|Malá protáhlá kupka
111.0|Malá prohlubeň
112.0|Jáma
113.0|Rozbitý povrch
113.1|Rozbitý povrch (jednotlivá tečka)|R
113.2|Rozbitý povrch (minimální velikost)|R
114.0|Velmi rozbitý povrch
114.1|Velmi rozbitý povrch (minimální velikost)|R
115.0|Výrazný terénní objekt
201.0|Nepřekonatelný sráz (plocha)|R
201.1|Nepřekonatelný sráz
201.2|Nepřekonatelný sráz bez čárek|R
201.3|Nepřekonatelný sráz (pouze čárky)|R
201.4|Nepřekonatelný sráz (minimální velikost)|R
201.5|Nepřekonatelný sráz (minimální velikost, varianta 2)|R
201.6|Nepřekonatelný sráz, jáma (minimální velikost)|R
202.0|Sráz
202.1|Sráz bez čárek|R
202.2|Sráz (minimální velikost)|R
202.3|Sráz (minimální velikost, varianta 2)|R
203.100|Kamenná jáma|R
203.101|Jeskyně|R
203.200|Nebezpečná jáma
204.0|Balvan
204.1|Střední balvan|R
205.0|Velký balvan
206.0|Obrovský balvan nebo skalní věž
207.0|Shluk balvanů
208.0|Balvanové pole
208.1|Balvanové pole (jednotlivý znak)|R
208.2|Balvanové pole (jednotlivý znak, 120 %)|R
208.3|Balvanové pole (minimální velikost)|R
209.0|Husté balvanové pole
210.0|Kamenitý povrch, pomalý běh
210.1|Kamenitý povrch (jednotlivá tečka)|R
211.0|Kamenitý povrch, chůze
212.0|Kamenitý povrch, obtížný pohyb
213.0|Písčitý povrch
214.0|Holá skála
215.0|Příkop
301.0|Nepřekonatelné vodní těleso
301.1|Nepřekonatelné vodní těleso (plocha)|R
301.2|Nepřekonatelné vodní těleso (okrajová linie)|R
301.4|Dominantní nepřekonatelné vodní těleso 70 %|R
301.5|Dominantní nepřekonatelné vodní těleso 70 % (plocha)|R
302.0|Mělké vodní těleso
302.4|Mělké vodní těleso (okrajová linie)|R
302.1|Občasné mělké vodní těleso (plocha)|R
302.2|Občasné mělké vodní těleso (okrajová linie)|R
303.0|Jáma s vodou
304.0|Překonatelný vodní tok
305.0|Malý překonatelný vodní tok
306.0|Malý / občasný vodní příkop
307.0|Nepřekonatelná bažina
307.1|Nepřekonatelná bažina (plocha)|R
307.2|Nepřekonatelná bažina (minimální velikost)|R
308.0|Bažina
308.1|Bažina (minimální velikost)|R
309.0|Úzká bažina
310.0|Nezřetelná bažina
310.1|Nezřetelná bažina (minimální velikost)|R
311.0|Studna, fontána nebo vodní nádrž
312.0|Pramen
313.0|Výrazný vodní objekt
401.0|Otevřený prostor
402.0|Otevřený prostor s rozptýlenými stromy
402.1|Otevřený prostor s rozptýlenými stromy (varianta)|R
403.0|Divoký otevřený prostor
404.0|Divoký otevřený prostor s rozptýlenými stromy
404.1|Divoký otevřený prostor s rozptýlenými stromy (varianta)|R
405.0|Les
406.0|Vegetace, pomalý běh
406.1|Vegetace, pomalý běh, jeden směr|R
407.0|Vegetace, pomalý běh, dobrá viditelnost
408.0|Vegetace, chůze|R
408.1|Vegetace, chůze, jeden směr|R
409.0|Vegetace, chůze, dobrá viditelnost
410.0|Vegetace, prodírání
410.1|Vegetace, prodírání, jeden směr|R
410.2|Živý plot|R
412.0|Obdělávaná půda
413.0|Sad
413.1|Sad (jednotlivá tečka)|R
414.0|Vinice nebo podobné kultury
415.0|Zřetelná hranice obdělávané půdy
416.0|Zřetelná hranice vegetace
416.1|Zřetelná hranice vegetace (alternativní)|R
417.0|Výrazný velký strom
418.0|Výrazný keř nebo strom
419.0|Výrazný vegetační objekt
501.0|Zpevněná plocha
501.1|Zpevněná plocha (plocha)|R
501.2|Zpevněná plocha (okraj)|R
502.0|Široká silnice (dálnice)|R
502.1|Široká silnice nad 7 m|R
502.2|Široká silnice 5–7 m|R
503.0|Silnice
504.0|Vozová cesta
505.0|Pěší cesta
506.0|Pěšina
507.0|Nezřetelná pěšina
508.0|Průsek nebo liniová trasa terénem
508.1|Průsek nebo liniová trasa terénem, dobrý běh|R
508.2|Průsek nebo liniová trasa terénem, pomalý běh|R
508.3|Průsek nebo liniová trasa terénem, obtížný pohyb|R
508.4|Průsek nebo liniová trasa terénem, normální běh|R
508.5|Průsek nebo liniová trasa terénem (krátká linie)|R
508.6|Průsek nebo liniová trasa terénem, dobrý běh (linie)|R
508.7|Průsek nebo liniová trasa terénem, pomalý běh (linie)|R
508.8|Průsek nebo liniová trasa terénem, obtížný pohyb (linie)|R
508.9|Průsek nebo liniová trasa terénem, normální běh (linie)|R
509.0|Železnice
509.1|Železnice, oblast se zákazem vstupu (linie)|R
510.0|Elektrické vedení, lanovka nebo lyžařský vlek
511.0|Hlavní elektrické vedení
511.1|Hlavní elektrické vedení (čtvercový sloup)|R
512.0|Most / tunel
512.1|Tunel (minimální velikost)|R
512.2|Lávka (minimální velikost)|R
513.100|Zeď
513.200|Opěrná zeď
514.0|Rozpadlá zeď
515.0|Nepřekonatelná zeď
516.0|Plot
517.0|Rozpadlý plot
518.0|Nepřekonatelný plot
519.0|Průchod
520.0|Oblast se zákazem vstupu
520.4|Oblast se zákazem vstupu (plocha)|R
520.1|Oblast se zákazem vstupu (obrys)|R
521.0|Budova
521.1|Velká budova|R
521.2|Velká budova (obrys)|R
521.3|Budova (minimální velikost)|R
522.0|Zastřešení
522.1|Zastřešení (obrys)|R
523.0|Zřícenina
523.1|Zřícenina (minimální velikost)|R
524.0|Vysoká věž
525.0|Malá věž
526.0|Mohyla
527.0|Krmelec
528.0|Výrazný liniový objekt
529.0|Výrazný nepřekonatelný liniový objekt
530.0|Výrazný umělý objekt – kroužek
531.0|Výrazný umělý objekt – křížek
532.0|Schodiště
601.0|Magnetický poledník (černý)|R
601.1|Magnetický poledník (modrý)|R
601.2|Magnetický poledník (černý, plocha)|R
601.3|Magnetický poledník (modrý, plocha)|R
602.0|Registrační značka
603.0|Výšková kóta
603.1|Výšková kóta (text)|R
701.0|Start
702.0|Místo výdeje map|R
703.0|Kontrola
704.0|Číslo kontroly
705.0|Spojnice
706.0|Cíl
707.0|Značený úsek
708.0|Nepřekonatelná hranice
709.0|Nepřístupná oblast
709.1|Nepřístupná oblast s čárkovaným ohraničením|R
709.2|Nepřístupná oblast, čárkovaná linie|R
709.3|Nepřístupná oblast s plným ohraničením|R
709.4|Nepřístupná oblast, plná linie|R
710.100|Průchod
710.200|Průchod (úsek)|R
711.0|Nepřístupná trasa
711.1|Nepřístupná trasa (linie)|R
712.0|Stanoviště první pomoci
713.0|Občerstvovací stanice
715.0|Bod pokračování po výměně map
900.0|Text 36 pt|R
901.0|Text 18 pt|R
902.0|Text 8 pt|R
"""

_SPRINT = """
101.0|Základní vrstevnice
101.1|Základní vrstevnice – spádnice|R
102.0|Zdůrazněná vrstevnice
102.3|Popisek zdůrazněné vrstevnice|R
103.0|Doplňková vrstevnice
103.1|Doplňková vrstevnice – spádnice|R
104.0|Zemní sráz
104.5|Velmi vysoký zemní sráz|R
105.100|Malý zemní val
105.200|Opěrný zemní val viditelný pouze z jedné strany
107.0|Erozní rýha
108.0|Malá erozní rýha
109.0|Malá kupka
110.0|Malá protáhlá kupka
111.0|Malá prohlubeň
112.0|Jáma nebo díra
113.0|Rozbitý povrch
113.1|Rozbitý povrch (jednotlivá tečka)|R
115.0|Výrazný terénní objekt
201.0|Nepřekonatelný sráz
201.4|Nepřekonatelný sráz (minimální velikost)|R
202.0|Překonatelný sráz
202.1|Překonatelný sráz bez čárek|R
202.2|Překonatelný sráz (minimální velikost)|R
202.3|Překonatelný sráz (minimální velikost, varianta 2)|R
203.0|Kamenná jáma|R
203.1|Jeskyně|R
204.0|Balvan
205.0|Velký balvan
206.0|Obrovský balvan nebo skalní věž
207.0|Shluk balvanů
208.0|Balvanové pole
208.1|Balvanové pole (jednotlivý znak)|R
208.2|Balvanové pole (jednotlivý znak, 120 %)|R
208.4|Balvanové pole (120 %)|R
210.0|Kamenitý povrch
210.1|Kamenitý povrch (jednotlivá tečka)|R
213.0|Otevřený písčitý povrch
214.0|Holá skála
301.0|Nepřekonatelné vodní těleso
301.1|Nepřekonatelné vodní těleso (plocha)|R
301.2|Nepřekonatelné vodní těleso (okrajová linie)|R
301.4|Dominantní nepřekonatelné vodní těleso 70 %|R
301.5|Dominantní nepřekonatelné vodní těleso 70 % (plocha)|R
302.0|Mělké vodní těleso
302.1|Mělké vodní těleso (plocha)|R
302.4|Mělké vodní těleso (okrajová linie)|R
303.0|Jáma s vodou
305.0|Malý překonatelný vodní tok
306.0|Malý / občasný vodní příkop|R
307.0|Nepřekonatelná bažina
307.1|Nepřekonatelná bažina (plocha)|R
308.0|Bažina
308.1|Bažina (minimální velikost)|R
309.0|Úzká bažina
310.0|Nezřetelná bažina
311.0|Malá fontána nebo studna|R
312.0|Pramen
313.0|Výrazný vodní objekt
401.0|Otevřený prostor
401.1|Otevřený prostor, dominantní|R
402.0|Otevřený prostor s rozptýlenými stromy
402.1|Otevřený prostor s rozptýlenými keři|R
403.0|Divoký otevřený prostor
404.0|Divoký otevřený prostor s rozptýlenými stromy
404.1|Divoký otevřený prostor s rozptýlenými keři|R
405.0|Les
406.0|Vegetace: pomalý běh
406.1|Vegetace: pomalý běh, jeden směr|R
406.2|Vegetace: pomalý běh, živý plot|R
407.0|Vegetace: pomalý běh, dobrá viditelnost
408.0|Vegetace: chůze
408.1|Vegetace: chůze, jeden směr|R
408.2|Vegetace: chůze, živý plot|R
409.0|Vegetace: chůze, dobrá viditelnost
410.0|Vegetace: prodírání
410.1|Vegetace: prodírání, jeden směr|R
410.2|Vegetace: prodírání, živý plot|R
411.0|Nepřekonatelná vegetace
411.2|Nepřekonatelná vegetace, živý plot|R
412.0|Obdělávaná půda
413.0|Sad
413.1|Sad (jednotlivá tečka)|R
413.100|Sad (divoký otevřený prostor)|R
414.0|Vinice nebo podobné kultury
414.100|Vinice nebo podobné kultury (divoký otevřený prostor)|R
415.0|Zřetelná hranice obdělávané půdy
416.0|Zřetelná hranice vegetace
417.0|Výrazný velký strom
418.0|Výrazný keř nebo malý strom
419.0|Výrazný vegetační objekt
501.0|Zpevněná plocha, horní úroveň (lehký provoz)|R
501.1|Zpevněná plocha, horní úroveň (lehký provoz), plocha|R
501.2|Zpevněná plocha, dolní úroveň (lehký provoz)|R
501.10|Zpevněná pěšina 1,4 m|R
501.11|Zpevněná pěšina 2 m|R
501.12|Zpevněná pěšina 3 m|R
501.13|Zpevněná pěšina 4 m|R
501.20|Zpevněná plocha (těžký provoz)|R
501.21|Zpevněná plocha (těžký provoz), plocha|R
501.30|Zpevněná ulice (těžký provoz) 2 m|R
501.31|Zpevněná ulice (těžký provoz) 4 m|R
501.32|Zpevněná ulice (těžký provoz) 6 m|R
501.33|Zpevněná ulice (těžký provoz) 8 m|R
501.100|Schod nebo okraj zpevněné plochy
501.200|Schod nebo okraj zpevněné plochy ve spodní úrovni
501.300|Zpevněná plocha s rozptýlenými stromy, lehký provoz
501.301|Zpevněná plocha s rozptýlenými stromy, těžký provoz|R
505.0|Nezpevněná pěšina nebo cesta 1,4 m|R
505.1|Nezpevněná pěšina nebo cesta 2 m|R
505.2|Nezpevněná pěšina nebo cesta 3 m|R
505.3|Nezpevněná pěšina nebo cesta 4 m|R
505.4|Nezpevněná pěšina nebo cesta, okrajová linie|R
506.0|Malá nezpevněná pěšina nebo cesta|R
507.0|Nezřetelná pěšina
508.0|Průsek
509.100|Železnice
509.101|Železnice, oblast se zákazem vstupu (linie)|R
509.200|Tramvajová trať
510.0|Elektrické vedení, lanovka nebo lyžařský vlek
511.0|Hlavní elektrické vedení
511.1|Hlavní elektrické vedení (čtvercový sloup)|R
512.100|Vstup pod most nebo do tunelu
512.101|Vstup pod most nebo do tunelu (jeden trojúhelník)|R
512.200|Podchod nebo tunel
512.300|Oblast průběžná v nižší úrovni
512.301|Oblast průběžná v nižší úrovni (214)|R
512.302|Oblast průběžná v nižší úrovni (301)|R
512.304|Oblast průběžná v nižší úrovni (401, 402, 412, 413, 414)|R
512.307|Oblast průběžná v nižší úrovni (408)|R
512.308|Oblast průběžná v nižší úrovni (410)|R
512.309|Oblast průběžná v nižší úrovni (411)|R
512.311|Oblast průběžná v nižší úrovni (501, 501.3)|R
512.312|Oblast průběžná v nižší úrovni (520)|R
512.315|Oblast průběžná v nižší úrovni (521)|R
512.313|Oblast průběžná v nižší úrovni (522)|R
512.314|Oblast průběžná v nižší úrovni (709, 714)|R
513.100|Překonatelná zeď
513.101|Překonatelná zeď bez teček|R
513.200|Překonatelná opěrná zeď
515.0|Nepřekonatelná zeď
516.0|Překonatelný plot nebo zábradlí
518.0|Nepřekonatelný plot nebo zábradlí
519.0|Průchod (volitelné)
520.0|Oblast se zákazem vstupu
520.1|Oblast se zákazem vstupu (plocha)|R
520.2|Oblast se zákazem vstupu (okrajová linie)|R
521.0|Budova
521.2|Budova (obrys)|R
521.3|Budova (minimální velikost)|R
522.0|Zastřešení
522.1|Zastřešení (obrys)|R
522.100|Pilíř
524.0|Vysoká věž
525.0|Malá věž
526.0|Mohyla, pomník, socha, malý památník nebo hraniční kámen
527.0|Krmelec
528.0|Výrazný liniový objekt
529.0|Výrazný nepřekonatelný liniový objekt
530.0|Výrazný umělý objekt – kroužek
531.0|Výrazný umělý objekt – křížek
532.0|Schodiště 1,6 m|R
532.1|Schodiště 3 m|R
532.2|Schodiště 4 m|R
533.0|Oblast s překážkami
601.0|Magnetický poledník (černý)|R
601.1|Magnetický poledník (modrý)|R
601.2|Magnetický poledník (černý, plocha)|R
601.3|Magnetický poledník (modrý, plocha)|R
701.0|Start
702.0|Místo výdeje map
703.0|Kontrola
704.0|Číslo kontroly
705.0|Spojnice
706.0|Cíl
707.0|Značený úsek
708.0|Nepřekonatelná hranice
709.0|Nepřístupná oblast
709.1|Nepřístupná oblast s čárkovaným ohraničením|R
709.2|Nepřístupná oblast, čárkovaná linie|R
709.3|Nepřístupná oblast s plným ohraničením|R
709.4|Nepřístupná oblast, plná linie|R
710.100|Bod přechodu
710.200|Úsek přechodu
714.0|Dočasná stavba nebo uzavřená oblast
714.1|Dočasná stavba nebo uzavřená oblast (okrajová linie)|R
715.0|Bod pokračování po výměně mapy
900.0|Text 36 pt|R
901.0|Text 18 pt|R
902.0|Text 8 pt|R
"""


def _parse(block: str) -> dict[str, tuple[str, bool]]:
    out: dict[str, tuple[str, bool]] = {}
    for line in block.strip().splitlines():
        parts = line.split("|")
        out[parts[0]] = (parts[1], len(parts) > 2 and parts[2] == "R")
    return out


NAMES: dict[str, dict[str, tuple[str, bool]]] = {
    "isom": _parse(_ISOM),
    "sprint": _parse(_SPRINT),
}
