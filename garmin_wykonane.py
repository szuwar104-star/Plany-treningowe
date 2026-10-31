#!/usr/bin/env python3
"""
Pobiera wykonane aktywności z Garmin Connect do pliku wykonane.json,
który czyta aplikacja (index.html): odhacza zrobione treningi, liczy
statystyki i Twoje rzeczywiste tempa (spokojne, regeneracja).

Logowanie: to samo co w garmin_trening.py (tokeny z sekretu GARMIN_TOKENS
na GitHubie albo z ~/.garminconnect lokalnie).

Użycie:
    python garmin_wykonane.py                  # ostatnie 21 dni (pierwszy raz: 120)
    python garmin_wykonane.py --dni 60         # dłuższe okno odświeżania
    python garmin_wykonane.py --zawsze-zapisz  # zapisz plik nawet bez nowych danych
                                               # (aplikacja widzi wtedy, że pobranie się udało)
    python garmin_wykonane.py --bez-odcinkow   # bez szczegółów okrążeń (szybciej)

W pliku są tylko podsumowania: data, godzina, nazwa, dystans, czas, tętno,
efekt treningowy i suma odcinków wg rodzaju kroku (rozgrzewka, bieg,
regeneracja, przerwa, schłodzenie) – bez śladu GPS.
"""

import argparse
import datetime as dt
import json
import sys

from garmin_trening import zaloguj

PLIK = "wykonane.json"
DNI_PIERWSZY_RAZ = 120

SPORT = {
    "running": "bieg", "treadmill_running": "bieg", "trail_running": "bieg",
    "track_running": "bieg", "indoor_running": "bieg", "street_running": "bieg",
    "virtual_run": "bieg", "ultra_run": "bieg",
    "strength_training": "sila",
}
# rodzaj okrążenia w Garminie -> rodzaj kroku w planie
ODCINKI = {
    "WARMUP": "rozgrzewka", "INTERVAL": "bieg", "ACTIVE": "bieg",
    "RECOVERY": "regeneracja", "REST": "przerwa", "COOLDOWN": "schlodzenie",
}


def wczytaj():
    try:
        with open(PLIK, encoding="utf-8") as f:
            dane = json.load(f)
        if isinstance(dane.get("aktywnosci"), list):
            return dane
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"Plik {PLIK} jest uszkodzony ({e}) – tworzę od nowa.")
    return {"aktywnosci": []}


def lista_aktywnosci(client, od, do):
    try:
        return client.get_activities_by_date(od.isoformat(), do.isoformat())
    except AttributeError:  # starsza/nowsza biblioteka bez tej metody
        wynik, start = [], 0
        while True:
            paczka = client.get_activities(start, 100) or []
            for a in paczka:
                d = str(a.get("startTimeLocal", ""))[:10]
                if od.isoformat() <= d <= do.isoformat():
                    wynik.append(a)
            if len(paczka) < 100 or min(str(a.get("startTimeLocal", ""))[:10] for a in paczka) < od.isoformat():
                return wynik
            start += 100


def odcinki(client, id_):
    """Suma okrążeń wg rodzaju kroku treningu: {"regeneracja": {"km": .., "sek": ..}, ...}"""
    try:
        dane = client.get_activity_splits(id_)
    except Exception as e:
        print(f"  odcinki {id_}: pominięte ({e})")
        return None
    wynik = {}
    for lap in (dane or {}).get("lapDTOs") or []:
        rodzaj = ODCINKI.get(str(lap.get("intensityType", "")).upper())
        if not rodzaj:
            continue
        s = wynik.setdefault(rodzaj, {"km": 0.0, "sek": 0})
        s["km"] += (lap.get("distance") or 0) / 1000
        s["sek"] += lap.get("movingDuration") or lap.get("duration") or 0
    for s in wynik.values():
        s["km"] = round(s["km"], 3)
        s["sek"] = round(s["sek"])
    # sam „bieg” bez innych rodzajów to zwykłe autolapy – nic nie wnoszą
    return wynik if set(wynik) - {"bieg"} else None


