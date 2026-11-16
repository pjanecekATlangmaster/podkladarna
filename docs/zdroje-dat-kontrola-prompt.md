# Prompt pro kontrolu registru zdrojů jiným LLM

Jak použít: zkopíruj celý blok níže do jiného modelu (ChatGPT, Gemini, Mistral …, ideálně s vyhledáváním
na webu) a **pod něj vlož celý obsah `configs/data_sources.yaml`** (repozitář je soukromý, model ho sám
nestáhne). Odpověď je ve stejném YAML formátu, takže nové nebo opravené záznamy jde po kontrole rovnou
vložit do registru a přegenerovat `docs/zdroje-dat.md` (`python scripts/data_sources_doc.py`).
Nic z odpovědi nepřebírej bez ověření na stránce poskytovatele (modely si URL i licence vymýšlejí).

---

```text
Jsi expert na otevřená geodata v Evropě (LiDAR, digitální modely terénu a povrchu, velkoměřítkové
technické mapy měst, ortofoto, katastr, topografické vektory). Kontroluješ registr zdrojů dat služby,
která z těchto dat automaticky generuje podkladové mapy pro orientační běh a mapy PRODÁVÁ
(cca 10 € za mapu). Proto rozhoduje, zda licence dovoluje KOMERČNÍ použití a šíření odvozeného díla.

Jak služba data používá:
- Výšky: potřebuje DTM (terén) i DSM / mračno bodů (výška porostu), ideálně rozlišení ≤ 1 m.
  Nejlepší je klasifikované LiDAR mračno (LAZ/COPC) s třídami vegetace; rastr DSM − DTM je slabší náhrada.
  30m modely (Copernicus GLO-30, FABDEM) jsou nepoužitelné.
- Kresba: OSM je základ; „technická mapa“ = velkoměřítková vektorová data měst (budovy, vozovky,
  chodníky, parkoviště, schody, zdi, ploty), obdoba české Digitální technické mapy (např. NL BGT, CH AV,
  Vídeň FMZK, Berlín Straßenbefahrung, Paříž Plan de voirie, EE ETAK, Flandry GRB, Helsinky YLRE).
- Šablony: ortofoto, podkladová mapa, katastr, stínování (WMS/WMTS/TMS), přibalené do ZIPu.
- Stahuje se automaticky ze serveru jen výřez mapy (1–25 km²) bez lidského zásahu. Ruční stahování
  přes portál, výdej na e-mail, objednávky a stovky MB na jedno stažení jsou problém.

Úkol:
1. Pro každou zemi v registru najdi zdroje, které CHYBÍ (hlavně: otevřené LiDAR mračno, DSM, technická
   mapa měst, ortofoto s otevřenou licencí), a navrhni pro ně nové záznamy.
2. Najdi země mimo registr, kde je otevřený LiDAR (DTM + DSM nebo mračno) s komerčně použitelnou licencí
   a automatickým stažením (API, WCS, STAC, COG, přímé soubory po listech), a navrhni záznamy.
3. Zkontroluj existující záznamy: platí URL serveru? Platí licence a požadovaná citace? Neuvádí registr
   chybně „public“ u dat s nekomerční licencí, poplatkem, nutností registrace, API klíče nebo e-mailu?
   Nezměnila se služba (přejmenování, ukončení, nová verze dat)? Známé problémy (výpadky, limity dotazů,
   nízká hustota bodů, data bez listí, díry v pokrytí, posuny souřadnic, chyby v datech)?
4. U každého tvrzení uveď ODKAZ na stránku poskytovatele (licence, produkt nebo dokumentace služby).
   Co nemůžeš doložit odkazem, označ v poli `checked` jako „neověřeno“. Nevymýšlej URL.

Formát odpovědi:
A) Krátké shrnutí (max. 10 bodů): co je v registru chybně a nejdůležitější nové zdroje.
B) YAML se seznamem `sources:` jen s NOVÝMI nebo OPRAVENÝMI záznamy, přesně ve formátu registru:
   - id (malá písmena, např. pl_gugik_laz), countries (ISO kódy; "NO" a "off" v uvozovkách),
     kinds (elevation | point_cloud | canopy | technical_map | vectors | buildings | orthophoto | map |
     cadastre | hillshade | imagery), name, provider, url (konkrétní služba / endpoint), info
     (stránka licence nebo produktu), role (kresba | výšky | šablona), status (candidate pro nové),
     license {class: public | restricted | unknown, name, url, attribution}, access (free | email |
     api_key | account | contract), checked („2026-MM-DD <co jsi ověřil>“ nebo „neověřeno“),
     issues (seznam krátkých vět).
   - U opraveného záznamu ponech stejné `id` a do `issues` připiš „OPRAVA: …“ s důvodem.
C) Seznam zdrojů, které jsi zvažoval a vyřadil, s důvodem (licence, bez API, malé rozlišení …).

Registr (configs/data_sources.yaml):
<sem vlož obsah souboru>
```
