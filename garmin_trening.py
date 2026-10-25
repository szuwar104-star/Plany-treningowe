#!/usr/bin/env python3
"""
Dodaje trening biegowy do Garmin Connect (Treningi -> Moje treningi)
i opcjonalnie planuje go w kalendarzu.

Instalacja:
    pip install --upgrade garminconnect curl_cffi

Użycie:
    python garmin_trening.py                      # dodaje trening z sekcji TRENING
    python garmin_trening.py --data 2026-10-25    # dodaje i planuje na dzień
    python garmin_trening.py --dry-run            # tylko pokazuje JSON, nic nie wysyła
    python garmin_trening.py --plan               # dodaje WSZYSTKIE treningi z sekcji PLAN
                                                  # i planuje je w kalendarzu na podane daty
    python garmin_trening.py --plan --dry-run     # podgląd JSON całego planu
    python garmin_trening.py --plan --bez-opcjonalnych
                                                  # plan bez dni opcjonalnych
                                                  # (wolne lub krótki rozruch)
    python garmin_trening.py --plan --tylko-kalendarz
                                                  # nic nie wysyła, tylko wstawia do kalendarza
                                                  # treningi już istniejące w "Moje treningi"

    python garmin_trening.py --plan --tak         # jak --plan, ale bez pytania o zgodę
                                                  # na wymianę starych treningów

    python garmin_trening.py --plan --sprzataj    # usuwa duplikaty z planu (pyta o zgodę)

Plan z pliku (YAML/JSON) zamiast sekcji PLAN – lokalnie albo z GitHuba:
    python garmin_trening.py --zrodlo plan.yaml --dry-run
    python garmin_trening.py --zrodlo https://api.github.com/repos/LOGIN/REPO/contents/plan.yaml?ref=main
                                                  # repo prywatne: token w GITHUB_TOKEN
    Adres można też ustawić raz w zmiennej GARMIN_PLAN_ZRODLO.
    Brak pliku = nic się nie dzieje. Plik niezmieniony od ostatniego udanego
    uruchomienia = nic się nie dzieje (wymuszenie: --zawsze), więc można to
    puszczać z harmonogramu np. co godzinę.

Treningi siłowe: sekcja zestawy_sily w pliku planu (opis w plan.yaml).
    python garmin_trening.py --cwiczenia plank    # nazwy ćwiczeń z katalogu Garmina

Tokeny dla GitHub Actions:
    python garmin_trening.py --eksport-tokenow    # wypisuje tekst do sekretu GARMIN_TOKENS

Wymiana starych treningów (przy --plan, po potwierdzeniu, albo od razu z --tak):
- wpisy w kalendarzu w zakresie dat planu, których nie ma w planie, są usuwane,
- trening o tej samej nazwie, ale innej treści (inny opis) jest wymieniany
  na nowy – także w kalendarzu,
- treningi ze starego planu (STARE_NAZWY) są usuwane z "Moje treningi".

Zabezpieczenia przed duplikatami:
- trening o tej samej nazwie, który już jest w "Moje treningi", nie jest
  wysyłany drugi raz – skrypt bierze istniejący,
- jeśli w danym dniu kalendarza jest już trening o tej nazwie, skrypt go
  nie planuje ponownie,
- można więc bezpiecznie uruchamiać skrypt wiele razy.

Pierwsze uruchomienie pyta o e-mail, hasło (i ewentualnie kod MFA),
potem tokeny są trzymane w ~/.garminconnect i logowanie jest automatyczne.
Dane logowania można też podać w zmiennych GARMIN_EMAIL / GARMIN_PASSWORD.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from getpass import getpass

TOKENSTORE = os.path.expanduser("~/.garminconnect")
OSTATNI_PLAN = os.path.expanduser("~/.garmin_plan.sha256")

# ============================================================================
#  DEFINICJA TRENINGU – edytuj tutaj
# ============================================================================
# Dostępne kroki:
#   rozgrzewka(...)  bieg(...)  odpoczynek(...)  schladzanie(...)
#   powtorz(n, [kroki...])
# Czas trwania kroku (jedno z): lap=True | min=10 | sek=90 | km=1.0 | m=400
# Cel (opcjonalnie):          tempo=("5:00", "5:30")  [min/km]  albo  strefa_hr=2
# Notatka do kroku:           notatka="..."


def TRENING():
    return {
        "nazwa": "Interwały 5x1 km",
        "opis": "Tempo progowe, przerwy w truchcie",
        "kroki": [
            rozgrzewka(lap=True),
            powtorz(5, [
                bieg(km=1, tempo=("4:50", "5:10")),
                odpoczynek(min=2),
            ]),
            schladzanie(lap=True),
        ],
    }

# ============================================================================

# ============================================================================
#  PLAN 26.10 - 20.12  (plan biegowy 10 km, 8 tygodni; używany z --plan)
# ============================================================================
# Źródło: plan_biegowy_26_10-20_12.ics
# Każda pozycja: data, nazwa, opis, funkcja zwracająca listę kroków,
# opcjonalnie "opcjonalny": True (pomijane z --bez-opcjonalnych).
# Ta sama nazwa = ten sam trening w "Moje treningi" (wstawiany na kilka dat).
# Cele tempa na zegarku są poszerzone o 2 s/km z każdej strony (mniej
# fałszywych alarmów), dokładny cel jest w notatce kroku.

STOP = (" Ból pachwiny/przywodzicieli/biodra albo nietypowa duszność/ból"
        " w klatce = koniec treningu.")
STOP_PROG = ("Zmniejsz/przerwij: tempo spada o >3 s/km vs 1. seria, RPE >17"
             " przed ostatnią serią, nogi drewniane już w rozgrzewce.")
STOP_VO2 = ("Zmniejsz/przerwij: powtórzenie wolniejsze niż zakres o >3 s/km"
            " – kończysz serię i schłodzenie.")
SILA = (" + Siła/profilaktyka 15-20 min (przywodziciele, Copenhagen plank,"
        " pośladki, ćwiczenia jednonóż).")


def _luz(od, do, s=2):
    """('4:11', '4:14') -> ('4:09', '4:16')"""
    def sek(t):
        m, ss = t.split(":")
        return int(m) * 60 + int(ss)

    def txt(x):
        return f"{x // 60}:{x % 60:02d}"
    return txt(sek(od) - s), txt(sek(do) + s)


def _przebiezki():
    return powtorz(4, [bieg(m=100, notatka="Przebieżka, lekko i płynnie"),
                       odpoczynek(sek=40, notatka="Trucht/marsz")])


def _rozgrzewka_std():
    return [rozgrzewka(km=3, notatka="Spokojnie 5:30-6:00/km"),
            _przebiezki(),
            odpoczynek(min=2)]


def _schlodzenie_std():
    return schladzanie(km=2, notatka="Spokojnie 5:45-6:00/km, możesz do 3 km")


def _spokojny(km, opcja=0, przebiezki=False):
    kroki = [bieg(km=km, notatka="Spokojnie 5:30-6:00/km, tętno poniżej ok. 140")]
    if opcja:
        kroki.append(bieg(km=opcja, notatka="OPCJONALNIE, jeśli nogi lekkie. "
                                             "Inaczej pomiń krok."))
    if przebiezki:
        kroki.append(_przebiezki())
    return kroki


def _dlugi(km, opcja=0):
    kroki = [bieg(km=km, notatka="Równo 5:20-5:35/km, tętno ok. 140-150. "
                                 "Poniżej 5:15 - zwolnij.")]
    if opcja:
        kroki.append(bieg(km=opcja, notatka="OPCJONALNIE, spokojnie. Przy ciężkich "
                                             "nogach pomiń krok."))
    return kroki


def spokojny(data, km_txt, km, opcja=0, przebiezki=False, sila=True):
    nazwa = (f"Spokojny {km_txt} km" + (" + przebieżki" if przebiezki else "")
             + ("" if sila else " (przedstartowy)"))
    opis = ("Regeneracja i baza tlenowa. 5:30-6:00/km (ze wspólnego biegu do 6:00),"
            " tętno poniżej ok. 140. Skróć/zwolnij przy ciężkich nogach lub HRV"
            " < ~70 ms kilka dni z rzędu (rano HRV <70 - skróć do 5-6 km)."
            + (" Na końcu 4x100 m przebieżki." if przebiezki else "")
            + (SILA if sila else "") + STOP)
    return {"data": data, "nazwa": nazwa, "opis": opis,
            "kroki": lambda: _spokojny(km, opcja, przebiezki)}


def opcja_rozruch(data):
    return {"data": data, "nazwa": "Opcja: wolne lub 5-6 km", "opcjonalny": True,
            "opis": ("Wolne albo 5-6 km bardzo spokojnie (5:50-6:10/km). Rozruch,"
                     " nie kilometraż. Po ciężkim akcencie albo przy niskim HRV"
                     " zostaw wolne." + STOP),
            "kroki": lambda: [
                bieg(km=5, notatka="Bardzo spokojnie 5:50-6:10/km"),
                bieg(km=1, notatka="OPCJONALNIE 6. km. Inaczej pomiń krok.")]}


def dlugi(data, km_txt, km, opcja=0, uwaga="Bez mocnej końcówki."):
    return {"data": data, "nazwa": f"Długi {km_txt} km",
            "opis": (f"Wytrzymałość tlenowa. {km_txt} km w 5:20-5:35/km, równo,"
                     f" tętno ok. 140-150. {uwaga} Poniżej 5:15 - zwolnij. Przy"
                     f" ciężkich nogach skróć o 2-4 km." + STOP),
            "kroki": lambda: _dlugi(km, opcja)}


def akcent(data, nazwa, cel, n, m, od, do, przerwa, przerwa_txt, stop,
           uwaga="", dodatkowe=None):
    """Rozgrzewka 3 km + przebieżki, n x m metrów, przerwy w truchcie, schłodzenie."""
    def kroki():
        notatka = f"Cel {od}-{do}/km. {uwaga}".strip()
        k = _rozgrzewka_std() + [
            powtorz(n, [bieg(m=m, tempo=_luz(od, do), notatka=notatka),
                        odpoczynek(notatka=f"Trucht {przerwa_txt}", **przerwa)])]
        if dodatkowe:
            k.append(bieg(m=m, tempo=_luz(od, do), notatka=dodatkowe))
        return k + [_schlodzenie_std()]

    opis = (f"{cel} Rozgrzewka 3 km + 4x100 m. {n}x{m} m w {od}-{do}/km."
            f" {uwaga} Przerwy {przerwa_txt} truchtu. Schłodzenie 2-3 km"
            f" 5:45-6:00/km. {stop}{STOP}")
    return {"data": data, "nazwa": nazwa, "opis": " ".join(opis.split()),
            "kroki": kroki}


def _test_5km():
    return _rozgrzewka_std()[:-1] + [
        odpoczynek(min=5, notatka="Przerwa przed testem"),
        bieg(km=2, tempo=_luz("3:56", "3:58"),
             notatka="Start 3:56-3:58/km, NIE szybciej niż 3:56"),
        bieg(km=2, tempo=_luz("3:56", "3:58"), notatka="Utrzymaj tempo, równo"),
        bieg(km=1, notatka="Ostatnie 1000 m - dociśnij"),
        schladzanie(km=2, notatka="Spokojnie, możesz do 3 km"),
    ]


def _start_10km():
    return [
        rozgrzewka(km=2, notatka="Trucht, możesz do 3 km"),
        _przebiezki(),
        odpoczynek(lap=True, notatka="Czekaj na start - naciśnij Lap na starcie"),
        bieg(km=2, tempo=_luz("4:03", "4:05"),
             notatka="Pierwsze 2 km 4:03-4:05/km, NIE szybciej"),
        bieg(km=6, tempo=_luz("4:00", "4:03"), notatka="3-8 km równo ok. 4:00-4:03/km"),
        bieg(km=2, notatka="Ostatnie 2 km - dociśnij według samopoczucia"),
        schladzanie(km=2, notatka="Spokojnie"),
    ]


PLAN = [
    # --- Tydzień 1/8 ---
    spokojny("2026-10-26", "7-8", 7, opcja=1),
    akcent("2026-10-27", "T1 Próg 3x2000", "Wytrzymałość tempowa, tempo bliskie 10 km.",
           3, 2000, "4:11", "4:14", {"min": 2}, "2:00",
           STOP_PROG + " Druga seria wolniejsza niż 4:16 - kończysz.",
           uwaga="Nie szybciej niż 4:10."),
    opcja_rozruch("2026-10-28"),
    spokojny("2026-10-29", "8-9", 8, opcja=1),
    akcent("2026-10-30", "T1 VO2 5-6x1000", "Utrzymanie szybkości i VO2max.",
           5, 1000, "3:52", "3:56", {"min": 2, "sek": 45}, "2:45",
           "Zmniejsz/przerwij: 4. powtórzenie wolniejsze niż 3:58 - kończysz na 4.",
           dodatkowe=("OPCJONALNIE 6. powtórzenie: tylko jeśli wtorek RPE <=17 "
                      "i HRV w normie. Inaczej pomiń krok.")),
    opcja_rozruch("2026-10-31"),
    dlugi("2026-11-01", "18-20", 18, opcja=2),
    # --- Tydzień 2/8 ---
    spokojny("2026-11-02", "7-8", 7, opcja=1),
    akcent("2026-11-03", "T2 Próg 2x3000", "Wytrzymałość tempowa - dłuższe odcinki.",
           2, 3000, "4:11", "4:14", {"min": 2, "sek": 30}, "2:30", STOP_PROG),
    opcja_rozruch("2026-11-04"),
    spokojny("2026-11-05", "8-9", 8, opcja=1),
    akcent("2026-11-06", "T2 VO2 5x1200", "VO2max i ekonomia biegu.",
           5, 1200, "3:55", "3:58", {"min": 2, "sek": 45}, "2:45", STOP_VO2),
    opcja_rozruch("2026-11-07"),
    dlugi("2026-11-08", "20", 20),
    # --- Tydzień 3/8 ---
    spokojny("2026-11-09", "7-8", 7, opcja=1),
    akcent("2026-11-10", "T3 Próg 4x2000",
           "Wytrzymałość tempowa - największa objętość progu w bloku (8 km).",
           4, 2000, "4:10", "4:13", {"min": 1, "sek": 45}, "1:45",
           STOP_PROG + " Po 3. serii RPE >17 - kończysz na 3."),
    opcja_rozruch("2026-11-11"),
    spokojny("2026-11-12", "8-9", 8, opcja=1),
    akcent("2026-11-13", "T3 Szybkość 8x400", "Szybkość i ekonomia, krótkie odcinki.",
           8, 400, "3:45", "3:50", {"m": 200}, "200 m", STOP_VO2,
           uwaga="Ok. 1:30-1:32 na odcinek."),
    opcja_rozruch("2026-11-14"),
    dlugi("2026-11-15", "20", 20),
    # --- Tydzień 4/8 (odciążenie + test) ---
    spokojny("2026-11-16", "6-7", 6, opcja=1),
    akcent("2026-11-17", "T4 Próg 4x1000 lekko",
           "Utrzymanie progu w tygodniu odciążenia.",
           4, 1000, "4:08", "4:10", {"sek": 90}, "90 s", STOP_PROG,
           uwaga="Kontrolowane, RPE <=15."),
    opcja_rozruch("2026-11-18"),
    spokojny("2026-11-19", "6", 6, przebiezki=True),
    {"data": "2026-11-20", "nazwa": "T4 TEST 5 km",
     "opis": ("Test formy - porównanie z 19:44 z 13.10. Rozgrzewka 3 km + 4x100 m"
              " + 5 min przerwy. 5 km maksymalnie równo: start 3:56-3:58/km (pierwsze"
              " 2 km nie szybciej niż 3:56), od 3 km utrzymaj, ostatnie 1000 m dociśnij."
              " Wynik: <=19:15 sub-40 realne; 19:15-19:40 cel 40:30-40:50; >19:40"
              " sub-40 na wiosnę. Przy wietrze/lodzie, złym samopoczuciu lub niskim HRV"
              " przełóż o dzień." + STOP),
     "kroki": _test_5km},
    opcja_rozruch("2026-11-21"),
    dlugi("2026-11-22", "14-16", 14, opcja=2,
          uwaga="Tydzień odciążenia - spokojnie."),
    # --- Tydzień 5/8 ---
    spokojny("2026-11-23", "7-8", 7, opcja=1),
    akcent("2026-11-24", "T5 Próg 2x4000",
           "Wytrzymałość tempowa, długie odcinki. Tempo skoryguj po teście"
           " (próg ok. 6-7% wolniej niż 5 km).",
           2, 4000, "4:10", "4:13", {"min": 2, "sek": 30}, "2:30", STOP_PROG),
    opcja_rozruch("2026-11-25"),
    spokojny("2026-11-26", "8-9", 8, opcja=1),
    akcent("2026-11-27", "T5 VO2 5x1000", "VO2max.",
           5, 1000, "3:52", "3:55", {"min": 2, "sek": 45}, "2:45", STOP_VO2,
           uwaga="Tempo = 5 km z testu minus 2-3 s/km."),
    opcja_rozruch("2026-11-28"),
    dlugi("2026-11-29", "20", 20),
    # --- Tydzień 6/8 ---
    spokojny("2026-11-30", "7-8", 7, opcja=1),
    akcent("2026-12-01", "T6 Próg 3x3000",
           "Wytrzymałość tempowa. Tempo skoryguj po teście 5 km.",
           3, 3000, "4:09", "4:12", {"min": 2}, "2:00",
           STOP_PROG + " Po 2. serii RPE >17 - kończysz na dwóch."),
    opcja_rozruch("2026-12-02"),
    spokojny("2026-12-03", "8-9", 8, opcja=1),
    akcent("2026-12-04", "T6 Tempo 5 km 4x1600", "Tempo 5 km, utrzymanie szybkości.",
           4, 1600, "3:56", "3:58", {"min": 2, "sek": 30}, "2:30", STOP_VO2),
    opcja_rozruch("2026-12-05"),
    dlugi("2026-12-06", "20", 20),
    # --- Tydzień 7/8 ---
    spokojny("2026-12-07", "7-8", 7, opcja=1),
    akcent("2026-12-08", "T7 Tempo startowe 3x2000",
           "Specyficzność - tempo docelowe na 10 km.",
           3, 2000, "4:02", "4:05", {"min": 2}, "2:00",
           STOP_PROG + " Tempo nie trzyma się w 3. serii - kończysz na dwóch.",
           uwaga="Jeśli test 5 km nie wskazuje na sub-40: 4:06-4:08."),
    opcja_rozruch("2026-12-09"),
    spokojny("2026-12-10", "8-9", 8, opcja=1),
    akcent("2026-12-11", "T7 VO2 5x1000", "Utrzymanie szybkości przed startem.",
           5, 1000, "3:52", "3:55", {"min": 2, "sek": 45}, "2:45", STOP_VO2),
    opcja_rozruch("2026-12-12"),
    dlugi("2026-12-13", "18", 18),
    # --- Tydzień 8/8 (start) ---
    spokojny("2026-12-14", "6-7", 6, opcja=1),
    akcent("2026-12-15", "T8 Wyostrzenie 4x1000",
           "Wyostrzenie przed startem, bez zmęczenia.",
           4, 1000, "4:00", "4:02", {"sek": 90}, "90 s",
           "Cokolwiek wymaga wysiłku ponad RPE 15 - kończysz.",
           uwaga="Kontrolowane."),
    opcja_rozruch("2026-12-16"),
    spokojny("2026-12-17", "6", 6, przebiezki=True, sila=False),
    {"data": "2026-12-18", "nazwa": "Rozruch 4-5 km + przebieżki",
     "opis": ("4-5 km spokojnie (5:40-6:00/km) + 4x100 m przebieżki. Dzień przed"
              " ostatnim wolnym: lekko, nic nowego." + STOP),
     "kroki": lambda: [bieg(km=4, notatka="Spokojnie 5:40-6:00/km"),
                       bieg(km=1, notatka="OPCJONALNIE 5. km. Inaczej pomiń krok."),
                       _przebiezki()]},
    {"data": "2026-12-19", "nazwa": "Opcja: wolne lub trucht 2-3 km",
     "opcjonalny": True,
     "opis": ("Wolne albo 2-3 km truchtu. Sprzęt, śniadanie i dojazd przygotuj"
              " dzień wcześniej."),
     "kroki": lambda: [bieg(km=2, notatka="Trucht, możesz do 3 km")]},
    {"data": "2026-12-20", "nazwa": "START 10 km",
     "opis": ("Start 10 km. Rozgrzewka 2-3 km truchtu + 4x100 m. Pierwsze 2 km"
              " 4:03-4:05/km (nie szybciej), 3-8 km równo ok. 4:00-4:03, ostatnie"
              " 2 km dociśnij. Tempo skoryguj wynikiem testu 5 km. Bez ibuprofenu;"
              " kofeina tylko jeśli przetestowana na akcencie; śniadanie 2-3 h przed,"
              " nic nowego. Schłodzenie 2 km." + STOP),
     "kroki": _start_10km},
]


# ============================================================================
#  PLAN Z PLIKU (YAML / JSON) – używany z --zrodlo
# ============================================================================
# Format opisany w plan.yaml. Szablony: spokojny, dlugi, akcent, opcja_rozruch;
# bez szablonu: nazwa + opis + kroki (lista kroków albo test_5km / start_10km).

_GOTOWE_KROKI = {"test_5km": _test_5km, "start_10km": _start_10km}
_STALE_STOP = {"PROG": STOP_PROG, "VO2": STOP_VO2}


def _mmss(v):
    """YAML czyta 4:11 bez cudzysłowu jako 251 (sekundy) – zamieniamy z powrotem."""
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v // 60}:{v % 60:02d}"
    return str(v)


def _kroki_z_pliku(lista):
    rodzaje = {"rozgrzewka": rozgrzewka, "bieg": bieg,
               "odpoczynek": odpoczynek, "schladzanie": schladzanie,
               "cwiczenie": cwiczenie, "przerwa": przerwa}
    wynik = []
    for k in lista:
        if not isinstance(k, dict) or not k:
            raise ValueError(f"krok musi być słownikiem, jest: {k!r}")
        if "powtorz" in k:
            wynik.append(powtorz(k["powtorz"], _kroki_z_pliku(k.get("kroki") or [])))
            continue
        if len(k) != 1:
            raise ValueError(f"krok ma mieć jeden klucz (rodzaj kroku): {k!r}")
        (typ, par), = k.items()
        if typ == "przebiezki":
            wynik.append(_przebiezki())
        elif typ == "rozgrzewka_std":
            wynik += _rozgrzewka_std()
        elif typ == "schlodzenie_std":
            wynik.append(_schlodzenie_std())
        elif typ in rodzaje:
            if typ == "cwiczenie" and isinstance(par, str):
                par = {"co": par}
            par = dict(par or {})
            if "tempo" in par:
                od, do_ = (_mmss(t) for t in par["tempo"])
                par["tempo"] = _luz(od, do_, s=int(par.pop("luz", 0)))
            wynik.append(rodzaje[typ](**par))
        else:
            raise ValueError(f"nieznany rodzaj kroku: {typ}")
    return wynik


def _wpis_z_pliku(w, zestawy=None):
    w = dict(w)
    d = str(w.pop("data"))
    szablon = w.pop("szablon", None)
    opcjonalny = w.pop("opcjonalny", None)

    if szablon is None and "sila" in w:  # trening siłowy z sekcji zestawy_sily
        klucz = str(w.pop("sila"))
        if not zestawy or klucz not in zestawy:
            raise ValueError(f"brak zestawu siłowego '{klucz}' w sekcji zestawy_sily")
        z = dict(zestawy[klucz])
        w = {**z, **w, "sport": "sila"}
        w.setdefault("nazwa", f"Siła {klucz}")

    if szablon == "spokojny":
        wynik = spokojny(d, str(w.pop("km_txt")), **w)
    elif szablon == "dlugi":
        wynik = dlugi(d, str(w.pop("km_txt")), **w)
    elif szablon == "opcja_rozruch":
        wynik = opcja_rozruch(d)
    elif szablon == "akcent":
        od, do_ = (_mmss(t) for t in w["tempo"])
        stop = _STALE_STOP.get(str(w.get("stop", "")).upper(), w.get("stop", ""))
        if w.get("stop_dodatek"):
            stop = f"{stop} {w['stop_dodatek']}"
        wynik = akcent(d, w["nazwa"], w["cel"], w["powtorzenia"], w["metry"], od, do_,
                       w["przerwa"], _mmss(w["przerwa_txt"]), stop,
                       uwaga=w.get("uwaga", ""), dodatkowe=w.get("dodatkowe"))
    elif szablon is None:
        kroki = w["kroki"]
        if isinstance(kroki, str):
            if kroki not in _GOTOWE_KROKI:
                raise ValueError(f"nieznany zestaw kroków: {kroki}")
            fn = _GOTOWE_KROKI[kroki]
        else:
            fn = (lambda k=kroki: _kroki_z_pliku(k))
        opis = w.get("opis", "") + (STOP if w.get("dopisz_stop") else "")
        sport = str(w.get("sport", "bieg"))
        if sport not in SPORTY:
            raise ValueError(f"nieznany sport: {sport} (dostępne: {', '.join(SPORTY)})")
        wynik = {"data": d, "nazwa": str(w["nazwa"]), "opis": opis, "kroki": fn,
                 "sport": sport}
    else:
        raise ValueError(f"nieznany szablon: {szablon}")

    if opcjonalny is not None:
        wynik["opcjonalny"] = bool(opcjonalny)
    return wynik


def pobierz_zrodlo(zrodlo):
    """Tekst pliku planu (ścieżka albo URL) albo None, gdy pliku nie ma."""
    if zrodlo.startswith(("http://", "https://")):
        req = urllib.request.Request(zrodlo, headers={"User-Agent": "garmin-trening"})
        if "api.github.com" in zrodlo:
            req.add_header("Accept", "application/vnd.github.raw")
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    if not os.path.exists(zrodlo):
        return None
    with open(zrodlo, encoding="utf-8") as f:
        return f.read()


def plan_z_tekstu(tekst, zrodlo):
    """-> (plan, stare_nazwy albo None)"""
    sciezka = urllib.parse.urlparse(zrodlo).path if "://" in zrodlo else zrodlo
    if sciezka.lower().endswith(".json"):
        dane = json.loads(tekst)
    else:
        try:
            import yaml
        except ImportError:
            sys.exit("Do plików YAML potrzebny jest pakiet: pip install pyyaml")
        dane = yaml.safe_load(tekst)
    if isinstance(dane, list):
        dane = {"treningi": dane}
    dane = dane or {}

    zestawy = dane.get("zestawy_sily") or {}
    zestawy = {str(k): v for k, v in zestawy.items()}
    plan = []
    for i, w in enumerate(dane.get("treningi") or [], 1):
        try:
            p = _wpis_z_pliku(w, zestawy)
            p["kroki"]()  # od razu sprawdza kroki, zanim cokolwiek pójdzie do Garmina
        except KeyError as e:
            sys.exit(f"Błąd w pliku planu, pozycja {i} ({w.get('data', '?')}"
                     f" {w.get('nazwa', w.get('szablon', ''))}): brak pola {e}")
        except Exception as e:
            sys.exit(f"Błąd w pliku planu, pozycja {i} ({w.get('data', '?')}"
                     f" {w.get('nazwa', w.get('szablon', ''))}): {e}")
        plan.append(p)
    return plan, dane.get("stare_nazwy")



STEP_TYPES = {
    "warmup": 1, "cooldown": 2, "interval": 3,
    "recovery": 4, "rest": 5, "repeat": 6,
}
CONDITIONS = {
    "lap.button": 1, "time": 2, "distance": 3, "iterations": 7, "reps": 10,
}
SPORTY = {
    "bieg": {"sportTypeId": 1, "sportTypeKey": "running"},
    "sila": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
}
TARGETS = {
    "no.target": 1, "heart.rate.zone": 4, "pace.zone": 6,
}


def _tempo_na_ms(tempo: str) -> float:
    """'5:00' (min/km) -> m/s"""
    m, s = tempo.split(":")
    sekundy = int(m) * 60 + int(s)
    return 1000 / sekundy


def _krok(typ, lap=False, min=None, sek=None, km=None, m=None,
          tempo=None, strefa_hr=None, notatka=None, powt=None):
    step = {
        "type": "ExecutableStepDTO",
        "stepType": {"stepTypeId": STEP_TYPES[typ], "stepTypeKey": typ},
    }

    # --- czas trwania ---
    if powt is not None:
        step["endCondition"] = {"conditionTypeId": CONDITIONS["reps"],
                                "conditionTypeKey": "reps"}
        step["endConditionValue"] = float(powt)
    elif km is not None or m is not None:
        metry = km * 1000 if km is not None else m
        step["endCondition"] = {"conditionTypeId": CONDITIONS["distance"],
                                "conditionTypeKey": "distance"}
        step["endConditionValue"] = float(metry)
        step["preferredEndConditionUnit"] = {
            "unitKey": "kilometer" if km is not None else "meter"}
    elif min is not None or sek is not None:
        step["endCondition"] = {"conditionTypeId": CONDITIONS["time"],
                                "conditionTypeKey": "time"}
        step["endConditionValue"] = float((min or 0) * 60 + (sek or 0))
    else:  # domyślnie / lap=True – naciśnięcie przycisku Lap
        step["endCondition"] = {"conditionTypeId": CONDITIONS["lap.button"],
                                "conditionTypeKey": "lap.button"}

    # --- docelowa intensywność ---
    if tempo:
        v1, v2 = sorted(_tempo_na_ms(t) for t in tempo)
        step["targetType"] = {"workoutTargetTypeId": TARGETS["pace.zone"],
                              "workoutTargetTypeKey": "pace.zone"}
        step["targetValueOne"] = v1
        step["targetValueTwo"] = v2
    elif strefa_hr:
        step["targetType"] = {"workoutTargetTypeId": TARGETS["heart.rate.zone"],
                              "workoutTargetTypeKey": "heart.rate.zone"}
        step["zoneNumber"] = int(strefa_hr)
    else:
        step["targetType"] = {"workoutTargetTypeId": TARGETS["no.target"],
                              "workoutTargetTypeKey": "no.target"}

    if notatka:
        step["description"] = notatka[:200]
    return step


def rozgrzewka(**kw):  return _krok("warmup", **kw)
def bieg(**kw):        return _krok("interval", **kw)
def odpoczynek(**kw):  return _krok("recovery", **kw)
def schladzanie(**kw): return _krok("cooldown", **kw)
def przerwa(**kw):     return _krok("rest", **kw)


# ============================================================================
#  ĆWICZENIA SIŁOWE (Garmin: kategoria + nazwa z katalogu Garmina)
# ============================================================================
# Polskie nazwy -> kandydaci (KATEGORIA, NAZWA). Skrypt bierze pierwszego, który
# jest w katalogu Garmina; nazwy spoza katalogu Garmin pomija, więc ćwiczenie
# zostaje wtedy opisane w notatce kroku. Własne nazwy: --cwiczenia SZUKAJ.
ALIASY = {
    # (KATEGORIA, NAZWA) sprawdzone w katalogu ćwiczeń Garmina
    "pompki": ("PUSH_UP", "PUSH_UP"),
    "pompki_na_kolanach": ("PUSH_UP", "KNEELING_PUSH_UP"),
    "przysiad": ("SQUAT", "AIR_SQUAT"),
    "przysiad_jednonoz": ("SQUAT", "PARTIAL_SINGLE_LEG_SQUAT"),
    "przysiad_bulgarski": ("LUNGE", "DUMBBELL_BULGARIAN_SPLIT_SQUAT"),
    "wykroki": ("LUNGE", "WALKING_LUNGE"),
    "wykroki_wstecz": ("LUNGE", "REVERSE_LUNGE_WITH_REACH_BACK"),
    "wykroki_boczne": ("LUNGE", "SIDE_LUNGE"),
    "plank": ("PLANK", "STRAIGHT_ARM_PLANK"),
    "plank_boczny": ("PLANK", "SIDE_PLANK"),
    "plank_boczny_noga": ("PLANK", "SIDE_PLANK_WITH_LEG_LIFT"),
    "copenhagen": ("PLANK", "SIDE_PLANK_WITH_FEET_ON_BENCH"),
    "przywodziciele": ("HIP_STABILITY", "INNER_THIGH_LIFT"),
    "mostek": ("HIP_RAISE", "HIP_RAISE"),
    "mostek_jednonoz": ("HIP_RAISE", "SINGLE_LEG_HIP_RAISE"),
    "mostek_marsz": ("HIP_RAISE", "MARCHING_HIP_RAISE"),
    "martwy_jednonoz": ("DEADLIFT", "SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL"),
    "dead_bug": ("HIP_STABILITY", "DEAD_BUG"),
    "bird_dog": ("HYPEREXTENSION", "BACK_EXTENSION_WITH_OPPOSITE_ARM_AND_LEG_REACH"),
    "brzuszki": ("CRUNCH", "CRUNCH"),
    "rowerek": ("CORE", "BICYCLE"),
    "unoszenie_nog": ("LEG_RAISE", "LYING_STRAIGHT_LEG_RAISE"),
    "hollow": ("CRUNCH", "HOLLOW_ROCK"),
    "superman": ("HYPEREXTENSION", "SUPERMAN_FROM_FLOOR"),
    "wspiecia": ("CALF_RAISE", "STANDING_CALF_RAISE"),
    "wspiecia_jednonoz": ("CALF_RAISE", "SINGLE_LEG_STANDING_CALF_RAISE"),
    "muszelka": ("HIP_RAISE", "CLAMS"),
    "wymachy_bok": ("HIP_STABILITY", "SIDE_LYING_LEG_RAISE"),
    "mountain_climber": ("PLANK", "MOUNTAIN_CLIMBER"),
    "burpee": ("TOTAL_BODY", "BURPEE"),
    "podciaganie": ("PULL_UP", "PULL_UP"),
    "dipy": ("TRICEPS_EXTENSION", "BENCH_DIP"),
    "wejscia_na_podwyzszenie": ("STAIR_STEPPER", "STEP_UP"),
}
KATALOG_URL = "https://connect.garmin.com/web-data/exercises/Exercises.json"
KATALOG_PLIK = os.path.expanduser("~/.garmin_cwiczenia.json")
_katalog_cache = None
_ostrzezenia = set()


def _katalog():
    """{(KATEGORIA, NAZWA)} z katalogu Garmina; pusty zbiór, gdy niedostępny."""
    global _katalog_cache
    if _katalog_cache is not None:
        return _katalog_cache
    dane = None
    try:
        if os.path.exists(KATALOG_PLIK) and (os.path.getmtime(KATALOG_PLIK)
                                             > __import__("time").time() - 30 * 86400):
            with open(KATALOG_PLIK, encoding="utf-8") as f:
                dane = json.load(f)
    except Exception:
        dane = None
    if dane is None:
        try:
            req = urllib.request.Request(KATALOG_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                dane = json.loads(r.read().decode("utf-8"))
            try:
                with open(KATALOG_PLIK, "w", encoding="utf-8") as f:
                    json.dump(dane, f)
            except OSError:
                pass
        except Exception as e:
            print(f"Uwaga: nie pobrałem katalogu ćwiczeń Garmina ({e}) – nazwy nie są sprawdzane.")
            dane = {}
    pary = set()
    kategorie = dane.get("categories", dane) if isinstance(dane, dict) else {}
    for kat, zawart in (kategorie.items() if isinstance(kategorie, dict) else []):
        cw = zawart.get("exercises", zawart) if isinstance(zawart, dict) else {}
        if isinstance(cw, dict):
            pary |= {(str(kat).upper(), str(n).upper()) for n in cw}
        elif isinstance(cw, list):
            pary |= {(str(kat).upper(), str(n).upper()) for n in cw if isinstance(n, str)}
    _katalog_cache = pary
    return pary


def _znajdz_cwiczenie(co=None, kategoria=None, nazwa=None):
    """-> (kategoria, nazwa, rozpoznane). Nierozpoznane ćwiczenie idzie bez nazwy
    (z opisem w notatce) – błędna nazwa potrafi zablokować trening na zegarku."""
    if co is not None:
        klucz = str(co).strip().lower().replace(" ", "_").replace("-", "_")
        if klucz in ALIASY:
            k, n = ALIASY[klucz]
            return k, n, True
        nazwa = klucz.upper()
    katalog = _katalog()
    n = (nazwa or "").upper().replace(" ", "_")
    k = (kategoria or "").upper()
    if katalog:
        if k and (k, n) in katalog:
            return k, n, True
        trafienia = [kk for kk, nn in katalog if nn == n]
        if n and not k and len(trafienia) == 1:
            return trafienia[0], n, True
    return None, None, False


def cwiczenie(co=None, kategoria=None, nazwa=None, notatka=None, **kw):
    """Krok siłowy: powt=12 | sek=45 | min=1 | lap=True (domyślnie)."""
    if not (co or kategoria or nazwa):
        raise ValueError("ćwiczenie wymaga pola co (np. plank) albo kategoria/nazwa")
    kat, naz, ok = _znajdz_cwiczenie(co, kategoria, nazwa)
    opis_cw = str(co or nazwa or kategoria).replace("_", " ")
    if not ok and opis_cw not in _ostrzezenia:
        _ostrzezenia.add(opis_cw)
        print(f"Uwaga: '{opis_cw}' nie ma odpowiednika w katalogu Garmina"
              f" – na zegarku będzie opisane w notatce kroku.")
    tekst = notatka or ""
    if not ok:  # bez nazwy Garmina – nazwa ćwiczenia trafia do notatki
        tekst = f"{opis_cw.capitalize()}. {tekst}".strip()
    step = _krok("interval", notatka=tekst or None, **kw)
    if kat and naz:
        step["category"] = kat
        step["exerciseName"] = naz
    return step


def wypisz_cwiczenia(szukaj):
    katalog = _katalog()
    if not katalog:
        sys.exit("Katalog Garmina niedostępny.")
    s = (szukaj or "").upper().replace(" ", "_")
    wynik = sorted((k, n) for k, n in katalog if s in n or s in k)
    for k, n in wynik:
        print(f"  {n:45} kategoria: {k}")
    print(f"{len(wynik)} pozycji. W planie: cwiczenie: {{kategoria: KATEGORIA, nazwa: NAZWA, powt: 10}}")
    print("Polskie skróty (co: ...): " + ", ".join(sorted(ALIASY)))


def powtorz(n, kroki):
    return {
        "type": "RepeatGroupDTO",
        "stepType": {"stepTypeId": STEP_TYPES["repeat"], "stepTypeKey": "repeat"},
        "numberOfIterations": int(n),
        "endCondition": {"conditionTypeId": CONDITIONS["iterations"],
                         "conditionTypeKey": "iterations"},
        "endConditionValue": float(n),
        "smartRepeat": False,
        "workoutSteps": kroki,
    }


def _numeruj(kroki, licznik=None):
    """Garmin wymaga kolejnych stepOrder (także wewnątrz powtórzeń)."""
    licznik = licznik or [0]
    for k in kroki:
        licznik[0] += 1
        k["stepOrder"] = licznik[0]
        if k["type"] == "RepeatGroupDTO":
            _numeruj(k["workoutSteps"], licznik)
    return kroki


def zbuduj_json(definicja):
    sport = SPORTY[definicja.get("sport") or "bieg"]
    opis = definicja.get("opis", "")
    if definicja.get("sport") == "sila":
        # krótki odcisk kroków: zmiana samych ćwiczeń też wymieni trening w Garminie
        odc = hashlib.sha1(json.dumps(definicja["kroki"], sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()[:6]
        opis = f"{opis[:500]} (v{odc})"
    return {
        "workoutName": definicja["nazwa"],
        "description": opis[:512],
        "sportType": sport,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": sport,
            "workoutSteps": _numeruj(definicja["kroki"]),
        }],
    }


def eksportuj_tokeny():
    """Pakuje zapisane tokeny do jednego tekstu (sekret GARMIN_TOKENS)."""
    if not os.path.exists(TOKENSTORE):
        sys.exit(f"Brak tokenów w {TOKENSTORE} – najpierw zaloguj się zwykłym uruchomieniem.")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(TOKENSTORE, arcname=os.path.basename(TOKENSTORE))
    print(base64.b64encode(buf.getvalue()).decode())


def _tokeny_ze_zmiennej():
    """Na serwerze (GitHub Actions): odtwarza tokeny z GARMIN_TOKENS."""
    dane = os.getenv("GARMIN_TOKENS", "").strip()
    if not dane or os.path.exists(TOKENSTORE):
        return
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(dane)), mode="r:gz") as tar:
        try:
            tar.extractall(os.path.dirname(TOKENSTORE), filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(os.path.dirname(TOKENSTORE))


def zaloguj():
    from garminconnect import Garmin

    _tokeny_ze_zmiennej()
    try:  # zapisane tokeny
        client = Garmin()
        client.login(TOKENSTORE)
        return client
    except Exception as e:
        if not sys.stdin.isatty():
            print(f"Logowanie tokenami nie powiodło się: {e}")

    if not sys.stdin.isatty() and not (os.getenv("GARMIN_EMAIL")
                                       and os.getenv("GARMIN_PASSWORD")):
        sys.exit("Brak ważnych tokenów i brak terminala do logowania. Zaloguj się"
                 " lokalnie i odśwież sekret GARMIN_TOKENS (--eksport-tokenow).")

    email = os.getenv("GARMIN_EMAIL") or input("E-mail Garmin: ")
    haslo = os.getenv("GARMIN_PASSWORD") or getpass("Hasło Garmin: ")
    client = Garmin(email, haslo, prompt_mfa=lambda: input("Kod MFA: "))
    client.login(TOKENSTORE)
    try:  # starsze wersje biblioteki nie zapisują tokenów same
        client.garth.dump(TOKENSTORE)
    except Exception:
        pass
    return client


def _api(client, metoda, sciezka, **kw):
    """Awaryjne surowe wywołanie API – tylko dla starszych wersji biblioteki."""
    if metoda == "GET":
        return client.connectapi(sciezka, **kw)
    garth = getattr(client, "garth", None)
    if garth is not None:
        return garth.connectapi(sciezka, method=metoda, **kw)
    return client.client.request(metoda, "connectapi", sciezka, api=True, **kw)


def wszystkie_treningi(client):
    """Lista treningów z zakładki Moje treningi."""
    wynik, start = [], 0
    while True:
        porcja = client.get_workouts(start, 100) or []
        wynik += porcja
        if len(porcja) < 100:
            return wynik
        start += 100


def istniejace_treningi(client):
    """{nazwa: workoutId} – przy duplikatach najstarszy (najniższe ID)."""
    wynik = {}
    for w in wszystkie_treningi(client):
        nazwa, wid = w.get("workoutName"), w.get("workoutId")
        if nazwa not in wynik or wid < wynik[nazwa]:
            wynik[nazwa] = wid
    return wynik


def wpisy_kalendarza(client, daty):
    """Treningi zaplanowane w miesiącach obejmujących podane daty."""
    miesiace = sorted({(int(d[:4]), int(d[5:7])) for d in daty})
    wpisy, widziane = [], set()
    for rok, mies in miesiace:
        if hasattr(client, "get_scheduled_workouts"):
            dane = client.get_scheduled_workouts(rok, mies) or {}
        else:
            dane = _api(client, "GET", f"/calendar-service/year/{rok}/month/{mies - 1}") or {}
        for it in dane.get("calendarItems", []):
            # widok miesiąca obejmuje też dni sąsiednich miesięcy (pełne tygodnie),
            # więc ten sam wpis może przyjść dwa razy – liczy się raz
            if it.get("itemType") == "workout" and it.get("id") not in widziane:
                widziane.add(it.get("id"))
                wpisy.append({"id": it.get("id"), "data": it.get("date"),
                              "tytul": it.get("title"), "workoutId": it.get("workoutId")})
    return wpisy


def zaplanuj(client, workout_id, dzien):
    """Wstawia trening do kalendarza (to samo co Harmonogram -> Dodaj do kalendarza)."""
    if hasattr(client, "schedule_workout"):
        return client.schedule_workout(workout_id, dzien)
    return _api(client, "POST", f"/workout-service/schedule/{workout_id}", json={"date": dzien})


def usun_z_kalendarza(client, schedule_id):
    if hasattr(client, "unschedule_workout"):
        return client.unschedule_workout(schedule_id)
    return _api(client, "DELETE", f"/workout-service/schedule/{schedule_id}")


def usun_trening(client, workout_id):
    if hasattr(client, "delete_workout"):
        return client.delete_workout(workout_id)
    return _api(client, "DELETE", f"/workout-service/workout/{workout_id}")


def sprzataj(client, zadania):
    """Usuwa duplikaty, ale tylko treningów o nazwach z PLAN-u."""
    nazwy = {pl["workoutName"] for _, pl in zadania}
    daty = [d for d, _ in zadania if d]

    # 1) duplikaty w kalendarzu: ten sam dzień + ta sama nazwa -> zostaje jeden
    kal_do_usuniecia, widziane = [], set()
    for w in sorted(wpisy_kalendarza(client, daty), key=lambda x: x["id"]):
        if w["tytul"] not in nazwy:
            continue
        klucz = (w["data"], w["tytul"])
        if klucz in widziane:
            kal_do_usuniecia.append(w)
        else:
            widziane.add(klucz)

    # 2) duplikaty w Moje treningi: ta sama nazwa -> zostaje najstarszy
    tr_do_usuniecia, zostaja = [], istniejace_treningi(client)
    for w in wszystkie_treningi(client):
        n = w.get("workoutName")
        if n in nazwy and w.get("workoutId") != zostaja[n]:
            tr_do_usuniecia.append(w)

    if not kal_do_usuniecia and not tr_do_usuniecia:
        print("Brak duplikatów.")
        return

    print("Do usunięcia:")
    for w in kal_do_usuniecia:
        print(f"  kalendarz  {w['data']}  {w['tytul']}")
    for w in tr_do_usuniecia:
        print(f"  trening    {w['workoutName']} (ID {w['workoutId']})")
    if input("Usunąć? [t/N] ").strip().lower() != "t":
        print("Anulowano.")
        return

    for w in kal_do_usuniecia:
        usun_z_kalendarza(client, w["id"])
    for w in tr_do_usuniecia:
        usun_trening(client, w["workoutId"])
    print("Usunięto. Uruchom ponownie z --plan, żeby uzupełnić ewentualne braki w kalendarzu.")


# Treningi ze starego planu (20-30.10) – usuwane z "Moje treningi" przy --plan.
STARE_NAZWY = [
    "Próg 4x1600", "Spokojny 9 km", "Interwały 5x1000",
    "Interwały 6x800", "Spokojny 8 km", "Próg 3x2000",
]


def _norm(tekst):
    return " ".join((tekst or "").split())


def opis_treningu(client, w):
    """Opis treningu z Garmina (z listy, a jak go tam nie ma – ze szczegółów)."""
    if w.get("description") is not None:
        return w["description"]
    try:
        if hasattr(client, "get_workout_by_id"):
            return (client.get_workout_by_id(w["workoutId"]) or {}).get("description")
        return (_api(client, "GET", f"/workout-service/workout/{w['workoutId']}")
                or {}).get("description")
    except Exception:
        return None


def przygotuj_wymiane(client, zadania, lista, wpisy):
    """Ustala, co trzeba wymienić, żeby Garmin zgadzał się z PLAN-em.

    Zwraca:
      kal_usun  – wpisy kalendarza w zakresie planu, których nie ma w PLAN-ie
                  (stary plan) albo które wskazują na zmieniony trening,
      wymiana   – {nazwa: [stare workoutId]} treningi z PLAN-u o tej samej
                  nazwie, ale innej treści (porównanie opisu),
      stare     – treningi ze STARE_NAZWY do usunięcia z "Moje treningi".
    """
    nowe_opisy = {pl["workoutName"]: pl["description"] for _, pl in zadania}
    daty = [d for d, _ in zadania if d]
    od, do_ = min(daty), max(daty)
    klucze_planu = {(d, pl["workoutName"]) for d, pl in zadania if d}

    wymiana = {}
    for w in lista:
        n = w.get("workoutName")
        if n not in nowe_opisy:
            continue
        opis = opis_treningu(client, w)
        if opis is None:
            print(f"Uwaga: nie odczytałem opisu treningu {n} – zostawiam bez zmian.")
            continue
        if _norm(opis) != _norm(nowe_opisy[n]):
            wymiana.setdefault(n, []).append(w["workoutId"])

    kal_usun = [w for w in wpisy
                if od <= (w["data"] or "") <= do_
                and ((w["data"], w["tytul"]) not in klucze_planu
                     or w["tytul"] in wymiana)]

    stare = [w for w in lista if w.get("workoutName") in STARE_NAZWY]
    return kal_usun, wymiana, stare


def main():
    p = argparse.ArgumentParser(description="Dodaj trening do Garmin Connect")
    p.add_argument("--data", help="zaplanuj w kalendarzu, format RRRR-MM-DD")
    p.add_argument("--plan", action="store_true",
                   help="dodaj wszystkie treningi z sekcji PLAN (z datami)")
    p.add_argument("--tylko-kalendarz", action="store_true",
                   help="nie wysyłaj nowych treningów, tylko zaplanuj istniejące")
    p.add_argument("--sprzataj", action="store_true",
                   help="usuń zduplikowane treningi i wpisy w kalendarzu (z PLAN-u)")
    p.add_argument("--bez-opcjonalnych", action="store_true",
                   help="z --plan: pomiń dni opcjonalne (wolne lub krótki rozruch)")
    p.add_argument("--tak", action="store_true",
                   help="z --plan: wymień/usuń stare treningi bez pytania")
    p.add_argument("--dry-run", action="store_true", help="pokaż JSON, nie wysyłaj")
    p.add_argument("--zrodlo", default=os.getenv("GARMIN_PLAN_ZRODLO"),
                   help="plik planu (YAML/JSON): ścieżka albo URL na GitHubie; włącza --plan")
    p.add_argument("--zawsze", action="store_true",
                   help="z --zrodlo: wykonaj, nawet jeśli plik się nie zmienił")
    p.add_argument("--cwiczenia", nargs="?", const="", metavar="SZUKAJ",
                   help="pokaż ćwiczenia z katalogu Garmina (np. --cwiczenia plank)")
    p.add_argument("--eksport-tokenow", action="store_true",
                   help="wypisz tokeny Garmina jako tekst do sekretu GARMIN_TOKENS")
    args = p.parse_args()

    if args.eksport_tokenow:
        eksportuj_tokeny()
        return
    if args.cwiczenia is not None:
        wypisz_cwiczenia(args.cwiczenia)
        return

    global STARE_NAZWY
    plan, odcisk = PLAN, None
    if args.zrodlo:
        tekst = pobierz_zrodlo(args.zrodlo)
        if tekst is None:
            print(f"Brak pliku planu ({args.zrodlo}) – nic nie robię.")
            return
        plan, stare = plan_z_tekstu(tekst, args.zrodlo)
        if stare is not None:
            STARE_NAZWY = list(stare)
        args.plan = True
        odcisk = hashlib.sha256(
            (tekst + f"|{args.bez_opcjonalnych}|{args.tylko_kalendarz}").encode()).hexdigest()
        if (not args.zawsze and not args.dry_run and not args.sprzataj
                and os.path.exists(OSTATNI_PLAN)
                and open(OSTATNI_PLAN).read().strip() == odcisk):
            print("Plan bez zmian od ostatniego udanego uruchomienia – nic nie robię"
                  " (wymuszenie: --zawsze).")
            return
        print(f"Plan z pliku: {args.zrodlo} ({len(plan)} pozycji)")

    if args.plan:
        dzis = date.today().isoformat()
        zadania = []
        for w in plan:
            if w["data"] < dzis:
                print(f"Pomijam {w['data']} {w['nazwa']} (data już minęła)")
                continue
            if args.bez_opcjonalnych and w.get("opcjonalny"):
                continue
            payload = zbuduj_json({"nazwa": w["nazwa"], "opis": w["opis"],
                                   "kroki": w["kroki"](), "sport": w.get("sport")})
            zadania.append((w["data"], payload))
    else:
        zadania = [(args.data, zbuduj_json(TRENING()))]

    if args.dry_run:
        for d, payload in zadania:
            print(f"# {d or '(bez daty)'}")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    client = zaloguj()

    if args.sprzataj:
        sprzataj(client, zadania)
        return

    lista = wszystkie_treningi(client)
    juz_sa = {}
    for w in lista:  # {nazwa: workoutId} – przy duplikatach najstarszy
        n, wid = w.get("workoutName"), w.get("workoutId")
        if n not in juz_sa or wid < juz_sa[n]:
            juz_sa[n] = wid
    try:
        wpisy = wpisy_kalendarza(client, [d for d, _ in zadania if d])
    except Exception as e:
        print(f"Nie udało się odczytać kalendarza ({e}) – przerywam, żeby nie zrobić duplikatów.")
        sys.exit(2)
    bledy = 0

    # --- wymiana starych treningów na nowe (tylko --plan) ---
    do_usuniecia_po = []          # (nazwa lub None, workoutId) – kasowane na końcu
    if args.plan and not args.tylko_kalendarz and zadania:
        kal_usun, wymiana, stare = przygotuj_wymiane(client, zadania, lista, wpisy)
        if kal_usun or wymiana or stare:
            print("Zmiany względem tego, co jest w Garmin Connect:")
            for w in sorted(kal_usun, key=lambda x: x["data"]):
                print(f"  usuń z kalendarza   {w['data']}  {w['tytul']}")
            for n in wymiana:
                print(f"  wymień na nowy      {n} (inna treść)")
            for w in stare:
                print(f"  usuń z Moje treningi  {w['workoutName']} (stary plan)")
            if not args.tak:
                if not sys.stdin.isatty():
                    print("Brak terminala do potwierdzenia – uruchom z --tak.")
                    sys.exit(2)
                if input("Wykonać? [t/N] ").strip().lower() != "t":
                    print("Anulowano – nic nie zmieniono.")
                    return
            for w in kal_usun:
                try:
                    usun_z_kalendarza(client, w["id"])
                    wpisy.remove(w)
                    print(f"Usunięto z kalendarza: {w['data']} {w['tytul']}")
                except Exception as e:
                    print(f"   !! nie udało się usunąć {w['data']} {w['tytul']}: {e}")
                    bledy += 1
            for n, ids in wymiana.items():
                juz_sa.pop(n, None)  # w pętli niżej wyśle się nowa wersja
                do_usuniecia_po += [(n, wid) for wid in ids]
            do_usuniecia_po += [(None, w["workoutId"]) for w in stare]

    w_kalendarzu = {(w["data"], w["tytul"]) for w in wpisy}

    for d, payload in zadania:
        nazwa = payload["workoutName"]
        workout_id = juz_sa.get(nazwa)

        if workout_id:
            print(f"Jest już: {nazwa} (ID {workout_id})")
        elif args.tylko_kalendarz:
            print(f"BRAK w Moje treningi: {nazwa} – pomijam")
            bledy += 1
            continue
        else:
            workout_id = client.upload_workout(payload).get("workoutId")
            juz_sa[nazwa] = workout_id
            print(f"Dodano: {nazwa} (ID {workout_id})")

        if d and (d, nazwa) in w_kalendarzu:
            print(f"   = w kalendarzu {d} już jest, pomijam")
        elif d:
            try:
                zaplanuj(client, workout_id, d)
                w_kalendarzu.add((d, nazwa))
                print(f"   -> kalendarz {d}")
            except Exception as e:
                print(f"   !! nie udało się zaplanować na {d}: {e}")
                bledy += 1

    for nazwa, wid in do_usuniecia_po:
        if nazwa and not juz_sa.get(nazwa):
            continue  # nowa wersja się nie wysłała – zostaw starą
        try:
            usun_trening(client, wid)
            print(f"Usunięto z Moje treningi: {nazwa or 'stary trening'} (ID {wid})")
        except Exception as e:
            print(f"   !! nie udało się usunąć treningu ID {wid}: {e}")
            bledy += 1

    if bledy:
        print(f"\nGotowe z problemami: {bledy}")
        sys.exit(2)
    if odcisk:
        try:
            with open(OSTATNI_PLAN, "w") as f:
                f.write(odcisk)
        except OSError:
            pass
    print("\nGotowe. Zsynchronizuj zegarek, żeby treningi pojawiły się w kalendarzu.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
