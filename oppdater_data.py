#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Henter besvarelser fra SurveyXact og skriver data.json til Norgeskartet (kart.html).

Kjøres med faste intervaller, f.eks. hvert 5. minutt:

    python3 oppdater_data.py                 # hent fra SurveyXact-API-et én gang
    python3 oppdater_data.py --loop 300      # hent hvert 300. sekund til den stoppes
    python3 oppdater_data.py --fra-fil dataset.csv   # test med en manuell eksport

Krever kun Python 3.8+ (ingen ekstra pakker).

VIKTIG: API-kallet er skrevet etter SurveyXacts REST-API slik det brukes i åpne
integrasjoner (GET /rest/surveys/{id}/export/dataset, Basic Auth), men er ikke
testet mot en ekte SurveyXact-konto. Kjør én gang med --vis-kolonner og
kontroller innstillingene under før jobben settes i drift.
"""

import argparse
import base64
import csv
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone

# ───────────────────────── Innstillinger ─────────────────────────
# Alle kan overstyres med miljøvariabler med samme navn.

# Adressen til SurveyXacts REST-API.
SX_BASE_URL = os.environ.get("SX_BASE_URL") or "https://rest.survey-xact.dk/rest"

# API-bruker. Legg brukernavn og passord i miljøvariabler, ikke i denne filen.
SX_USER = os.environ.get("SX_USER", "")
SX_PASSWORD = os.environ.get("SX_PASSWORD", "")

# Målingens id i SurveyXact (tallet i https://rest.survey-xact.dk/rest/surveys/ID).
SX_SURVEY_ID = os.environ.get("SX_SURVEY_ID") or "1229499"

# Variabelen som sier hvilken kommune respondenten hører til. Verdien kan være
# kommunenavn (f.eks. Bergen) eller kommunenummer (f.eks. 4601 eller 301).
SX_KOMMUNE_VARIABEL = os.environ.get("SX_KOMMUNE_VARIABEL") or "ptype2t"

# En respondent telles som besvarelse når statusvariabelen har en av verdiene
# under. Standard: statoverall = 4 («Gjennomført»). Bruk "3,4" for å ta med
# delvise svar. Er variabelen eksportert som 0/1-kolonner (statoverall_4),
# brukes de automatisk.
SX_STATUS_VARIABEL = os.environ.get("SX_STATUS_VARIABEL") or "stato"
SX_STATUS_VERDIER = [v.strip() for v in (os.environ.get("SX_STATUS_VERDIER") or "4").split(",") if v.strip()]

# Kommuner med færre besvarelser enn dette (men flere enn 0) skrives som
# «færre enn N» i stedet for eksakt antall. 0 = vis alle tall.
# Merk: fylkestotalen vises uansett, så med bare én skjult kommune i et fylke
# kan tallet regnes ut. Øk grensen eller vis kun fylker hvis det er et problem.
MIN_ANTALL = int(os.environ.get("MIN_ANTALL") or "0")

# Overskriften i kartet.
TITTEL = (
    os.environ.get("TITTEL")
    or "IMDi – Brukerundersøkelse blant nyankomne innvandrere i opplæring 2026"
)

# Hvor data.json skal skrives (samme mappe på webserveren som kart.html).
UT_FIL = os.environ.get("UT_FIL") or "data.json"
# ─────────────────────────────────────────────────────────────────

# Kommuneinndelingen fra 2024 (357 kommuner), samme som i kartet.
KOMMUNER = {
    "0301": "Oslo", "1101": "Eigersund", "1103": "Stavanger", "1106": "Haugesund",
    "1108": "Sandnes", "1111": "Sokndal", "1112": "Lund", "1114": "Bjerkreim", "1119": "Hå",
    "1120": "Klepp", "1121": "Time", "1122": "Gjesdal", "1124": "Sola", "1127": "Randaberg",
    "1130": "Strand", "1133": "Hjelmeland", "1134": "Suldal", "1135": "Sauda",
    "1144": "Kvitsøy", "1145": "Bokn", "1146": "Tysvær", "1149": "Karmøy", "1151": "Utsira",
    "1160": "Vindafjord", "1505": "Kristiansund", "1506": "Molde", "1508": "Ålesund",
    "1511": "Vanylven", "1514": "Sande", "1515": "Herøy", "1516": "Ulstein", "1517": "Hareid",
    "1520": "Ørsta", "1525": "Stranda", "1528": "Sykkylven", "1531": "Sula", "1532": "Giske",
    "1535": "Vestnes", "1539": "Rauma", "1547": "Aukra", "1554": "Averøy", "1557": "Gjemnes",
    "1560": "Tingvoll", "1563": "Sunndal", "1566": "Surnadal", "1573": "Smøla", "1576": "Aure",
    "1577": "Volda", "1578": "Fjord", "1579": "Hustadvika", "1580": "Haram", "1804": "Bodø",
    "1806": "Narvik", "1811": "Bindal", "1812": "Sømna", "1813": "Brønnøy", "1815": "Vega",
    "1816": "Vevelstad", "1818": "Herøy", "1820": "Alstahaug", "1822": "Leirfjord",
    "1824": "Vefsn", "1825": "Grane", "1826": "Aarborte - Hattfjelldal", "1827": "Dønna",
    "1828": "Nesna", "1832": "Hemnes", "1833": "Rana", "1834": "Lurøy", "1835": "Træna",
    "1836": "Rødøy", "1837": "Meløy", "1838": "Gildeskål", "1839": "Beiarn", "1840": "Saltdal",
    "1841": "Fauske - Fuossko", "1845": "Sørfold", "1848": "Steigen", "1851": "Lødingen",
    "1853": "Evenes - Evenášši", "1856": "Røst", "1857": "Værøy", "1859": "Flakstad",
    "1860": "Vestvågøy", "1865": "Vågan", "1866": "Hadsel", "1867": "Bø", "1868": "Øksnes",
    "1870": "Sortland - Suortá", "1871": "Andøy", "1874": "Moskenes",
    "1875": "Hábmer - Hamarøy", "3101": "Halden", "3103": "Moss", "3105": "Sarpsborg",
    "3107": "Fredrikstad", "3110": "Hvaler", "3112": "Råde", "3114": "Våler",
    "3116": "Skiptvet", "3118": "Indre Østfold", "3120": "Rakkestad", "3122": "Marker",
    "3124": "Aremark", "3201": "Bærum", "3203": "Asker", "3205": "Lillestrøm",
    "3207": "Nordre Follo", "3209": "Ullensaker", "3212": "Nesodden", "3214": "Frogn",
    "3216": "Vestby", "3218": "Ås", "3220": "Enebakk", "3222": "Lørenskog", "3224": "Rælingen",
    "3226": "Aurskog-Høland", "3228": "Nes", "3230": "Gjerdrum", "3232": "Nittedal",
    "3234": "Lunner", "3236": "Jevnaker", "3238": "Nannestad", "3240": "Eidsvoll",
    "3242": "Hurdal", "3301": "Drammen", "3303": "Kongsberg", "3305": "Ringerike",
    "3310": "Hole", "3312": "Lier", "3314": "Øvre Eiker", "3316": "Modum", "3318": "Krødsherad",
    "3320": "Flå", "3322": "Nesbyen", "3324": "Gol", "3326": "Hemsedal", "3328": "Ål",
    "3330": "Hol", "3332": "Sigdal", "3334": "Flesberg", "3336": "Rollag",
    "3338": "Nore og Uvdal", "3401": "Kongsvinger", "3403": "Hamar", "3405": "Lillehammer",
    "3407": "Gjøvik", "3411": "Ringsaker", "3412": "Løten", "3413": "Stange",
    "3414": "Nord-Odal", "3415": "Sør-Odal", "3416": "Eidskog", "3417": "Grue", "3418": "Åsnes",
    "3419": "Våler", "3420": "Elverum", "3421": "Trysil", "3422": "Åmot", "3423": "Stor-Elvdal",
    "3424": "Rendalen", "3425": "Engerdal", "3426": "Tolga", "3427": "Tynset", "3428": "Alvdal",
    "3429": "Folldal", "3430": "Os", "3431": "Dovre", "3432": "Lesja", "3433": "Skjåk",
    "3434": "Lom", "3435": "Vågå", "3436": "Nord-Fron", "3437": "Sel", "3438": "Sør-Fron",
    "3439": "Ringebu", "3440": "Øyer", "3441": "Gausdal", "3442": "Østre Toten",
    "3443": "Vestre Toten", "3446": "Gran", "3447": "Søndre Land", "3448": "Nordre Land",
    "3449": "Sør-Aurdal", "3450": "Etnedal", "3451": "Nord-Aurdal", "3452": "Vestre Slidre",
    "3453": "Øystre Slidre", "3454": "Vang", "3901": "Horten", "3903": "Holmestrand",
    "3905": "Tønsberg", "3907": "Sandefjord", "3909": "Larvik", "3911": "Færder",
    "4001": "Porsgrunn", "4003": "Skien", "4005": "Notodden", "4010": "Siljan",
    "4012": "Bamble", "4014": "Kragerø", "4016": "Drangedal", "4018": "Nome",
    "4020": "Midt-Telemark", "4022": "Seljord", "4024": "Hjartdal", "4026": "Tinn",
    "4028": "Kviteseid", "4030": "Nissedal", "4032": "Fyresdal", "4034": "Tokke",
    "4036": "Vinje", "4201": "Risør", "4202": "Grimstad", "4203": "Arendal",
    "4204": "Kristiansand", "4205": "Lindesnes", "4206": "Farsund", "4207": "Flekkefjord",
    "4211": "Gjerstad", "4212": "Vegårshei", "4213": "Tvedestrand", "4214": "Froland",
    "4215": "Lillesand", "4216": "Birkenes", "4217": "Åmli", "4218": "Iveland",
    "4219": "Evje og Hornnes", "4220": "Bygland", "4221": "Valle", "4222": "Bykle",
    "4223": "Vennesla", "4224": "Åseral", "4225": "Lyngdal", "4226": "Hægebostad",
    "4227": "Kvinesdal", "4228": "Sirdal", "4601": "Bergen", "4602": "Kinn", "4611": "Etne",
    "4612": "Sveio", "4613": "Bømlo", "4614": "Stord", "4615": "Fitjar", "4616": "Tysnes",
    "4617": "Kvinnherad", "4618": "Ullensvang", "4619": "Eidfjord", "4620": "Ulvik",
    "4621": "Voss", "4622": "Kvam", "4623": "Samnanger", "4624": "Bjørnafjorden",
    "4625": "Austevoll", "4626": "Øygarden", "4627": "Askøy", "4628": "Vaksdal",
    "4629": "Modalen", "4630": "Osterøy", "4631": "Alver", "4632": "Austrheim", "4633": "Fedje",
    "4634": "Masfjorden", "4635": "Gulen", "4636": "Solund", "4637": "Hyllestad",
    "4638": "Høyanger", "4639": "Vik", "4640": "Sogndal", "4641": "Aurland", "4642": "Lærdal",
    "4643": "Årdal", "4644": "Luster", "4645": "Askvoll", "4646": "Fjaler", "4647": "Sunnfjord",
    "4648": "Bremanger", "4649": "Stad", "4650": "Gloppen", "4651": "Stryn",
    "5001": "Trondheim - Tråante", "5006": "Steinkjer", "5007": "Namsos - Nåavmesjenjaelmie",
    "5014": "Frøya", "5020": "Osen", "5021": "Oppdal", "5022": "Rennebu",
    "5025": "Rosse - Røros", "5026": "Holtålen", "5027": "Midtre Gauldal", "5028": "Melhus",
    "5029": "Skaun", "5031": "Malvik", "5032": "Selbu", "5033": "Tydal", "5034": "Meråker",
    "5035": "Stjørdal", "5036": "Frosta", "5037": "Levanger", "5038": "Verdal",
    "5041": "Snåase - Snåsa", "5042": "Lierne", "5043": "Raarvihke - Røyrvik",
    "5044": "Namsskogan", "5045": "Grong", "5046": "Høylandet", "5047": "Overhalla",
    "5049": "Flatanger", "5052": "Leka", "5053": "Inderøy", "5054": "Indre Fosen",
    "5055": "Heim", "5056": "Hitra", "5057": "Ørland", "5058": "Åfjord", "5059": "Orkland",
    "5060": "Nærøysund", "5061": "Rindal", "5501": "Tromsø", "5503": "Harstad - Hárstták",
    "5510": "Kvæfjord", "5512": "Dielddanuorri - Tjeldsund", "5514": "Ibestad",
    "5516": "Gratangen", "5518": "Loabák - Lavangen", "5520": "Bardu", "5522": "Salangen",
    "5524": "Målselv", "5526": "Sørreisa", "5528": "Dyrøy", "5530": "Senja",
    "5532": "Balsfjord", "5534": "Karlsøy", "5536": "Lyngen",
    "5538": "Storfjord - Omasvuotna - Omasvuono", "5540": "Gáivuotna - Kåfjord - Kaivuono",
    "5542": "Skjervøy", "5544": "Nordreisa - Ráisa - Raisi", "5546": "Kvænangen",
    "5601": "Alta", "5603": "Hammerfest - Hámmerfeasta", "5605": "Sør-Varanger",
    "5607": "Vadsø", "5610": "Kárášjohka - Karasjok", "5612": "Guovdageaidnu - Kautokeino",
    "5614": "Loppa", "5616": "Hasvik", "5618": "Måsøy", "5620": "Nordkapp",
    "5622": "Porsanger - Porsáŋgu - Porsanki", "5624": "Lebesby", "5626": "Gamvik",
    "5628": "Deatnu - Tana", "5630": "Berlevåg", "5632": "Båtsfjord", "5634": "Vardø",
    "5636": "Unjárga - Nesseby",
}

# Navn som finnes i to fylker. Må oppgis med kommunenummer eller fylke i parentes.
TVETYDIGE = {"herøy", "våler"}


def logg(*deler):
    print(datetime.now().strftime("%H:%M:%S"), *deler, file=sys.stderr, flush=True)


def _nokkel(tekst):
    """Normaliserer et kommunenavn for oppslag."""
    t = str(tekst).strip().casefold()
    for strek in ("–", "—", "‐"):
        t = t.replace(strek, "-")
    t = t.replace(" kommune", "")
    return " ".join(t.split())


def bygg_navneoppslag():
    oppslag = {}
    fylker = {"15": "møre og romsdal", "18": "nordland", "31": "østfold", "34": "innlandet"}
    for nr, navn in KOMMUNER.items():
        varianter = {_nokkel(navn)} | {_nokkel(del_) for del_ in navn.split(" - ")}
        for v in varianter:
            if v in TVETYDIGE:
                oppslag["%s (%s)" % (v, fylker[nr[:2]])] = nr
            else:
                oppslag[v] = nr
    return oppslag


NAVN = bygg_navneoppslag()


def finn_kommune(verdi, verdikart):
    """Returnerer firesifret kommunenummer, eller None hvis verdien ikke kan tolkes."""
    v = str(verdi).strip()
    if not v:
        return None
    if v in verdikart:
        v = verdikart[v]
    tall = _tall(v)
    if tall.isdigit():
        nr = tall.zfill(4)
        return nr if nr in KOMMUNER else None
    return NAVN.get(_nokkel(v))


def les_verdikart(sti):
    """Leser en valgfri oversettelsesfil: «verdi;kommunenummer» per linje."""
    kart = {}
    if not sti:
        return kart
    with open(sti, encoding="utf-8-sig", newline="") as f:
        for rad in csv.reader(f, delimiter=";"):
            if len(rad) >= 2 and rad[0].strip():
                kart[rad[0].strip()] = rad[1].strip()
    return kart


def dekod(raa):
    """Gjør om svaret fra SurveyXact (ev. zip, UTF-16 eller UTF-8) til tekst."""
    if raa[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raa)) as z:
            navn = [n for n in z.namelist() if "dataset" in n.lower()] or z.namelist()
            raa = z.read(navn[0])
    if raa[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raa.decode("utf-16")
    if raa[:3] == b"\xef\xbb\xbf":
        return raa.decode("utf-8-sig")
    if b"\x00" in raa[:200]:
        return raa.decode("utf-16-le")
    try:
        return raa.decode("utf-8")
    except UnicodeDecodeError:
        return raa.decode("cp1252")


def les_rader(tekst):
    """Leser datasettet som liste av dict. Finner selv skilletegnet."""
    forste = tekst.split("\n", 1)[0]
    skille = max(("\t", ";", ","), key=forste.count)
    leser = csv.DictReader(io.StringIO(tekst), delimiter=skille)
    kolonner = [k.strip() for k in (leser.fieldnames or [])]
    leser.fieldnames = kolonner
    return kolonner, list(leser)


def hent_fra_api():
    if not (SX_USER and SX_PASSWORD):
        raise SystemExit("Mangler SX_USER og SX_PASSWORD (miljøvariabler).")
    url = "%s/surveys/%s/export/dataset?%s" % (
        SX_BASE_URL.rstrip("/"),
        urllib.parse.quote(SX_SURVEY_ID),
        urllib.parse.urlencode({"format": "INTL_US"}),
    )
    foresporsel = urllib.request.Request(url)
    token = base64.b64encode(("%s:%s" % (SX_USER, SX_PASSWORD)).encode("utf-8")).decode("ascii")
    foresporsel.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(foresporsel, timeout=120) as svar:
            return svar.read()
    except urllib.error.HTTPError as e:
        hint = {
            401: "Feil brukernavn eller passord.",
            403: "API-brukeren har ikke tilgang til målingen.",
            404: "Fant ikke målingen. Sjekk SX_SURVEY_ID og SX_BASE_URL.",
        }.get(e.code, "")
        raise RuntimeError("SurveyXact svarte HTTP %s. %s" % (e.code, hint)) from e
    except urllib.error.URLError as e:
        raise RuntimeError("Fikk ikke kontakt med SurveyXact: %s" % e.reason) from e


def _tall(verdi):
    v = str(verdi).strip()
    return v[:-2] if v.endswith(".0") else v


def lag_statustest(kolonner):
    """Returnerer en funksjon som sier om en rad skal telles."""
    if SX_STATUS_VARIABEL in kolonner:
        verdier = set(SX_STATUS_VERDIER)
        return lambda rad: _tall(rad.get(SX_STATUS_VARIABEL, "")) in verdier
    dummyer = ["%s_%s" % (SX_STATUS_VARIABEL, v) for v in SX_STATUS_VERDIER]
    if all(d in kolonner for d in dummyer):
        return lambda rad: any(_tall(rad.get(d, "")) == "1" for d in dummyer)
    return None


def tell(kolonner, rader, verdikart):
    statustest = lag_statustest(kolonner)
    mangler = [] if statustest else [SX_STATUS_VARIABEL]
    if SX_KOMMUNE_VARIABEL not in kolonner:
        mangler.insert(0, SX_KOMMUNE_VARIABEL)
    if mangler:
        status = [k for k in kolonner if k.lower().startswith("stat")]
        raise RuntimeError(
            "Finner ikke variabelen %s i datasettet (%d kolonner).\n"
            "Statusvariabler som finnes: %s\n"
            "Kjør med --vis-kolonner for å se alle kolonner."
            % (" og ".join(mangler), len(kolonner), ", ".join(status) or "(ingen)")
        )
    per_kommune = Counter()
    ukjente = Counter()
    total = 0
    for rad in rader:
        if not statustest(rad):
            continue
        total += 1
        verdi = rad.get(SX_KOMMUNE_VARIABEL, "")
        nr = finn_kommune(verdi, verdikart)
        if nr:
            per_kommune[nr] += 1
        else:
            ukjente[str(verdi).strip() or "(tom)"] += 1
    return total, per_kommune, ukjente


def bygg_data(total, per_kommune, ukjente):
    fylker = Counter()
    for nr, n in per_kommune.items():
        fylker[nr[:2]] += n
    kommuner = {}
    for nr in sorted(per_kommune):
        n = per_kommune[nr]
        kommuner[nr] = None if 0 < n < MIN_ANTALL else n
    return {
        "survey": TITTEL,
        "updated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "total": total,
        "unknown": sum(ukjente.values()),
        "min_count": MIN_ANTALL,
        "fylker": {nr: fylker[nr] for nr in sorted(fylker)},
        "kommuner": kommuner,
    }


def skriv_atomisk(sti, data):
    """Skriver til en midlertidig fil og bytter den inn, så kartet aldri leser en halv fil."""
    mappe = os.path.dirname(os.path.abspath(sti))
    fd, tmp = tempfile.mkstemp(dir=mappe, prefix=".data-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, sti)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def kjor(args, verdikart):
    if args.fra_fil:
        with open(args.fra_fil, "rb") as f:
            raa = f.read()
    else:
        raa = hent_fra_api()
    kolonner, rader = les_rader(dekod(raa))

    if args.vis_kolonner:
        print("%d rader, %d kolonner:" % (len(rader), len(kolonner)))
        for k in kolonner:
            print("  " + k)
        return

    total, per_kommune, ukjente = tell(kolonner, rader, verdikart)
    data = bygg_data(total, per_kommune, ukjente)
    skriv_atomisk(args.ut, data)
    logg(
        "Skrev %s: %d besvarelser i %d kommuner, %d uten gyldig kommune."
        % (args.ut, total, len(per_kommune), data["unknown"])
    )
    if ukjente and args.vis_ukjente:
        vanligste = ", ".join("«%s» (%d)" % (v, n) for v, n in ukjente.most_common(8))
        logg("Verdier i %s som ikke ble gjenkjent: %s" % (SX_KOMMUNE_VARIABEL, vanligste))
    elif ukjente:
        logg("Kjør med --vis-ukjente for å se hvilke verdier som ikke ble gjenkjent.")


def main():
    p = argparse.ArgumentParser(description="Oppdaterer data.json til Norgeskartet fra SurveyXact.")
    p.add_argument("--ut", default=UT_FIL, help="hvor data.json skal skrives (standard: %(default)s)")
    p.add_argument("--fra-fil", metavar="FIL", help="les en manuell datasett-eksport i stedet for API-et")
    p.add_argument("--verdikart", metavar="FIL", help="CSV med «verdi;kommunenummer» for egne koder")
    p.add_argument("--vis-kolonner", action="store_true", help="list kolonnene i datasettet og avslutt")
    p.add_argument("--vis-ukjente", action="store_true",
                   help="logg kommuneverdier som ikke ble gjenkjent (ikke bruk der loggen er offentlig)")
    p.add_argument("--loop", type=int, metavar="SEK", help="kjør på nytt hvert SEK. sekund")
    args = p.parse_args()
    verdikart = les_verdikart(args.verdikart)

    if not args.loop:
        try:
            kjor(args, verdikart)
        except RuntimeError as e:
            raise SystemExit("FEIL: %s" % e)
        return

    pause = max(30, args.loop)
    logg("Oppdaterer hvert %d. sekund. Stopp med Ctrl+C." % pause)
    while True:
        try:
            kjor(args, verdikart)
        except RuntimeError as e:
            # Behold forrige data.json; kartet viser da siste kjente tall.
            logg("FEIL: %s" % e)
        except KeyboardInterrupt:
            break
        try:
            time.sleep(pause)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