def rekord(a):
    typ = (a.get("activityType") or {}).get("typeKey", "")
    start = str(a.get("startTimeLocal") or "")
    zaokr = lambda v, n=0: (round(v, n) if n else round(v)) if isinstance(v, (int, float)) and v else None
    r = {
        "id": a.get("activityId"),
        "data": start[:10],
        "godz": start[11:16],
        "nazwa": a.get("activityName") or "",
        "typ": typ,
        "sport": SPORT.get(typ, "inny"),
        "km": round((a.get("distance") or 0) / 1000, 3),
        "sek": round(a.get("duration") or 0),
        "ruch_sek": zaokr(a.get("movingDuration")),
        "hr": zaokr(a.get("averageHR")),
        "hr_max": zaokr(a.get("maxHR")),
        "kcal": zaokr(a.get("calories")),
        "przewyzszenie": zaokr(a.get("elevationGain")),
        "kadencja": zaokr(a.get("averageRunningCadenceInStepsPerMinute")),
        "te": zaokr(a.get("aerobicTrainingEffect"), 1),
        "te_an": zaokr(a.get("anaerobicTrainingEffect"), 1),
        "obciazenie": zaokr(a.get("activityTrainingLoad")),
        "serie": a.get("activeSets") or a.get("totalSets") or None,
        "trening_id": a.get("workoutId") or None,
    }
    return {k: v for k, v in r.items() if v not in (None, "") or k in ("km", "sek")}


def zapisz(dane):
    # jedna aktywność w linii – czytelne zmiany w historii repo
    linie = [json.dumps(a, ensure_ascii=False, separators=(",", ":")) for a in dane["aktywnosci"]]
    with open(PLIK, "w", encoding="utf-8") as f:
        f.write('{"zaktualizowano":' + json.dumps(dane["zaktualizowano"]) + ',"aktywnosci":[\n')
        f.write(",\n".join(linie))
        f.write("\n]}\n")


def main():
    p = argparse.ArgumentParser(description="Pobierz wykonane treningi z Garmin Connect")
    p.add_argument("--dni", type=int, default=21, help="ile dni wstecz odświeżyć (domyślnie 21)")
    p.add_argument("--zawsze-zapisz", action="store_true", help="zapisz plik także bez zmian")
    p.add_argument("--bez-odcinkow", action="store_true", help="bez sumy okrążeń wg rodzaju kroku")
    arg = p.parse_args()

    dane = wczytaj()
    dzis = dt.date.today()
    dni = arg.dni if dane["aktywnosci"] else max(arg.dni, DNI_PIERWSZY_RAZ)
    od = dzis - dt.timedelta(days=dni)

    client = zaloguj()
    surowe = lista_aktywnosci(client, od, dzis)
    print(f"Garmin: {len(surowe)} aktywności od {od}.")

    stare = {a["id"]: a for a in dane["aktywnosci"]}
    nowe = []
    for a in surowe:
        r = rekord(a)
        if not r.get("id") or not r["data"]:
            continue
        poprz = stare.get(r["id"])
        if r["sport"] == "bieg" and not arg.bez_odcinkow:
            if poprz and "odcinki" in poprz:       # okrążenia się nie zmieniają – bez ponownego pytania
                r["odcinki"] = poprz["odcinki"]
            else:
                o = odcinki(client, r["id"])
                if o:
                    r["odcinki"] = o
        nowe.append(r)

    # okno odświeżania zastępujemy w całości (aktywność usunięta w Garminie znika też tu)
    zostaja = [a for a in dane["aktywnosci"] if a.get("data", "") < od.isoformat()]
    wynik = sorted(zostaja + nowe, key=lambda a: (a["data"], a.get("godz", ""), a["id"]))

    if wynik == dane["aktywnosci"] and not arg.zawsze_zapisz:
        print("Bez zmian.")
        return
    dodane = len({a["id"] for a in wynik} - set(stare))
    usuniete = len(set(stare) - {a["id"] for a in wynik})
    dane = {"zaktualizowano": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "aktywnosci": wynik}
    zapisz(dane)
    print(f"Zapisano {PLIK}: {len(wynik)} aktywności (nowe: {dodane}, usunięte: {usuniete}).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
