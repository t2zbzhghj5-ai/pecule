"""Batterie de tests de Pécule (navigateur automatisé).

  python3 tests/run_tests.py                      # avec le fichier iCompta fictif
  python3 tests/run_tests.py --icompta mon.cdb    # en plus : comparaison avec un vrai fichier iCompta
  python3 tests/run_tests.py -k cartes            # seulement les tests dont le nom contient « cartes »

Prérequis : pip install playwright && playwright install chromium
Aucune donnée personnelle dans ce dossier : le fichier iCompta de test est inventé (fixture_icompta.py).
"""
import argparse
import datetime as dt
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import traceback

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
NOW = dt.datetime(2026, 11, 11, 10, 0)
DATES = ["2026-11-11", "2026-11-30", "2026-12-31", "2027-01-31", "2027-02-28"]
MOBILE, DESKTOP = {"width": 390, "height": 844}, {"width": 1440, "height": 900}
BAD_TEXT = re.compile(r"\bNaN\b|undefined|Invalid Date|\[object|\bnull\s?€|Infinity")
# version installée (données sur l'appareil) : rien ne doit parler de Claude ni d'une synchronisation entre appareils
STANDALONE_TEXT = re.compile(r"Claude|tous tes appareils|espace personnel|🏷")

sys.path.insert(0, HERE)
import fixture_icompta  # noqa: E402

# ---------------------------------------------------------------- serveur (avec accès de test aux calculs)
HOOK = ('window.__dbg = {A, S, Store, Hist, NAV, SHEET, occurrences, balance, amtFor, accList, today, budgets, lineAt, cardNext, cardPending, '
        'halfBase, halfParts, halfName, halfFrom, halfJoint, halfNeedsOk, healthChecksAll, dataChecks, lineBudget, lineActual, prOn, prMonth, glanceState, shownBal, cats, catIc, guessIc, baseAmt, envAt, lineAt, lineById, linesOf, setLineAmount, restEndOf, parseBankText, half50, half50Due, half50Msg, halfBase, healthChecks, hKey, kidsOf, cardNext, runBal, addDays, updCheck, UPD, APP_VERSION, prItems, nextPending, fitViewport, bdgGroups, dueAsks, dueStart, dailySeries};\n')


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
            mark = 'render(html`<${App} />`'
            assert mark in html, "point d'accroche introuvable"
            body = html.replace(mark, HOOK + mark, 1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()


def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


# ---------------------------------------------------------------- outils
class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def near(a, b, eps=0.005):
    return a is not None and b is not None and abs(a - b) < eps


class App:
    """Une page Pécule neuve (données vides), à l'heure de NOW."""

    def __init__(self, browser, url, view=MOBILE, dark=False, now=None):
        self.ctx = browser.new_context(viewport=view, color_scheme="dark" if dark else "light", service_workers="block", accept_downloads=True)
        self.pg = self.ctx.new_page()
        self.errors = []
        self.pg.on("pageerror", lambda e: self.errors.append("Erreur JS : %s" % e))
        self.pg.on("console", lambda m: m.type == "error" and not self.external(m) and self.errors.append("Console : %s" % m.text))
        self.base = url
        self.pg.clock.install(time=now or NOW)
        self.pg.goto(url)
        self.pg.wait_for_timeout(1200)

    def external(self, m):
        """Ressources extérieures (polices web) : sans objet dans les tests hors ligne."""
        u = (m.location or {}).get("url", "")
        return "Failed to load resource" in m.text and not u.startswith(self.base)

    def js(self, body, arg=None):
        return self.pg.evaluate("(arg) => { const D = window.__dbg; %s }" % body, arg)

    def wait(self, ms=400):
        self.pg.wait_for_timeout(ms)

    def import_ic(self, path, pending=True, joint=None):
        self.pg.click("text=Importer depuis iCompta")
        self.pg.set_input_files(".wiz input[type=file] >> nth=0", path)
        self.wait(2500)
        if not pending:
            self.pg.locator(".wiz").get_by_text("Seulement les opérations créées et prévues").click()
            self.wait(2500)
        # question « Un de ces comptes est-il un compte joint ? » : le Joint du fichier fictif, aucun sinon
        if joint is None:
            joint = ["Joint"] if path == FICTIF else []
        for n in joint:
            self.pg.locator('.wiz [role=group][aria-labelledby=ic-joint]').get_by_role("button", name=n, exact=True).click()
            self.wait(200)
        self.pg.click('.wgo:has-text("Importer")')
        self.wait(4500)
        self.dismiss()

    def dismiss(self):
        if self.pg.locator(".undobar button[aria-label]").count():
            self.pg.click(".undobar button[aria-label]")
        if self.pg.locator("text=Plus tard").count():
            self.pg.click("text=Plus tard")
        self.wait(200)

    def acc_id(self, name):
        return self.js("return Object.values(D.A().acc).find(a => a.name === arg)?.id || null;", name)

    def pick(self, name):
        if not self.pg.locator(".accpill").count():
            self.pg.evaluate("location.hash = '#comptes'")
            self.wait(400)
        self.pg.click(".accpill")
        self.pg.click('.sheet .row:has-text("%s")' % name)
        self.wait(500)

    def body(self):
        return self.pg.inner_text("body")

    def no_errors(self, where=""):
        errs = [e for e in self.errors if "favicon" not in e]
        self.errors.clear()
        check(not errs, "%s %s" % (where, " | ".join(errs[:3])))
        body = self.body()
        bad = BAD_TEXT.search(body) or STANDALONE_TEXT.search(body)
        check(not bad, "%s texte suspect à l'écran : « %s »" % (where, body[max(0, bad.start() - 60):bad.end() + 40].replace("\n", " ") if bad else ""))

    def projected(self, dates):
        """Solde projeté de chaque compte à chaque date (opérations + prévisions de Pécule)."""
        return self.js("""const out = {};
          for (const a of D.accList(true)) out[a.name] = arg.map(d => { const occ = D.occurrences(d);
            return Math.round((D.balance(a.id, {upTo: d}) + occ.filter(o => o.date <= d).reduce((s, o) => s + D.amtFor(o, a.id), 0)) * 100) / 100; });
          return out;""", dates)

    def close(self):
        self.ctx.close()


# ---------------------------------------------------------------- tests
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def fixture_truth():
    f, names = fixture_icompta.build(FICTIF)
    return fixture_icompta.truth(f, names, DATES)


@test
def import_fictif_soldes(br, url):
    """Import (opérations en cours) : soldes projetés de chaque compte = iCompta, à 5 dates."""
    want = fixture_truth()
    a = App(br, url)
    a.import_ic(FICTIF)
    got = a.projected(DATES)
    for acc, v in want.items():
        check(got.get(acc) is not None, "compte %s absent" % acc)
        for d, x, y in zip(DATES, got[acc], v):
            check(near(x, y), "%s au %s : Pécule %s, iCompta %s" % (acc, d, x, y))
    a.no_errors("après import")
    a.close()


@test
def import_fictif_historique(br, url):
    """Import complet (avec l'historique) : mêmes soldes projetés."""
    want = fixture_truth()
    a = App(br, url)
    a.import_ic(FICTIF, pending=False)
    got = a.projected(DATES)
    for acc, v in want.items():
        for d, x, y in zip(DATES, got[acc], v):
            check(near(x, y), "%s au %s : Pécule %s, iCompta %s" % (acc, d, x, y))
    a.close()


@test
def import_fictif_details(br, url):
    """Échéances (annuelle, limitée), prévue passée, annulée, ponctuelle, catégories mixtes."""
    a = App(br, url)
    a.import_ic(FICTIF)
    sch = a.js("return Object.values(D.A().sch).map(s => ({name: s.name, next: s.next, left: s.left, freq: s.freq}));")
    by = {s["name"]: s for s in sch}
    check("Assurance habitation" in by and by["Assurance habitation"]["next"] == "2027-03-10", "assurance annuelle : %s" % by.get("Assurance habitation"))
    check(by.get("Électroménager", {}).get("next") == "2026-12-20" and by["Électroménager"].get("left") == 2, "échéance limitée : %s" % by.get("Électroménager"))
    check(by.get("Cantine", {}).get("next") == "2026-12-03", "cantine : %s" % by.get("Cantine"))
    tx = a.js("return Object.values(D.A().tx).map(t => ({name: t.name, date: t.date, st: t.st, amount: t.amount}));")
    check(any(t["name"] == "Cantine" and t["date"] == "2026-11-03" and t["st"] == "saisie" for t in tx), "cantine du 3 nov. (prévue passée) non reprise à valider")
    check(not any(t["name"] == "Erreur annulée" for t in tx), "opération annulée importée")
    check(any(t["name"] == "Impôts" and t["date"] == "2027-01-15" and t["st"] == "prevue" for t in tx), "opération ponctuelle prévue absente ou pas « prévue »")
    check(not any(re.search(r"\(.*\d\d/\d\d/\d{4} - ", t["name"]) for t in tx), "ligne de projection de budget importée")
    ty = a.js("return D.cats().map(c => c.type);")
    check("both" in ty, "catégories mixtes absentes")
    a.close()


def check_budgets(a, path, until="2027-02-28"):
    """Reste de chaque ligne de budget = ligne de projection créée par iCompta (« Courses (Courses, 01/11/2026 - 30/11/2026) »)."""
    db = sqlite3.connect(path)
    proj = {}
    for name, date, amt in db.execute("select t.name, t.date, s.amount from ICTransaction t join ICTransactionSplit s on s.\"transaction\"=t.ID where t.status='ICTransactionStatus.PlannedStatus' and t.name like '% - %/%/%)' and t.date<=?", (until,)):
        m = re.match(r"(.*?) \((.*), (\d\d)/(\d\d)/(\d{4}) - (\d\d)/(\d\d)/(\d{4})\)$", name.strip())
        if m:
            k = (m.group(2).strip(), date)
            proj[k] = round(proj.get(k, 0) - float(amt), 2)
    check(proj, "aucune ligne de projection de budget dans le fichier")
    mids = sorted({d[:8] + "15" for _, d in proj})
    got = a.js("""const out = {}; for (const l of Object.values(D.A().bud)) { if (l.income) continue;
        for (const d of arg) { const x = D.lineAt(l, d); if (x) { const k = (D.A().cat[l.cat]?.name || '') + '|' + x.pe; out[k] = Math.round(((out[k] || 0) + Math.max(0, x.rest)) * 100) / 100; } } } return out;""", mids)
    for (cat, date), v in proj.items():
        x = got.get("%s|%s" % (cat, date))
        check(near(x, v), "budget %s, période finissant le %s : reste Pécule %s, iCompta %s" % (cat, date, x, v))


@test
def budgets_reste(br, url):
    """Reste de chaque budget (nov. à févr.) = projection d'iCompta."""
    a = App(br, url)
    a.import_ic(FICTIF)
    check_budgets(a, FICTIF)
    a.close()


def check_cards(a, n=None):
    cards = a.js("""return D.accList(true).filter(c => c.type === 'carte').map(c => { const n = D.cardNext(c), p = D.cardPending(c.id);
        return {name: c.name, nx: n && n.amt, cut: n && n.cut, val: D.balance(c.id, {cleared: true}), pend: p.amt, enc: D.balance(c.id), atCut: n ? D.balance(c.id, {upTo: n.cut}) : null}; });""")
    check(n == 0 or cards and (n is None or len(cards) == n), "cartes : %s" % cards)
    occ_cut = {c["name"]: a.js("""const c = D.accList(true).find(x => x.name === arg), n = D.cardNext(c);
        return Math.round((D.balance(c.id, {upTo: n.cut}) + D.occurrences(n.cut).filter(o => o.date <= n.cut).reduce((s, o) => s + D.amtFor(o, c.id), 0)) * 100) / 100;""", c["name"]) for c in cards}
    for c in cards:
        check(near(c["nx"], -occ_cut[c["name"]]), "%s : prochain débit %s ≠ encours à la clôture %s" % (c["name"], c["nx"], -occ_cut[c["name"]]))
        check(near(-c["val"] + c["pend"] + (c["nx"] + c["enc"]), c["nx"]), "%s : validé %s + à valider %s + prévus ≠ %s" % (c["name"], -c["val"], c["pend"], c["nx"]))
        a.pick(c["name"])
        t = a.pg.inner_text("main section")
        check(("%.2f" % c["nx"]).replace(".", ",") in t.replace(" ", "").replace(" ", ""), "%s : montant du prochain débit absent de l'accueil" % c["name"])
    a.pg.click(".accpill")
    t = a.pg.inner_text(".sheet")
    for c in cards:
        check(("%.2f" % c["nx"]).replace(".", ",") in t.replace(" ", "").replace(" ", ""), "%s : montant absent du choix du compte" % c["name"])
    a.pg.keyboard.press("Escape")
    a.js("try { D.SHEET.close(); } catch (e) {}")
    a.no_errors("cartes")


@test
def cartes_coherence(br, url):
    """Cartes : validé + à valider + prévus = prochain débit ; même montant sur l'accueil et dans le choix du compte."""
    a = App(br, url)
    a.import_ic(FICTIF)
    check_cards(a, 3)
    a.close()


def all_screens(a):
    """Ouvre tous les écrans et toutes les fenêtres ; vérifie l'absence d'erreur et de texte suspect."""
    ids = a.js("""const one = k => Object.values(D.A()[k])[0]?.id || null;
      const tx = Object.values(D.A().tx).find(t => !t.kind && (t.splits || []).length);
      const b = D.budgets(true)[0], l = Object.values(D.A().bud)[0], s = Object.values(D.A().sch)[0];
      return {acc: D.accList()[0].id, card: D.accList(true).find(c => c.type === 'carte')?.id, tx: tx?.id, bdg: b?.id, line: l?.id, sch: s?.id, cat: one('cat')};""")
    pg = a.pg
    for tab in ["comptes", "budgets", "prev", "mes", "echeancier", "analyses"]:
        pg.evaluate("location.hash = '#%s'" % tab)
        a.wait(600)
        a.no_errors("onglet %s" % tab)
    views = [{"v": "settings"}, {"v": "cats"}, {"v": "rules"}, {"v": "people"}, {"v": "data"}, {"v": "prefs"}, {"v": "hidden"}, {"v": "accList"},
             {"v": "health"}, {"v": "hist"}, {"v": "guide"}, {"v": "joint"}, {"v": "close"}, {"v": "acc", "id": ids["acc"]}, {"v": "acc", "id": ids["card"]},
             {"v": "budget", "id": ids["bdg"]}, {"v": "line", "id": ids["line"]}, {"v": "allTx"}, {"v": "stmts", "id": ids["acc"]},
             {"v": "catTx", "cat": ids["cat"], "k": "2026-11", "months": ["2026-10", "2026-11"]}]
    for v in views:
        a.js("D.NAV.push(arg);", v)
        a.wait(500)
        a.no_errors("écran %s" % v["v"])
        a.js("D.NAV.pop();")
        a.wait(250)
    sheets = [{"type": "quick"}, {"type": "op", "kind": "tx"}, {"type": "op", "kind": "sch"}, {"type": "op", "kind": "tx", "src": "TX"}, {"type": "op", "kind": "sch", "src": "SCH"},
              {"type": "accPick"}, {"type": "half"}, {"type": "gloss"}, {"type": "icImport"}, {"type": "import"}, {"type": "acc"}, {"type": "accActions", "id": ids["acc"]},
              {"type": "reconcile", "id": ids["acc"]}, {"type": "bdg", "id": ids["bdg"]}, {"type": "bdgActions", "id": ids["bdg"]}, {"type": "line", "budget": ids["bdg"]},
              {"type": "line", "id": ids["line"]}, {"type": "lineDates", "budget": ids["bdg"]}, {"type": "amt", "line": ids["line"]}, {"type": "bdgLines", "budget": ids["bdg"]}, {"type": "fillAvg", "budget": ids["bdg"]}, {"type": "cash"}, {"type": "reset", "id": ids["acc"]},
              {"type": "sim"}, {"type": "jointSetup"}, {"type": "schedImport"}, {"type": "cat"}, {"type": "cat", "id": ids["cat"]}, {"type": "rule"}, {"type": "per"},
              {"type": "schDetail", "id": ids["sch"], "aid": ids["acc"]}, {"type": "export", "ids": [ids["acc"]]}, {"type": "catpick"}, {"type": "archive", "id": ids["acc"]}]
    for s in sheets:
        a.js("""const s = {...arg}; if (s.src === 'TX') s.src = D.A().tx[%s]; if (s.src === 'SCH') s.src = D.A().sch[%s]; D.SHEET.open(s);""" % (json.dumps(ids["tx"]), json.dumps(ids["sch"])), s)
        a.wait(450)
        a.no_errors("fenêtre %s" % s["type"])
        a.pg.keyboard.press("Escape")
        a.js("try { D.SHEET.close && D.SHEET.close(); } catch (e) {}")
        a.wait(250)


@test
def ecrans_iphone(br, url):
    """Tous les écrans et fenêtres sur iPhone (clair) : aucune erreur, aucun NaN, pas de débordement horizontal."""
    a = App(br, url)
    a.import_ic(FICTIF)
    all_screens(a)
    for tab in ["comptes", "budgets", "prev", "mes", "echeancier", "analyses"]:
        a.pg.evaluate("location.hash = '#%s'" % tab)
        a.wait(400)
        w = a.pg.evaluate("(() => { const e = document.getElementById('app'); return Math.max(document.documentElement.scrollWidth - innerWidth, e.scrollWidth - e.clientWidth); })()")
        check(w <= 1, "onglet %s : déborde de %d px en largeur" % (tab, w))
    a.close()


@test
def ecrans_mac_sombre(br, url):
    """Tous les écrans et fenêtres sur grand écran, thème sombre."""
    a = App(br, url, view=DESKTOP, dark=True)
    a.import_ic(FICTIF)
    check(a.pg.locator(".side-brand").is_visible(), "menu latéral absent sur grand écran")
    all_screens(a)
    a.close()


def wizard(a, typ, amount, cat=None, acc=None, to=None):
    a.pg.click('[aria-label="Saisir"]')
    a.wait(300)
    a.pg.click(".wiz >> text=%s" % typ)
    a.wait(300)
    if acc:
        a.pg.select_option('.wiz select[aria-label="Compte"]', label=acc)
    for k in amount:
        a.pg.click('.wiz button:text-is("%s")' % k)
    a.pg.click(".wgo")
    a.wait(300)
    if to:
        a.pg.locator('.wiz button:has-text("%s")' % to).first.click()
        a.wait(300)
    elif cat:
        names = a.pg.locator(".wiz .wbig").all_inner_texts()
        check(any(cat in n for n in names), "catégorie %s absente de l'étape « Pour quoi ? » (%d proposées)" % (cat, len(names)))
        a.pg.locator('.wiz .wbig:has-text("%s")' % cat).first.click()
        a.wait(300)
    for _ in range(3):
        if a.pg.locator('.wgo:has-text("Enregistrer")').count():
            break
        a.pg.click(".wgo")
        a.wait(300)
    a.pg.click('.wgo:has-text("Enregistrer")')
    a.wait(700)


@test
def saisie_assistant(br, url):
    """Bouton + : dépense, revenu, virement ; catégorie, compte, soldes."""
    a = App(br, url)
    a.import_ic(FICTIF)
    j = a.acc_id("Joint")
    b0 = a.js("return D.balance(arg);", j)
    wizard(a, "Dépense", "23", cat="Courses", acc="Joint")
    t = a.js("return Object.values(D.A().tx).find(t => t.amount === -23);")
    check(t and t["account"] == j, "dépense non enregistrée sur le Joint : %s" % t)
    cname = a.js("return D.A().cat[arg]?.name;", (t["splits"] or [{}])[0].get("cat"))
    check(cname == "Courses", "catégorie enregistrée : %s" % cname)
    check(near(a.js("return D.balance(arg);", j), b0 - 23), "solde du Joint non mis à jour")
    wizard(a, "Revenu", "100", cat="Salaire", acc="Courant")
    check(a.js("return !!Object.values(D.A().tx).find(t => t.amount === 100 && t.splits?.[0]?.cat);"), "revenu non enregistré avec sa catégorie")
    c, l = a.acc_id("Courant"), a.acc_id("Livret")
    bc, bl = a.js("return D.balance(arg);", c), a.js("return D.balance(arg);", l)
    wizard(a, "Virement", "50", acc="Courant", to="Livret")
    check(near(a.js("return D.balance(arg);", c), bc - 50) and near(a.js("return D.balance(arg);", l), bl + 50), "virement Courant → Livret mal enregistré")
    a.no_errors("saisie")
    a.close()


@test
def saisie_autre_date(br, url):
    """Bouton + › Dépense › « Autre date » dans un autre mois : la date est gardée, même si l'iPhone ne signale pas le changement (champ modifié sans événement)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    pg.click('[aria-label="Saisir"]'); a.wait(300)
    pg.click(".wiz >> text=Dépense"); a.wait(300)
    for k in "37": pg.click('.wiz button:text-is("%s")' % k)
    pg.click(".wgo"); a.wait(300)
    pg.locator('.wiz .wbig:has-text("Courses")').first.click(); a.wait(300)
    check("Quand" in pg.locator(".wiz").inner_text(), "étape Quand absente")
    # comme sur l'iPhone : la valeur change sans événement « input »
    a.js("document.querySelector('[aria-label=\"Autre date\"]').value = '2026-10-05';")
    pg.click(".wgo"); a.wait(300)
    check("5 oct" in pg.locator(".wiz").inner_text(), "récapitulatif sans la date choisie : %r" % pg.locator(".wiz").inner_text()[-200:])
    pg.click('.wgo:has-text("Enregistrer")'); a.wait(700)
    t = a.js("return Object.values(D.A().tx).find(t => t.amount === -37);")
    check(t and t["date"] == "2026-10-05", "date enregistrée : %s" % (t and t["date"]))
    # échéance : 1re date choisie dans un autre mois, sans événement
    pg.click('[aria-label="Saisir"]'); a.wait(300)
    pg.click(".wiz >> text=Échéance"); a.wait(300)
    pg.click(".wiz >> text=Une dépense"); a.wait(300)
    for k in "9": pg.click('.wiz button:text-is("%s")' % k)
    pg.click(".wgo"); a.wait(300)
    pg.locator('.wiz .wbig:has-text("Divers")').first.click(); a.wait(300)
    a.js("document.querySelector('[aria-label=\"Date de la première échéance\"]').value = '2027-02-14';")
    pg.click(".wgo"); a.wait(300)
    for _ in range(3):
        if pg.locator('.wgo:has-text("Enregistrer")').count(): break
        pg.click(".wgo"); a.wait(300)
    pg.click('.wgo:has-text("Enregistrer")'); a.wait(700)
    sc = a.js("return Object.values(D.A().sch).find(s => s.amount === -9);")
    check(sc and sc["next"] == "2027-02-14" and sc.get("anchor") == 14, "1re échéance : %s" % (sc and sc["next"]))
    a.no_errors("autre date")
    a.close()


@test
def pas_de_zoom_saisie(br, url):
    """iPhone (écran tactile) : tous les champs du bouton + (texte collé, compte, date, libellé) et du formulaire complet sont écrits en 16 px au moins, sinon Safari zoome en les touchant et la page reste agrandie."""
    orig = br.new_context
    br.new_context = lambda **kw: orig(**{**kw, "is_mobile": True, "has_touch": True})
    try:
        a = App(br, url)
    finally:
        br.new_context = orig
    a.import_ic(FICTIF)
    pg = a.pg
    small = lambda sel: a.js("return [...document.querySelectorAll(arg)].filter(e => e.offsetParent && parseFloat(getComputedStyle(e).fontSize) < 16).map(e => (e.getAttribute('aria-label') || e.tagName) + ' ' + getComputedStyle(e).fontSize);", sel)
    pg.click('[aria-label="Saisir"]'); a.wait(400)
    check(pg.locator('.wiz textarea').count() == 1, "champ de saisie par texte absent")
    check(not small(".wiz input, .wiz textarea, .wiz select"), "champs trop petits à l'ouverture du + : %s" % small(".wiz input, .wiz textarea, .wiz select"))
    pg.click(".wiz >> text=Dépense"); a.wait(300)
    for k in "12": pg.click('.wiz button:text-is("%s")' % k)
    pg.click(".wgo"); a.wait(300)
    pg.locator(".wiz .wbig").first.click(); a.wait(300)
    check(not small(".wiz input, .wiz textarea, .wiz select"), "champs trop petits à l'étape Quand : %s" % small(".wiz input, .wiz textarea, .wiz select"))
    pg.click(".wgo"); a.wait(300)
    check(not small(".wiz input, .wiz textarea, .wiz select"), "champs trop petits au récapitulatif : %s" % small(".wiz input, .wiz textarea, .wiz select"))
    a.js("D.SHEET.close()"); a.wait(300)
    a.js("D.SHEET.open({type: 'op', kind: 'tx'})"); a.wait(500)
    check(not small(".sheet input, .sheet textarea, .sheet select"), "champs trop petits dans le formulaire : %s" % small(".sheet input, .sheet textarea, .sheet select"))
    big = a.js("return parseFloat(getComputedStyle(document.querySelector('#op-amt')).fontSize);")
    check(big >= 40, "montant de la fiche réduit à %s px sur iPhone" % big)
    a.no_errors("zoom")
    a.close()


@test
def fiche_saisie(br, url):
    """Fiche de saisie (maquette A) : montant dans un bloc de couleur, quatre lignes (libellé, catégorie, compte, date), options en interrupteurs ; plus de Tiers, Projet, Surlignage, Moyen de paiement ni Dépense partagée ; « Déjà passée à la banque » valide ; date de débit seulement pour une carte."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    a.js("D.SHEET.open({type: 'op', kind: 'tx'})"); a.wait(500)
    sh = pg.locator(".sheet")
    check(sh.locator(".famt").count() == 1 and "Montant" in sh.locator(".famt").inner_text(), "bloc du montant absent")
    rows = sh.locator(".fcard").first.inner_text()
    check(all(x in rows for x in ("Libellé", "Catégorie", "Compte", "Date")), "lignes de la fiche : %r" % rows)
    sh.locator("button", has_text="Plus d'options").first.click(); a.wait(300)
    txt = sh.inner_text()
    gone = [x for x in ("Tiers", "Projet", "Surlignage", "Moyen de paiement", "Dépense partagée", "Remboursement de") if x in txt]
    check(not gone, "champs retirés encore visibles : %s" % gone)
    check("Statut" in txt and "Note" in txt, "Statut ou Note absents de « Plus d'options »")
    check("Date de débit" not in txt, "date de débit proposée hors carte")
    sh.locator("#op-amt").fill("12")
    sh.locator("#op-name").fill("Fiche test")
    sh.locator("#op-cat").click(); a.wait(300)
    sh.locator(".frow-grid .catbtn", has_text="Courses").first.click(); a.wait(200)
    check("Courses" in sh.locator("#op-cat").inner_text(), "catégorie non choisie")
    sh.locator('[role=switch]:has-text("Déjà passée à la banque")').click(); a.wait(200)
    sh.locator(".btn.pri").last.click(); a.wait(600)
    t = a.js("return Object.values(D.A().tx).find(t => t.name === 'Fiche test');")
    check(t and t["st"] == "pointee" and t["amount"] == -12 and a.js("return D.A().cat[arg]?.name;", t["splits"][0]["cat"]) == "Courses" and t["pay"] == "cb", "opération enregistrée : %s" % t)
    # virement : ligne « Vers » ; carte : date de débit dans « Plus d'options »
    a.js("D.SHEET.open({type: 'op', kind: 'tx'})"); a.wait(500)
    sh = pg.locator(".sheet").last
    sh.locator(".famt .seg button", has_text="Virement").click(); a.wait(200)
    check(sh.locator('label[for="op-to"]').count() == 1, "ligne « Vers » absente pour un virement")
    sh.locator(".famt .seg button", has_text="Dépense").click(); a.wait(200)
    sh.locator("#op-acc").select_option(label="Carte Perso"); a.wait(200)
    sh.locator("button", has_text="Plus d'options").first.click(); a.wait(300)
    check("Date de débit" in sh.inner_text(), "date de débit absente pour une carte")
    a.no_errors("fiche")
    a.close()


@test
def saisie_texte_et_suggestions(br, url):
    """Saisie par texte collé et libellés proposés dès la 1re lettre."""
    a = App(br, url)
    a.import_ic(FICTIF)
    a.pg.click('[aria-label="Saisir"]')
    a.wait(300)
    a.pg.fill(".wiz textarea", "Boulangerie 4,20 € hier")
    a.pg.click('.wiz button:has-text("Analyser")')
    a.wait(500)
    for _ in range(4):
        if a.pg.locator('.wgo:has-text("Enregistrer")').count():
            break
        if a.pg.locator(".wiz").get_by_text("Sans catégorie").count():
            a.pg.locator(".wiz").get_by_text("Sans catégorie").first.click()
        else:
            a.pg.click(".wgo")
        a.wait(350)
    t = a.pg.inner_text(".wiz") + " " + " ".join(a.pg.eval_on_selector_all(".wiz input", "l => l.map(i => i.value)"))
    check("4,20" in t and "Boulangerie" in t and "10 nov" in t, "texte « Boulangerie 4,20 € hier » mal lu : %s" % t.replace("\n", " | ")[:300])
    # montants à une décimale ou sans virgule, libellés gardés en entier
    cases = [("Courses Carrefour 12,5 €", "Courses Carrefour", -12.5), ("Boulangerie 3,5", "Boulangerie", -3.5), ("Ciné 12", "Ciné", -12),
             ("Pain au chocolat 2,40", "Pain au chocolat", -2.4), ("Cadeau de Noël pour Louis 35,90", "Cadeau de Noël pour Louis", -35.9),
             ("Pharmacie de la gare 8,9", "Pharmacie de la gare", -8.9), ("Apéro chez Paul 1 234,5", "Apéro chez Paul", -1234.5),
             ("Remboursement mutuelle 18,7", "Remboursement mutuelle", 18.7), ("Paiement CB 4532 de 18,30 € le 10/11 chez FNAC", "Fnac", -18.3),
             ("Le poinçonneur 12,50 €", "Le poinçonneur", -12.5), ("Le poinçonneur, 12,5 & 3", "Le poinçonneur", -12.5), ("Chez Paul 20 € CB", "Chez Paul", -20)]
    for txt, nm, v in cases:
        r = a.js("return D.parseBankText(arg).map(x => [x.name, x.amount]);", txt)
        check(len(r) == 1 and r[0][0] == nm and near(r[0][1], v), "« %s » lu %s (attendu %s, %s)" % (txt, r, nm, v))
    a.pg.keyboard.press("Escape")
    a.js("try { D.SHEET.close && D.SHEET.close(); } catch (e) {}")
    a.wait(300)
    a.js("D.SHEET.open({type: 'op', kind: 'tx'});")
    a.wait(400)
    a.pg.fill("#op-name", "M")
    a.wait(300)
    check("Marché" in a.pg.inner_text(".sheet"), "« Marché » non proposé après « M »")
    a.pg.fill("#op-name", "lo")
    a.wait(300)
    check("Loyer" in a.pg.inner_text(".sheet"), "« Loyer » (échéancier) non proposé après « lo »")
    a.no_errors("texte")
    a.close()


@test
def cinquante_cinquante(br, url):
    """50/50 : la moitié s'ajoute au virement et à l'opération de Claire du mois ; décocher retire ; décembre en prévu."""
    a = App(br, url)
    a.import_ic(FICTIF)
    check(a.js("return D.halfName();") == "Claire", "prénom deviné : %s" % a.js("return D.halfName();"))

    def pair():
        return a.js("""const {T, M} = D.halfBase('2026-11'); return [T && T.amount, M && M.amount];""")
    check(pair() == [-30, 30], "paire de base : %s" % pair())
    j = a.acc_id("Joint")
    a.js("""const t = {k: 'tx', id: 'tx_test_half', account: arg, date: '2026-11-12', name: 'Cinéma test', amount: -40, splits: [{amount: -40, cat: null}], st: 'saisie', half: true}; D.Store.put(t);""", j)
    a.wait(400)
    check(pair() == [-50, 50], "après une dépense 50/50 de 40 € : %s (attendu −50 / 50)" % pair())
    a.js("D.Store.put({...D.A().tx.tx_test_half, half: false});")
    a.wait(400)
    check(pair() == [-30, 30], "après décochage : %s" % pair())
    a.js("""D.Store.put({k: 'tx', id: 'tx_test_dec', account: arg, date: '2026-12-05', name: 'Cadeau', amount: -50, splits: [{amount: -50, cat: null}], st: 'saisie', half: true});""", j)
    a.wait(400)
    dec = a.js("return D.occurrences('2027-01-31').filter(o => o.kind === 'half').map(o => [o.name, o.amount, o.date]);")
    check(sorted(dec) == sorted([["Virement au Joint (50/50)", -25, "2026-12-30"], ["Claire", 25, "2026-12-30"]]), "prévues de décembre : %s" % dec)
    a.no_errors("50/50")
    a.close()


@test
def cinquante_cinquante_depuis_le_courant(br, url):
    """50/50 : un livret qui vire parfois vers le joint en fin de mois n'est jamais pris pour le compte d'où part ta moitié."""
    a = App(br, url)
    a.import_ic(FICTIF)
    j, liv, cour = a.acc_id("Joint"), a.acc_id("Livret"), a.acc_id("Courant")
    for d in ["2026-08-30", "2026-09-29", "2026-10-30"]:
        a.js("""const [j, l, d] = arg; D.Store.put({k: 'tx', id: 'tx_liv_' + d, account: l, date: d, name: 'Virement au Joint', amount: -100, splits: [{to: j, amount: -100}], st: 'pointee', st2: 'pointee'});""", [j, liv, d])
    a.wait(500)
    check(a.js("return D.halfFrom();") == cour, "ta moitié partirait du compte %s" % a.js("return D.A().acc[D.halfFrom()]?.name;"))
    a.js("""D.Store.put({k: 'tx', id: 'tx_test_half2', account: arg, date: '2026-11-12', name: 'Cinéma test', amount: -40, splits: [{amount: -40, cat: null}], st: 'saisie', half: true});""", j)
    a.wait(500)
    pair = a.js("""const {T, M} = D.halfBase('2026-11'); return [T && T.amount, M && M.amount, T && D.A().acc[T.account].name];""")
    check(pair == [-50, 50, "Courant"], "la moitié ne s'ajoute pas au virement du Courant : %s" % pair)
    on_liv = a.js("return D.occurrences('2026-12-31').filter(o => o.kind === 'half' && o.account === arg).map(o => [o.name, o.amount]);", liv)
    check(not on_liv, "ligne 50/50 sur le livret : %s" % on_liv)
    a.no_errors("50/50 depuis le courant")
    a.close()


@test
def cinquante_cinquante_sans_compte_joint(br, url):
    """Compte joint indiqué explicitement, jamais deviné par son nom : sans compte joint (cas de Marie), rien du 50/50 ; on l'active dans la fiche du compte."""
    a = App(br, url)
    a.import_ic(FICTIF, joint=[])
    j = a.acc_id("Joint")
    check(a.js("return D.halfJoint();") is None, "un compte est pris pour le joint d'après son nom")
    a.js("D.SHEET.open({type: 'op', kind: 'tx', acc: arg});", j)
    a.wait(500)
    check("Remboursement 50/50" not in a.pg.inner_text(".sheet"), "option 50/50 proposée sans compte joint")
    a.js("D.SHEET.close();")
    a.wait(300)
    a.pg.evaluate("location.hash = '#accueil'")
    a.wait(300)
    check("50/50" not in a.pg.inner_text("main"), "question 50/50 sur l'accueil sans compte joint")
    check(not [k for k in a.js("return D.dataChecks().map(c => c.key);") if k.startswith("half")], "vérification 50/50 sans compte joint")
    # fiche du compte : interrupteur « Compte joint »
    a.js("D.SHEET.open({type: 'acc', id: arg});", j)
    a.wait(500)
    a.pg.locator('.sheet [role=switch]:has-text("Compte joint")').click()
    a.wait(200)
    a.pg.locator('.sheet button.link:has-text("Enregistrer"), .sheet button:has-text("Enregistrer")').first.click()
    a.wait(500)
    check(a.js("return D.halfJoint();") == j, "le compte marqué joint n'est pas reconnu")
    a.js("D.SHEET.open({type: 'op', kind: 'tx', acc: arg});", j)
    a.wait(500)
    check("Remboursement 50/50" in a.pg.inner_text(".sheet"), "option 50/50 absente après avoir marqué le compte joint")
    a.js("D.SHEET.close();")
    a.wait(300)
    a.no_errors("sans compte joint")
    a.close()


@test
def compte_joint_import_et_mise_a_jour(br, url):
    """Import iCompta : la réponse à « compte joint ? » est enregistrée ; mise à jour : le compte où des dépenses 50/50 sont déjà cochées est marqué joint."""
    a = App(br, url)
    a.import_ic(FICTIF)
    j = a.acc_id("Joint")
    check(a.js("return Object.values(D.A().acc).filter(x => x.joint).map(x => x.name);") == ["Joint"], "réponse à l'import mal enregistrée : %s" % a.js("return Object.values(D.A().acc).filter(x => x.joint).map(x => x.name);"))
    # données d'avant la mise à jour : aucun compte marqué, une dépense 50/50 sur la carte du joint
    cj = a.acc_id("Carte Joint")
    a.js("""const [j, cj] = arg; D.Store.bulk([['put', {...D.A().acc[j], joint: false}], ['put', {...D.S(), jointMig: false}],
      ['put', {k: 'tx', id: 'tx_mig_half', account: cj, date: '2026-11-12', name: 'Restaurant', amount: -60, splits: [{amount: -60, cat: null}], st: 'saisie', half: true}]]);""", [j, cj])
    a.wait(800)
    check(a.js("return [D.A().acc[arg].joint, D.S().jointMig, D.halfJoint()];", j) == [True, True, j], "migration : %s" % a.js("return [D.A().acc[arg].joint, D.S().jointMig, D.halfJoint()];", j))
    a.no_errors("compte joint")
    a.close()


@test
def ventilation_avec_virement(br, url):
    """Ventilation : « + Ajouter un virement » ajoute une part vers un autre compte (dépense et revenu, signe ±), soldes des deux comptes justes, compte conservé à la réouverture."""
    a = App(br, url)
    a.import_ic(FICTIF)
    cour, liv, j = a.acc_id("Courant"), a.acc_id("Livret"), a.acc_id("Joint")
    b0 = a.js("return [D.balance(arg[0]), D.balance(arg[1])];", [cour, liv])
    def fill(acc, mode, total, name, parts):
        a.js("D.SHEET.open({type: 'op', kind: 'tx', acc: arg});", acc)
        a.wait(500)
        sh = a.pg.locator(".sheet")
        sh.locator('[role=tab]:has-text("%s"), button:has-text("%s")' % (mode, mode)).first.click()
        sh.locator("#op-amt").fill(total)
        sh.locator("#op-name").fill(name)
        sh.locator('button:has-text("Plusieurs catégories")').click()
        a.wait(300)
        # parts : [(montant, négatif?, compte de virement ou None)] ; la 2e ligne vide de départ est retirée à la fin
        for k, (v, neg, to) in enumerate(parts):
            row = 0 if k == 0 else k + 1
            if k > 0:
                sh.locator('button:has-text("%s")' % ("+ Ajouter un virement" if to else "+ Ajouter une part")).click()
                a.wait(200)
            sh.locator('[aria-label="Montant de la part"]').nth(row).fill(v)
            if neg:
                sh.locator('[aria-label="Inverser le signe de la part"]').nth(row).click()
            if to:
                sh.locator('[aria-label="Virement vers"]').last.select_option(to)
        sh.locator('[aria-label="Retirer la part"]').nth(1).click()
        a.wait(200)
        a.pg.screenshot(path=os.path.join(OUT, "ventilation-virement-%s.png" % mode))
        check("Équilibrée" in sh.inner_text(), "%s : ventilation pas équilibrée" % name)
        sh.locator('button:has-text("Enregistrer")').last.click()
        a.wait(600)
        return a.js("return Object.values(D.A().tx).find(x => x.name === arg);", name)
    t = fill(cour, "Dépense", "50", "Courses et épargne", [("30", False, None), ("20", False, liv)])
    check(t and t["amount"] == -50 and sorted([sp["amount"], sp.get("to")] for sp in t["splits"]) == sorted([[-30, None], [-20, liv]]), "dépense : %s" % (t and t["splits"]))
    b1 = a.js("return [D.balance(arg[0]), D.balance(arg[1])];", [cour, liv])
    check(near(b1[0], b0[0] - 50) and near(b1[1], b0[1] + 20), "soldes après la dépense : %s (avant %s)" % (b1, b0))
    t2 = fill(j, "Revenu", "100", "Marie ventilée", [("120", False, None), ("20", True, liv)])
    check(t2 and t2["amount"] == 100 and sorted([sp["amount"], sp.get("to")] for sp in t2["splits"]) == sorted([[120, None], [-20, liv]]), "revenu : %s" % (t2 and t2["splits"]))
    a.js("D.SHEET.open({type: 'op', kind: 'tx', src: D.A().tx[arg]});", t["id"])
    a.wait(500)
    check(a.pg.locator('.sheet [aria-label="Virement vers"]').input_value() == liv, "compte du virement perdu à la réouverture")
    a.js("D.SHEET.close();")
    a.no_errors("ventilation avec virement")
    a.close()


@test
def compte_joint_prorata(br, url):
    """Prorata des salaires : seuls les éléments cochés (échéances du joint et de ses cartes, enveloppes du joint, rentrées) comptent ; deux lignes par mois (ton virement, la ligne de l'autre) ; annuelle lissée ; ligne validée figée."""
    a = App(br, url)
    a.import_ic(FICTIF)
    j, cour, cj = a.acc_id("Joint"), a.acc_id("Courant"), a.acc_id("Carte Joint")
    # réglage par l'écran
    a.js("D.NAV.push({v: 'joint'});")
    a.wait(400)
    a.pg.click('main button:has-text("Configurer")')
    a.wait(400)
    sh = a.pg.locator(".sheet")
    sh.locator("#pr-f").select_option(cour)
    sh.locator("#pr-n").fill("Claire")
    sh.locator("#pr-m").fill("3000")
    sh.locator("#pr-p").fill("2000")
    check("60 %" in sh.inner_text(), "prorata 60/40 non affiché")
    sh.locator('button:has-text("Enregistrer")').last.click()
    a.wait(500)
    check(a.js("return D.prOn();") is True, "réglage du prorata non enregistré")
    # cocher le loyer (échéance du joint) par sa fiche
    loyer = a.js("return Object.values(D.A().sch).find(s => s.name === 'Loyer').id;")
    a.js("D.SHEET.open({type: 'op', kind: 'sch', src: D.A().sch[arg]});", loyer)
    a.wait(500)
    a.pg.locator('.sheet [role=switch]:has-text("Au prorata des salaires")').click()
    a.wait(200)
    a.pg.locator('.sheet button:has-text("Enregistrer")').last.click()
    a.wait(500)
    check(a.js("return !!D.A().sch[arg].prorata;", loyer), "case « Au prorata » de l'échéance non enregistrée")
    # cocher l'enveloppe Courses (budget du joint) par sa fiche
    lid = a.js("return Object.values(D.A().bud).find(l => D.A().cat[l.cat]?.name === 'Courses').id;")
    a.js("D.SHEET.open({type: 'line', id: arg});", lid)
    a.wait(500)
    a.pg.locator('.sheet [role=switch]:has-text("Au prorata des salaires")').click()
    a.wait(200)
    a.pg.locator('.sheet button:has-text("Enregistrer")').last.click()
    a.wait(500)
    check(a.js("return !!D.A().bud[arg].prorata;", lid), "case « Au prorata » de l'enveloppe non enregistrée")
    # pas de case pour une échéance hors joint ; une annuelle sur la carte du joint (lissée) et une rentrée du joint
    epargne = a.js("return Object.values(D.A().sch).find(s => s.name === 'Épargne').id;")
    a.js("D.SHEET.open({type: 'op', kind: 'sch', src: D.A().sch[arg]});", epargne)
    a.wait(400)
    check("Au prorata" not in a.pg.inner_text(".sheet"), "case prorata proposée hors compte joint")
    a.js("D.SHEET.close();")
    a.js("""const [j, cj] = arg; D.Store.bulk([
      ['put', {k: 'sch', id: 'sch_t_assur', account: cj, name: 'Assurance carte', amount: -120, splits: [{amount: -120, cat: null}], freq: {n: 1, u: 'y'}, next: '2027-03-10', anchor: 10, prorata: true}],
      ['put', {k: 'sch', id: 'sch_t_caf', account: j, name: 'CAF', amount: 100, splits: [{amount: 100, cat: null}], freq: {n: 1, u: 'm'}, next: '2026-12-08', anchor: 8, prorata: true}]]);""", [j, cj])
    a.wait(500)
    # un virement vers le joint peut aussi être coché : vu du joint, il réduit le total
    vj = a.js("return Object.values(D.A().sch).find(s => s.name === 'Virement au Joint').id;")
    a.js("D.SHEET.open({type: 'op', kind: 'sch', src: D.A().sch[arg]});", vj)
    a.wait(500)
    check("Au prorata des salaires" in a.pg.inner_text(".sheet"), "pas de case prorata sur un virement vers le joint")
    a.js("D.SHEET.close();")
    a.js("D.Store.put({...D.A().sch[arg], prorata: true});", vj)
    a.wait(300)
    check(near(a.js("return D.prMonth('2026-12').N;"), 1200 + 600 + 10 - 100 - 2000), "virement coché mal compté : %s" % a.js("return D.prMonth('2026-12').N;"))
    a.js("D.Store.put({...D.A().sch[arg], prorata: false});", vj)
    a.wait(300)
    m = a.js("return D.prMonth('2026-12');")
    check(near(m["N"], 1200 + 600 + 10 - 100), "total à partager en décembre : %s (attendu 1710)" % m["N"])
    check(near(m["mine"], 1026) and near(m["hers"], 684), "parts : %s / %s (attendu 1026 / 684)" % (m["mine"], m["hers"]))
    a.wait(400)
    check(sorted(a.js("return Object.values(D.A().sch).filter(s => s.prAuto).map(s => s.name);")) == ["Claire (prorata)", "Virement au Joint (prorata)"], "les deux échéances du prorata ne sont pas créées")
    occ = a.js("return D.occurrences('2026-12-31').filter(o => /^sch_pr_/.test(o.sch || '') && o.date.startsWith('2026-12')).map(o => [o.account, o.name, o.amount, o.date, (o.splits[0] || {}).to || null]);")
    check(sorted(occ) == sorted([[cour, "Virement au Joint (prorata)", -1026, "2026-12-01", j], [j, "Claire (prorata)", 684, "2026-12-01", None]]), "lignes de décembre : %s" % occ)
    # ligne validée : elle ne bouge plus
    a.js("""const o = D.occurrences('2026-12-31').find(x => x.sch === 'sch_pr_her' && x.date === '2026-12-01'); const {virtual, odate, ...t} = o; D.Store.put({...t, k: 'tx', st: 'saisie'});""")
    vid = a.js("return D.occurrences('2027-01-31').length >= 0 && Object.keys(D.A().tx).find(id => id.startsWith('tx_s_sch_pr_her_2026-12'));")
    a.js("D.Store.put({...D.A().sch.sch_t_caf, prorata: false});")
    a.wait(500)
    check(vid and a.js("return D.A().tx[arg].amount;", vid) == 684, "ligne validée modifiée")
    check(not a.js("return D.occurrences('2026-12-31').some(o => o.sch === 'sch_pr_her' && o.date.startsWith('2026-12'));"), "ligne validée encore prévue")
    check(near(a.js("return D.occurrences('2027-01-31').find(o => o.sch === 'sch_pr_her' && o.date.startsWith('2027-01')).amount;"), 724), "janvier ne suit pas le nouveau total")
    check(near(a.js("return D.prMonth('2026-12').N;"), 1810), "décocher la CAF ne change pas le total")
    # budget entier au prorata (réglage du budget) : la ligne Courses compte même décochée
    bid = a.js("return D.A().bud[arg].budget;", lid)
    a.js("D.Store.put({...D.A().bud[arg], prorata: false});", lid)
    a.js("D.SHEET.open({type: 'bdg', id: arg});", bid)
    a.wait(500)
    a.pg.locator('.sheet [role=switch]:has-text("Au prorata des salaires")').click()
    a.wait(200)
    a.pg.locator('.sheet button:has-text("Enregistrer")').last.click()
    a.wait(500)
    check(a.js("return !!D.A().bdg[arg].prorata;", bid), "case « Au prorata » du budget non enregistrée")
    check(any(x["name"].startswith("Courses") for x in a.js("return D.prMonth('2027-01').items;")), "budget au prorata non compté")
    # échéancier du joint : la ligne de l'autre personne apparaît avec son montant
    a.pg.evaluate("location.hash = '#echeancier'")
    a.wait(400)
    a.pg.locator('main [aria-label^="Changer de compte"]').click()
    a.wait(300)
    a.pg.click('.sheet .row:has-text("Joint") >> nth=0')
    a.wait(400)
    ech = a.pg.inner_text("main")
    check("Claire (prorata)" in ech, "ligne du prorata absente de l'échéancier du joint : %s" % ech.replace("\n", " | ")[-400:])
    check("Échéance Prêt" in ech, "mensualité du prêt absente de l'échéancier du joint")
    a.pg.screenshot(path=os.path.join(OUT, "echeancier-joint.png"), full_page=True)
    # prêt payé par le joint : case dans sa fiche, mensualité comptée
    n0 = a.js("return D.prMonth('2027-01').N;")
    a.pg.click('main .row:has-text("Échéance Prêt")')
    a.wait(500)
    a.pg.locator('.sheet [role=switch]:has-text("Au prorata des salaires")').click()
    a.wait(200)
    a.pg.locator('.sheet button:has-text("Enregistrer")').last.click()
    a.wait(500)
    check(a.js("return !!D.A().acc[arg].loan.prorata;", a.acc_id("Prêt")), "case prorata du prêt non enregistrée")
    check(near(a.js("return D.prMonth('2027-01').N;"), n0 + 500), "mensualité du prêt non comptée : %s (avant %s)" % (a.js("return D.prMonth('2027-01').N;"), n0))
    a.js("D.NAV.push({v: 'joint'});")
    a.wait(400)
    check("à partager" in a.pg.inner_text("main"), "écran du prorata vide")
    a.pg.screenshot(path=os.path.join(OUT, "prorata.png"), full_page=True)
    a.no_errors("prorata")
    a.close()


@test
def tri_et_filtres_retenus(br, url):
    """Comptes : tri « Date, récentes d'abord » par défaut ; le tri et les filtres choisis sont retenus (même après réouverture de l'app) ; « Tout effacer » revient au défaut."""
    a = App(br, url)
    a.import_ic(FICTIF)
    cour = a.acc_id("Courant")
    a.js("D.NAV.push({v: 'acc', id: arg});", cour)
    a.wait(500)
    a.pg.click('main button:has-text("Opérations")')
    a.wait(300)
    a.pg.click('[aria-label="Filtres et tri"]')
    a.wait(400)
    check(a.pg.locator("#fl-s").input_value() == "dd", "tri par défaut : %s" % a.pg.locator("#fl-s").input_value())
    a.pg.locator("#fl-s").select_option("da")
    a.pg.locator('.sheet button:has-text("Appliquer")').last.click()
    a.wait(400)
    check(a.js("return D.S().accSort;") == "da", "tri non retenu")
    a.pg.reload()
    a.wait(1500)
    a.js("D.NAV.push({v: 'acc', id: arg});", cour)
    a.wait(500)
    a.pg.click('main button:has-text("Opérations")')
    a.wait(300)
    a.pg.click('[aria-label="Filtres et tri"]')
    a.wait(400)
    check(a.pg.locator("#fl-s").input_value() == "da", "tri perdu après réouverture")
    a.pg.locator('.sheet button:has-text("Tout effacer")').click()
    a.wait(400)
    check(a.js("return D.S().accSort;") == "dd", "« Tout effacer » ne revient pas au tri par défaut")
    a.no_errors("tri retenu")
    a.close()


@test
def verifications_donnees(br, url):
    """Vérifications : question 50/50 posée une fois sur l'accueil, incohérences repérées (50/50 sur un livret, carte sans compte, compte supprimé, ventilation fausse, livret négatif, budget périmé) et nettoyage."""
    a = App(br, url)
    a.import_ic(FICTIF)
    check(a.js("return D.dataChecks().map(c => c.key);") == [], "fichier propre signalé : %s" % a.js("return D.dataChecks().map(c => c.title);"))
    j, liv, cour = a.acc_id("Joint"), a.acc_id("Livret"), a.acc_id("Courant")
    # 1. 50/50 utilisé : Pécule demande confirmation une fois
    a.js("""D.Store.put({k: 'tx', id: 'tx_v_half', account: arg, date: '2026-11-12', name: 'Cinéma', amount: -40, splits: [{amount: -40, cat: null}], st: 'saisie', half: true});""", j)
    a.wait(500)
    a.pg.evaluate("location.hash = '#accueil'")
    a.wait(400)
    main = a.pg.inner_text("main")
    check("Remboursement 50/50" in main and "Courant" in main, "question 50/50 absente de l'accueil")
    a.pg.screenshot(path=os.path.join(OUT, "accueil-50-50.png"))
    a.pg.click('main button:has-text("Oui")')
    a.wait(400)
    check(a.js("return [D.S().halfOk, D.S().halfFrom] ;") == [True, cour], "confirmation 50/50 non enregistrée : %s" % a.js("return [D.S().halfOk, D.S().halfFrom];"))
    check("Remboursement 50/50" not in a.pg.inner_text("main"), "la question 50/50 reste affichée")
    # 2. moitié réglée sur un livret : signalé sur l'accueil et dans la fiche
    a.js("D.Store.put({...D.S(), halfFrom: arg});", liv)
    a.wait(400)
    check("half-from" in a.js("return D.dataChecks().map(c => c.key);"), "50/50 depuis un livret non signalé")
    check("part du Livret" in a.pg.inner_text("main"), "alerte 50/50 absente de l'accueil")
    a.js("D.SHEET.open({type: 'half'});")
    a.wait(400)
    check("n'est pas un compte courant" in a.pg.inner_text(".sheet"), "pas d'avertissement dans la fiche 50/50")
    a.js("D.SHEET.close();")
    a.wait(300)
    a.js("D.Store.put({...D.S(), halfFrom: arg});", cour)
    # 3. autres incohérences
    bud = a.js("return Object.values(D.A().bdg).find(b => /Loisirs/.test(b.name)).id;")
    a.js("""const [j, l, bud] = arg; D.Store.bulk([
      ['put', {k: 'acc', id: 'acc_v_card', name: 'Carte orpheline', type: 'carte', linked: 'acc_nope', debitDay: 4, initial: 0, hidden: false, order: 50}],
      ['put', {k: 'tx', id: 'tx_v_lost', account: j, date: '2026-11-05', name: 'Vers un compte disparu', amount: -10, splits: [{to: 'acc_nope', amount: -10}], st: 'saisie'}],
      ['put', {k: 'tx', id: 'tx_v_sum', account: j, date: '2026-11-06', name: 'Ventilation fausse', amount: -30, splits: [{amount: -20, cat: null}], st: 'saisie'}],
      ['put', {k: 'tx', id: 'tx_v_neg', account: l, date: '2026-11-07', name: 'Retrait énorme', amount: -999999, splits: [{amount: -999999, cat: null}], st: 'pointee'}],
      ['put', {k: 'tx', id: 'tx_v_bdg', account: j, date: '2026-11-08', name: 'Rattachement périmé', amount: -12, splits: [{amount: -12, cat: null, bdg: bud}], st: 'saisie'}]]);""", [j, liv, bud])
    a.wait(600)
    keys = a.js("return D.dataChecks().map(c => c.key);")
    for k in ["card-acc_v_card", "lost", "split-sum", "neg-" + liv, "bdg-stale"]:
        check(k in keys, "incohérence non repérée : %s (repérées : %s)" % (k, keys))
    check("incohérences" in a.pg.inner_text("main"), "pas d'alerte d'incohérences sur l'accueil")
    a.pg.evaluate("location.hash = '#reglages'")
    a.js("D.NAV.push({v: 'health'});")
    a.wait(500)
    a.pg.screenshot(path=os.path.join(OUT, "verifications.png"), full_page=True)
    a.pg.click('.card:has-text("budget qui ne suit pas") button:has-text("Nettoyer")')
    a.wait(500)
    check("bdg-stale" not in a.js("return D.dataChecks().map(c => c.key);"), "« Nettoyer » n'a pas retiré le rattachement")
    check(a.js("return D.A().tx.tx_v_bdg.splits[0].bdg;") is None, "rattachement toujours présent")
    a.no_errors("vérifications")
    a.close()


@test
def import_echeances_semaines_et_montant(br, url):
    """Import iCompta : échéance toutes les 4 semaines (unité 1 = semaine) et montant porté par l'échéance (« useSumOfSplits » à 0), cas d'Elliott."""
    path = os.path.join(OUT, "echeances.cdb")
    if os.path.exists(path):
        os.remove(path)
    f, names = fixture_icompta.build_echeances(path)
    a = App(br, url)
    a.import_ic(path)
    sch = a.js("return Object.fromEntries(Object.values(D.A().sch).map(s => [s.name, [s.amount, s.freq.n + s.freq.u, s.next]]));")
    check(sch.get("Salle de sport") == [-29.99, "4w", "2026-12-11"], "salle de sport : %s" % sch.get("Salle de sport"))
    check(sch.get("Argent de poche") == [220, "1m", "2026-12-01"], "argent de poche : %s" % sch.get("Argent de poche"))
    dates = ["2026-11-30", "2026-12-31", "2027-01-31", "2027-02-28"]
    want, got = fixture_icompta.truth(f, names, dates)["Courant"], a.projected(dates)["Courant"]
    for d, x, y in zip(dates, got, want):
        check(near(x, y), "Courant au %s : Pécule %s, iCompta %s" % (d, x, y))
    a.no_errors("échéances semaines")
    a.close()


@test
def ventilation_part_negative(br, url):
    """Ventilation : une part peut être négative (bouton ±), l'équilibre se fait sur la somme signée, et le signe est conservé à la réouverture."""
    a = App(br, url)
    a.import_ic(FICTIF)
    j = a.acc_id("Joint")
    a.js("D.SHEET.open({type: 'op', kind: 'tx', acc: arg, mode: 'income'});", j)
    a.wait(500)
    sh = a.pg.locator(".sheet")
    sh.locator('button:has-text("Revenu"), [role=tab]:has-text("Revenu")').first.click() if sh.locator('button:has-text("Revenu"), [role=tab]:has-text("Revenu")').count() else None
    a.wait(200)
    sh.locator("#op-amt").fill("100")
    sh.locator("#op-name").fill("Marie test")
    sh.locator('button:has-text("Plusieurs catégories")').click()
    a.wait(300)
    parts = sh.locator('[aria-label="Montant de la part"]')
    parts.nth(0).fill("120")
    parts.nth(1).fill("20")
    sh.locator('[aria-label="Inverser le signe de la part"]').nth(1).click()
    a.wait(200)
    sh.locator('[aria-label="Montant de la part"]').nth(1).scroll_into_view_if_needed()
    a.pg.screenshot(path=os.path.join(OUT, "ventilation.png"))
    check("Équilibrée" in sh.inner_text(), "ventilation 120 / −20 pas équilibrée : %s" % sh.inner_text()[-200:])
    sh.locator('button:has-text("Enregistrer")').last.click()
    a.wait(600)
    t = a.js("return Object.values(D.A().tx).find(x => x.name === 'Marie test');")
    check(t and t["amount"] == 100 and sorted(sp["amount"] for sp in t["splits"]) == [-20, 120], "opération enregistrée : %s" % (t and [t["amount"], [sp["amount"] for sp in t["splits"]]]))
    a.js("D.SHEET.open({type: 'op', kind: 'tx', src: D.A().tx[arg]});", t["id"])
    a.wait(500)
    vals = a.pg.locator('.sheet [aria-label="Montant de la part"]').evaluate_all("els => els.map(e => e.value)")
    check(sorted(vals) == ["-20", "120"], "signe perdu à la réouverture : %s" % vals)
    a.js("D.SHEET.close();")
    a.no_errors("part négative")
    a.close()


@test
def valider_supprimer_annuler(br, url):
    """Valider une opération, la supprimer, annuler la suppression."""
    a = App(br, url)
    a.import_ic(FICTIF)
    c = a.acc_id("Courant")
    v0 = a.js("return D.balance(arg, {cleared: true});", c)
    t = a.js("return Object.values(D.A().tx).find(t => t.account === arg && t.st === 'saisie' && t.name === 'Frais bancaires');", c)
    check(t, "opération « Frais bancaires » à valider introuvable")
    a.pick("Courant")
    a.pg.locator("main .row:has-text('Frais bancaires') [aria-label^='Statut']").first.click()
    a.wait(500)
    v1 = a.js("return D.balance(arg, {cleared: true});", c)
    check(near(v1, v0 - 6.5), "validation : solde validé %s → %s (attendu −6,50)" % (v0, v1))
    b0 = a.js("return D.balance(arg);", c)
    a.js("D.Store.del(D.A().tx[arg]);", t["id"])
    a.wait(800)
    check(near(a.js("return D.balance(arg);", c), b0 + 6.5), "suppression sans effet sur le solde")
    a.js("D.Hist.close(); return D.Hist.undo(D.Hist.list[0]);")
    a.wait(800)
    check(a.js("return !!D.A().tx[arg];", t["id"]), "annulation : opération non restaurée")
    a.no_errors("valider")
    a.close()


@test
def sauvegarde_aller_retour(br, url):
    """Télécharger la sauvegarde, repartir de zéro, la restaurer : données identiques."""
    a = App(br, url)
    a.import_ic(FICTIF)
    a.js("D.NAV.push({v: 'data'});")
    a.wait(500)
    with a.pg.expect_download() as dl:
        a.pg.click("text=Télécharger une sauvegarde complète (JSON)")
    path = os.path.join(OUT, "sauvegarde-test.json")
    dl.value.save_as(path)
    snap = "const U = ['lastExport', 'bakSnooze', 'theme', 'guideDone', 'prevSpan', 'glanceSpan']; return D.Store.all().filter(o => o.k !== 'bak').map(o => { o = {...o}; if (o.k === 'set') U.forEach(k => delete o[k]); return JSON.stringify(o); }).sort();"
    before = a.js(snap)
    proj = a.projected(DATES)
    a.close()
    b = App(br, url)
    with b.pg.expect_file_chooser() as fc:
        b.pg.get_by_text("Restaurer une sauvegarde").first.click()
    fc.value.set_files(path)
    b.wait(600)
    if b.pg.locator('.sheet button:has-text("Restaurer")').count():
        b.pg.locator('.sheet button:has-text("Restaurer")').last.click()
    b.wait(3500)
    after = b.js(snap)
    missing = set(before) - set(after)
    check(not missing, "%d éléments perdus à la restauration : %s" % (len(missing), sorted(missing)[:2]))
    check(b.projected(DATES) == proj, "soldes projetés différents après restauration")
    b.no_errors("restauration")
    b.close()


@test
def saisie_echeance(br, url):
    """Bouton + › Échéance : dépense de 15 €, catégorie Divers, à partir du 12 nov. (date choisie) jusqu'au 12 janv. 2027 (fin facultative, refusée si avant le début)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    n0 = a.js("return Object.keys(D.A().sch).length;")
    a.pg.click('[aria-label="Saisir"]')
    a.wait(300)
    a.pg.click(".wiz >> text=Échéance")
    a.wait(300)
    a.pg.click(".wiz >> text=Une dépense")
    a.wait(300)
    for k in "15":
        a.pg.click('.wiz button:text-is("%s")' % k)
    a.pg.click(".wgo")
    a.wait(300)
    a.pg.locator('.wiz .wbig:has-text("Divers")').first.click()
    a.wait(300)
    # étape « Quand commence-t-elle ? » : date de début au choix, date de fin facultative
    check("Quand commence-t-elle" in a.pg.locator(".wiz").inner_text(), "étape de la date de début absente")
    a.pg.fill('[aria-label="Date de la première échéance"]', "2026-11-12")
    a.wait(200)
    a.pg.locator('.wiz [role=switch]:has-text("Avec une date de fin")').click()
    a.wait(200)
    a.pg.fill('[aria-label="Date de la dernière échéance"]', "2026-10-01")
    a.wait(200)
    check(a.pg.locator(".wgo").is_disabled() and "avant la première" in a.pg.locator(".wiz").inner_text(), "fin avant le début acceptée")
    a.pg.fill('[aria-label="Date de la dernière échéance"]', "2027-01-12")
    a.wait(200)
    a.pg.click(".wgo")
    a.wait(300)
    rec = a.pg.locator(".wiz").inner_text()
    check("Dès le 12 nov." in rec and "12 janv. 2027" in rec, "récapitulatif : %r" % rec[-200:])
    for _ in range(3):
        if a.pg.locator('.wgo:has-text("Enregistrer")').count():
            break
        a.pg.click(".wgo")
        a.wait(300)
    a.pg.click('.wgo:has-text("Enregistrer")')
    a.wait(700)
    s = a.js("return Object.values(D.A().sch).find(s => s.amount === -15);")
    check(a.js("return Object.keys(D.A().sch).length;") == n0 + 1 and s, "échéance non créée")
    check(s["next"] == "2026-11-12", "prochaine date : %s (attendu 2026-11-12)" % s["next"])
    check(a.js("return D.A().cat[arg]?.name;", s["splits"][0].get("cat")) == "Divers", "catégorie de l'échéance")
    occ = a.js("return D.occurrences('2027-01-31').filter(o => o.name === arg).map(o => o.date);", s["name"])
    check(occ[:3] == ["2026-11-12", "2026-12-12", "2027-01-12"], "occurrences : %s" % occ)
    occ = a.js("return D.occurrences('2027-12-31').filter(o => o.name === arg).map(o => o.date);", s["name"])
    check(occ == ["2026-11-12", "2026-12-12", "2027-01-12"] and s.get("end") == "2027-01-12" and s.get("start") == "2026-11-12", "fin non respectée : %s (fin %s)" % (occ, s.get("end")))
    a.no_errors("échéance")
    a.close()


@test
def restauration_exacte(br, url):
    """Réglages › Sauvegarde › Restaurer : retour exact à l'état sauvegardé (ce qui a été saisi depuis disparaît)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    snap = "const U = ['lastExport', 'bakSnooze', 'theme', 'guideDone', 'prevSpan', 'glanceSpan']; return D.Store.all().filter(o => o.k !== 'bak').map(o => { o = {...o}; if (o.k === 'set') U.forEach(k => delete o[k]); return JSON.stringify(o); }).sort();"
    a.js("D.NAV.push({v: 'data'});")
    a.wait(500)
    with a.pg.expect_download() as dl:
        a.pg.click("text=Télécharger une sauvegarde complète (JSON)")
    path = os.path.join(OUT, "sauvegarde-exacte.json")
    dl.value.save_as(path)
    before = a.js(snap)
    a.js("D.Store.put({k: 'tx', id: 'tx_apres', account: D.accList()[0].id, date: '2026-11-11', name: 'Saisie après la sauvegarde', amount: -5, splits: [{amount: -5}], st: 'saisie'});")
    a.js("const s = {...Object.values(D.A().sch)[0]}; s.amount = -1; s.splits = [{...s.splits[0], amount: -1}]; D.Store.put(s);")
    a.wait(300)
    a.pg.set_input_files("#dv-r", path)
    a.wait(500)
    a.pg.locator('.sheet button:has-text("Restaurer")').last.click()
    a.wait(3500)
    after = a.js(snap)
    check(not a.js("return !!D.A().tx.tx_apres;"), "l'opération saisie après la sauvegarde est toujours là")
    check(set(before) == set(after), "%d différence(s) après restauration" % len(set(before) ^ set(after)))
    a.no_errors("restauration")
    a.close()


@test
def previsions_et_themes(br, url):
    """Prévisions 12, 24, 36 mois sur chaque compte ; thèmes clair, sombre, automatique."""
    a = App(br, url)
    a.import_ic(FICTIF)
    a.pg.evaluate("location.hash = '#prev'")
    a.wait(500)
    for acc in ["Courant", "Joint", "Livret", "Carte Perso", "Prêt"]:
        a.js("D.Store.put({...D.S(), k: 'set', id: 'set_main', mainAcc: Object.values(D.A().acc).find(x => x.name === arg).id});", acc)
        for n in ("12 mois", "24 mois", "36 mois"):
            if a.pg.locator('main button:text-is("%s")' % n).count():
                a.pg.locator('main button:text-is("%s")' % n).first.click()
                a.wait(300)
            a.no_errors("prévisions %s %s" % (acc, n))
    for th in ("dark", "light", "auto"):
        a.js("D.Store.put({...D.S(), k: 'set', id: 'set_main', theme: arg});", th)
        a.wait(250)
        a.no_errors("thème %s" % th)
    a.close()


@test
def valider_virement(br, url):
    """Valider un virement entre tes comptes d'un seul geste : les deux côtés le sont ; idem pour revenir en « créée »."""
    a = App(br, url)
    a.import_ic(FICTIF)
    c, j = a.acc_id("Courant"), a.acc_id("Joint")
    t = a.js("return Object.values(D.A().tx).find(t => t.name === 'Virement au Compte joint' && t.date === '2026-11-29');")
    check(t and t.get("st") == "saisie" and t.get("st2") == "saisie", "virement de test : %s" % t)
    vc, vj = a.js("return D.balance(arg, {cleared: true, upTo: '2026-11-30'});", c), a.js("return D.balance(arg, {cleared: true, upTo: '2026-11-30'});", j)
    a.pick("Joint")
    a.pg.locator("main .row").filter(has_text="Virement").filter(has_text="29").first.locator("[aria-label^='Statut']").click()
    a.wait(500)
    t2 = a.js("return D.A().tx[arg];", t["id"])
    check(t2["st"] == "pointee" and t2["st2"] == "pointee", "validé depuis le Joint : statuts %s / %s" % (t2["st"], t2["st2"]))
    check(near(a.js("return D.balance(arg, {cleared: true, upTo: '2026-11-30'});", c), vc - 30) and near(a.js("return D.balance(arg, {cleared: true, upTo: '2026-11-30'});", j), vj + 30), "soldes validés des deux comptes")
    a.pg.locator("main .row").filter(has_text="Virement").filter(has_text="29").first.locator("[aria-label^='Statut']").click() if a.pg.locator("main .row").filter(has_text="Virement").filter(has_text="29").count() else a.js("D.Store.put({...D.A().tx[arg], st: 'saisie', st2: 'saisie'});", t["id"])
    a.wait(500)
    a.no_errors("virement")
    a.close()


@test
def reorganiser_comptes(br, url):
    """Comptes › Réorganiser : les cartes se rangent sous leur compte ; ↑ échange avec la voisine affichée ; l'accueil suit."""
    a = App(br, url)
    a.import_ic(FICTIF)
    def cards_home():
        a.pick("Joint")
        return [x.split(" : ")[0] for x in a.pg.locator("button.ktile").evaluate_all("l => l.map(b => b.getAttribute('aria-label'))")]
    before = cards_home()
    check(len(before) == 2, "cartes sous le Joint : %s" % before)
    a.pg.evaluate("location.hash = '#mes'")
    a.wait(400)
    a.pg.click("text=Réorganiser")
    a.wait(300)
    rows = a.pg.locator("main .card .row .t1").all_inner_texts()
    check(rows.index(before[1]) == rows.index(before[0]) + 1 and rows.index(before[0]) == rows.index("Joint") + 1, "cartes non rangées sous le Joint : %s" % rows)
    check(a.pg.locator('[aria-label="Monter %s"]' % before[0]).is_disabled(), "la première carte peut encore monter")
    a.pg.click('[aria-label="Monter %s"]' % before[1])
    a.wait(500)
    a.pg.click('[aria-label="Terminer la réorganisation"]')
    after = cards_home()
    check(after == before[::-1], "ordre des cartes sur l'accueil : %s (attendu %s)" % (after, before[::-1]))
    a.pg.evaluate("location.hash = '#mes'")
    a.wait(300)
    a.pg.click("text=Réorganiser")
    a.wait(300)
    a.pg.click('[aria-label="Monter Joint"]')
    a.wait(500)
    rows = a.pg.locator("main .card .row .t1").all_inner_texts()
    check(rows[:4] == ["Joint", after[0], after[1], "Courant"], "Joint monté avec ses cartes : %s" % rows[:5])
    a.no_errors("réorganiser")
    a.close()


@test
def menus_comptes_dans_l_ordre(br, url):
    """Les menus déroulants de comptes suivent l'ordre de l'écran Comptes (chaque compte suivi de ses cartes), même si l'ordre enregistré est désordonné."""
    a = App(br, url)
    a.import_ic(FICTIF)
    # ordre enregistré volontairement désordonné : les cartes en fin de liste, mélangées
    a.js("""const L = Object.values(D.A().acc); const pos = n => ({'Courant': 0, 'Joint': 1, 'Livret': 2, 'Prêt': 3, 'Carte Claire': 4, 'Carte Perso': 5, 'Carte Joint': 6})[n] ?? 9;
            D.Store.bulk(L.map(x => ['put', {...x, order: pos(x.name)}]));""")
    a.wait(400)
    def screen_order():
        a.pg.evaluate("location.hash = '#mes'")
        a.wait(400)
        a.pg.click("text=Réorganiser")
        a.wait(300)
        rows = a.pg.locator("main .card .row .t1").all_inner_texts()
        a.pg.click('[aria-label="Terminer la réorganisation"]')
        a.wait(200)
        return rows
    def menu_order():
        a.js("D.SHEET.open({type: 'op'});")
        a.wait(500)
        opts = a.pg.evaluate("() => [...document.querySelectorAll('#op-acc option')].map(o => o.text.trim())")
        a.js("D.SHEET.close();")
        a.wait(300)
        return opts
    rows, opts = screen_order(), menu_order()
    check(len(opts) >= 5, "menu des comptes introuvable : %s" % opts)
    check([o for o in opts if o in rows] == [r for r in rows if r in opts], "menu %s ≠ écran Comptes %s" % (opts, rows))
    j = opts.index("Joint")
    check(opts[j + 1].startswith("Carte") and opts[j + 2].startswith("Carte"), "cartes du Joint pas sous le Joint : %s" % opts)
    # on inverse les deux cartes du Joint : le menu suit
    first, second = opts[j + 1], opts[j + 2]
    a.pg.evaluate("location.hash = '#mes'")
    a.wait(400)
    a.pg.click("text=Réorganiser")
    a.wait(300)
    a.pg.click('[aria-label="Monter %s"]' % second)
    a.wait(500)
    a.pg.click('[aria-label="Terminer la réorganisation"]')
    a.wait(200)
    opts2 = menu_order()
    j2 = opts2.index("Joint")
    check(opts2[j2 + 1:j2 + 3] == [second, first], "menu après inversion : %s" % opts2)
    # même ordre dans le menu de l'assistant +
    a.pg.evaluate("location.hash = '#accueil'")
    a.wait(300)
    texts = a.pg.evaluate("() => [...document.querySelectorAll('select')].map(s => [...s.options].map(o => o.text.trim()))")
    for t in texts:
        if second in t and first in t:
            check(t.index(second) < t.index(first), "un menu garde l'ancien ordre : %s" % t)
    a.no_errors("menus de comptes")
    a.close()


@test
def echeancier_choix_du_compte(br, url):
    """Échéancier : même menu « Choisir un compte » que l'accueil (cartes sous leur compte) ; le choix ne change pas le compte de l'accueil."""
    a = App(br, url)
    a.import_ic(FICTIF)
    a.pick("Courant")
    main0 = a.js("return D.S().mainAcc;")
    a.pg.evaluate("location.hash = '#echeancier'")
    a.wait(500)
    pill = a.pg.locator('main [aria-label^="Changer de compte"]')
    check(pill.count() == 1, "pas de menu de compte dans l'échéancier")
    check(not a.pg.locator('main [role=tablist][aria-label="Compte"]').count(), "les anciennes pastilles de comptes sont encore là")
    pill.click()
    a.wait(400)
    sheet = a.pg.inner_text(".sheet")
    check("Choisir un compte" in sheet and "Carte Joint" in sheet and "Carte Claire" in sheet, "feuille de choix incomplète : %s" % sheet.replace("\n", " | ")[:300])
    a.pg.click('.sheet .row:has-text("Joint") >> nth=0')
    a.wait(500)
    check("Joint" in a.pg.locator('main [aria-label^="Changer de compte"]').inner_text(), "le menu n'affiche pas le compte choisi")
    body = a.pg.inner_text("main")
    check("Virement au Joint" in body or "Virement" in body, "échéances du Joint absentes : %s" % body.replace("\n", " | ")[:300])
    check(a.js("return D.S().mainAcc;") == main0, "choisir dans l'échéancier a changé le compte de l'accueil")
    a.pg.screenshot(path=os.path.join(OUT, "echeancier.png"))
    # une carte peut être choisie aussi
    a.pg.locator('main [aria-label^="Changer de compte"]').click()
    a.wait(400)
    a.pg.click('.sheet .nest .row:has-text("Carte Joint")')
    a.wait(500)
    check("Carte Joint" in a.pg.locator('main [aria-label^="Changer de compte"]').inner_text(), "carte non choisie dans l'échéancier")
    a.no_errors("échéancier, choix du compte")
    a.close()


@test
def rentrees_et_solde_de_depart(br, url):
    """Accueil sans « Prochaine rentrée » (maquette F). Changer le signe du solde de départ : avertissement avant d'enregistrer."""
    a = App(br, url)
    a.import_ic(FICTIF)
    c = a.acc_id("Courant")
    a.js("D.Store.put({k: 'tx', id: 'tx_prime', account: arg, date: '2026-11-28', name: 'Prime', amount: 300, splits: [{amount: 300}], st: 'saisie'});", c)
    a.pick("Courant")
    t = a.pg.inner_text("main section")
    check("rentrée" not in t.lower() and "Solde validé" in t, "accueil : %s" % t.replace("\n", " | ")[:300])
    a.js("D.SHEET.open({type: 'acc', id: arg});", c)
    a.wait(500)
    check(not a.pg.locator(".sheet .banner[role=status]").count(), "avertissement affiché sans changement")
    a.pg.click('.sheet [aria-label="Changer le signe"]')
    a.wait(300)
    w = a.pg.inner_text(".sheet .banner[role=status]") if a.pg.locator(".sheet .banner[role=status]").count() else ""
    check("passera de" in w and "Seul le signe change" in w, "pas d'avertissement en changeant le signe : %s" % w)
    a.no_errors("solde de départ")
    a.close()


@test
def icones_categories(br, url):
    """Icônes des catégories (maquette N) : choisies d'après le nom, modifiables, affichées partout en pictogrammes."""
    a = App(br, url)
    a.import_ic(FICTIF)
    got = a.js("return Object.fromEntries(D.cats().map(c => [c.name, D.catIc(c.id)]));")
    want = {"Salaire": "euro", "Loyer": "home", "Courses": "cart", "Loisirs": "ticket", "Cinéma": "ticket", "Assurance": "shield", "Divers": "dots", "Cantine": "utensils"}
    for k, v in want.items():
        check(got.get(k) == v, "icône de « %s » : %s (attendu %s)" % (k, got.get(k), v))
    for name, k in (("Prêt immobilier (Intérêt)", "trend"), ("Prêt immobilier (Assurance)", "shield"), ("Louis - Périscolaire", "child"), ("Natation Louis", "swim"),
                    ("Restaurants / Cafés", "coffee"), ("Elliott - Études", "grad"), ("Énergie", "bolt"), ("Cash / Retrait", "cash"), ("CAF", "people"), ("Appel de fonds", "building"),
                    ("Frais bancaires", "bank"), ("Noël", "tree"), ("Anniversaires", "cake"), ("Abonnement", "screen"), ("Transports", "train"), ("Scolarité ", "backpack"), ("Hovelacque", None)):
        g = a.js("return D.guessIc(arg);", name)
        check(g == k, "nom « %s » : %s (attendu %s)" % (name, g, k))
    # l'étape « Pour quoi ? » montre des pictogrammes
    a.pg.click('[aria-label="Saisir"]')
    a.wait(300)
    a.pg.click(".wiz >> text=Dépense")
    a.pg.click('.wiz button:text-is("5")')
    a.pg.click(".wgo")
    a.wait(300)
    check(a.pg.locator(".wiz .wbig .cic svg").count() >= 6, "pas de pictogrammes à l'étape « Pour quoi ? »")
    a.pg.keyboard.press("Escape")
    a.js("try { D.SHEET.close(); } catch (e) {}")
    # changer l'icône d'une catégorie
    cid = a.js("return D.cats().find(c => c.name === 'Divers').id;")
    a.js("D.SHEET.open({type: 'cat', id: arg});", cid)
    a.wait(400)
    a.pg.click('.sheet .icpick[aria-label="Cadeaux"]')
    a.pg.locator('.sheet button:has-text("Enregistrer")').first.click()
    a.wait(400)
    check(a.js("return D.catIc(arg);", cid) == "gift", "icône choisie non enregistrée")
    a.js("D.NAV.push({v: 'cats'});")
    a.wait(400)
    check(a.pg.locator("main .cic svg").count() >= 8, "Réglages › Catégories sans pictogrammes")
    a.no_errors("icônes")
    a.close()


def rest(a, cat, d="2026-11-15"):
    return a.js("const l = Object.values(D.A().bud).find(l => D.A().cat[l.cat]?.name === arg[0]); const x = D.lineAt(l, arg[1]); return x && x.rest;", [cat, d])


@test
def etat_prevue(br, url):
    """État « Prévue » choisi à la main : formulaire, modification groupée, rond de statut ; compté « à venir » dans les budgets, même total."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    lid = a.js("return Object.values(D.A().bud).find(l => D.A().cat[l.cat]?.name === 'Loisirs').id;")
    l = a.js("return D.A().bud[arg];", lid)
    acc = a.js("return D.A().bdg[arg].accounts[0];", l["budget"])
    tid = "tx_test_prevue"
    a.js("D.Store.put({k: 'tx', id: arg[0], account: arg[1], date: D.today(), name: 'Test prévue', amount: -25, splits: [{cat: arg[2], amount: -25}], st: 'saisie', pay: 'cb'});", [tid, acc, l["cat"]])
    a.wait(300)
    at = lambda: a.js("const x = D.lineAt(D.lineById(arg), D.today()); return [x.spent, x.plan, x.act];", lid)
    s0, p0, t0 = at()
    # formulaire : Plus d'options › Statut › Prévue
    a.js("D.SHEET.open({type: 'op', kind: 'tx', src: D.A().tx[arg]});", tid)
    a.wait(400)
    if pg.locator(".sheet .seg button", has_text="Prévue").count() == 0:
        pg.locator(".sheet button", has_text="Plus d'options").first.click()
        a.wait(300)
    pg.locator(".sheet .seg button", has_text="Prévue").first.click()
    pg.locator(".sheet .btn.pri").last.click()
    a.wait(500)
    check(a.js("return D.A().tx[arg].st;", tid) == "prevue", "formulaire : état %s" % a.js("return D.A().tx[arg].st;", tid))
    s1, p1, t1 = at()
    check(near(t1, t0) and near(s1, s0 - 25) and near(p1, p0 + 25), "budget : dépensé %s→%s, à venir %s→%s, total %s→%s" % (s0, s1, p0, p1, t0, t1))
    # la ligne s'affiche « prévue », et le rond la fait passer en créée
    a.js("D.NAV.push({v: 'acc', id: arg});", acc)
    a.wait(600)
    pg.locator(".seg button", has_text="Opérations").first.click()
    a.wait(500)
    row = pg.locator(".row", has_text="Test prévue").first
    check("prévue" in row.inner_text(), "ligne sans « prévue » : %s" % row.inner_text())
    row.locator('button.st[aria-label^="Statut : Prévue"]').click()
    a.wait(300)
    check(a.js("return D.A().tx[arg].st;", tid) == "saisie", "rond de statut : pas repassée en créée")
    # modification groupée
    a.js("D.SHEET.open({type: 'bulk', ids: [arg]});", tid)
    a.wait(400)
    pg.locator(".sheet .seg button", has_text="Prévue").first.click()
    pg.locator(".sheet .link", has_text="Appliquer").click()
    a.wait(400)
    check(a.js("return D.A().tx[arg].st;", tid) == "prevue", "modification groupée : état %s" % a.js("return D.A().tx[arg].st;", tid))
    a.no_errors("état prévue")
    a.close()


@test
def budgets_par_compte(br, url):
    """Budgets : menu « Choisir un compte » (le compte et ses cartes, ou tous les comptes), choix retenu."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    pg.evaluate("location.hash = '#budgets'")
    a.wait(800)
    info = a.js("return D.budgets(true).map(b => [b.name, (b.accounts || []).map(id => D.A().acc[id].name)]);")
    names = [n for n, _ in info]
    check(all(n in a.body() for n in names), "tous les budgets au départ : %s" % names)
    joint = a.js("return Object.values(D.A().acc).find(x => x.name === 'Joint').id;")
    on_joint = [n for n, accs in info if not accs or "Joint" in accs or any(x.startswith("Carte Joint") for x in accs)]
    pg.locator('button.accpill[aria-label*="Tous les comptes"]').click()
    a.wait(400)
    sheet = pg.locator(".sheet").inner_text()
    check("Tous les comptes" in sheet and "Prêt" not in sheet, "menu des comptes : %s" % sheet[:300])
    pg.locator(".sheet .row", has_text="Joint").first.click()
    a.wait(500)
    body = a.body()
    shown = [n for n in names if pg.locator(".benv-main", has_text=n).count()]
    check(sorted(shown) == sorted(on_joint) and len(shown) < len(names), "budgets du Joint : %s (attendu %s)" % (shown, on_joint))
    check(a.js("return D.S().bdgAcc;") == joint, "choix non retenu")
    a.no_errors("budgets du Joint")
    pg.reload()
    a.wait(1200)
    pg.evaluate("location.hash = '#budgets'")
    a.wait(800)
    check(pg.locator('button.accpill[aria-label*="Joint"]').count() == 1, "après réouverture, le Joint n'est plus choisi")
    pg.locator("button.accpill").first.click()
    a.wait(400)
    pg.locator(".sheet .row", has_text="Tous les comptes").first.click()
    a.wait(500)
    check(all(pg.locator(".benv-main", has_text=n).count() for n in names) and a.js("return D.S().bdgAcc;") is None, "retour à tous les comptes")
    a.no_errors("budgets de tous les comptes")
    a.close()


@test
def restant_mois_suivant(br, url):
    """Fin du restant « le 24 du mois suivant » : l'accueil montre le solde prévu ce jour-là (débit carte du 4 compris), choix par compte."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    joint = a.js("return Object.values(D.A().acc).find(x => x.name === 'Joint').id;")
    a.js("D.Store.put({...D.S(), k: 'set', id: 'set_main', mainAcc: arg});", joint)
    pg.evaluate("location.hash = '#accueil'")
    a.wait(700)
    proj = lambda d: a.js("return Math.round((D.balance(arg[0], {upTo: arg[1]}) + D.occurrences(arg[1]).reduce((s, o) => s + D.amtFor(o, arg[0]), 0)) * 100) / 100;", [joint, d])
    g0 = a.js("const g = D.glanceState(arg); return [g.pe, g.eomB];", joint)
    check(g0[0] == "2026-11-30" and near(g0[1], proj("2026-11-30")), "avant : %s" % g0)
    pg.locator('button[aria-label^="Restant calculé jusqu"]').click()
    a.wait(400)
    pg.locator("#rs-d").select_option("25")
    pg.locator(".sheet .seg button", has_text="Le mois suivant").click()
    a.wait(200)
    check("24 déc." in pg.locator(".sheet").inner_text(), "aperçu : %s" % pg.locator(".sheet").inner_text()[:300])
    pg.locator(".sheet .btn.pri", has_text="Enregistrer").click()
    a.wait(600)
    acc = a.js("return D.A().acc[arg];", joint)
    check(acc.get("restNext") is True and acc.get("resetDay") == 25, "réglage : %s / %s" % (acc.get("restNext"), acc.get("resetDay")))
    g1 = a.js("const g = D.glanceState(arg); return [g.pe, g.eomB];", joint)
    card_debit = a.js("return D.occurrences('2026-12-24').filter(o => o.date === '2026-12-04' && D.amtFor(o, arg) < 0 && o.card).length;", joint)
    check(g1[0] == "2026-12-24" and near(g1[1], proj("2026-12-24")) and card_debit >= 1, "après : %s (attendu %s), débits carte du 4 déc. : %s" % (g1, proj("2026-12-24"), card_debit))
    body = a.body()
    check("jusqu'au 24 déc." in body, "accueil : %s" % body[:300])
    # le 26 novembre, toujours le 24 décembre ; le 1er décembre, le 24 janvier
    check(a.js("return [D.restEndOf(D.A().acc[arg], '2026-11-26'), D.restEndOf(D.A().acc[arg], '2026-12-01')];", joint) == ["2026-12-24", "2027-01-24"], "dates suivantes")
    # les autres comptes ne changent pas
    other = a.js("return D.accList().find(x => x.type === 'courant' && x.id !== arg).id;", joint)
    check(a.js("return D.restEndOf(D.A().acc[arg]);", other) == "2026-11-30", "un autre compte a changé")
    a.no_errors("restant au mois suivant")
    a.close()


@test
def alerte_cinquante_cinquante(br, url):
    """Alerte 50/50 : la ligne de la partenaire change → bandeau sur l'accueil, détail, message prêt à copier ; « C'est envoyé » le range jusqu'au prochain changement."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    j = a.acc_id("Joint")
    name = a.js("return D.halfName();")
    check(a.js("return D.half50Due();") is None, "alerte sans dépense 50/50")
    add = lambda tid, nm, d, v: a.js("const [j, id, nm, d, v] = arg; D.Store.put({k: 'tx', id, account: j, date: d, name: nm, amount: v, splits: [{amount: v, cat: null}], st: 'saisie', half: true});", [j, tid, nm, d, v])
    add("tx_t50_resto", "Restaurant", "2026-11-12", -80)
    a.wait(500)
    pg.evaluate("location.hash = '#accueil'")
    a.wait(600)
    ban = pg.locator("button.h50b")
    check(ban.count() == 1 and ("%s · 50/50 de novembre : 40,00" % name) in ban.inner_text().replace("\u202f", " ").replace("\xa0", " "), "bandeau : %s" % (ban.inner_text() if ban.count() else "absent"))
    a.no_errors("bandeau 50/50")
    ban.click()
    a.wait(500)
    fixed = a.js("return D.half50('2026-11').fixed;")
    msg = pg.locator("#h50m").input_value().replace("\u202f", " ").replace("\xa0", " ")
    want0 = ("Joint · virement de novembre : %s €\ndont 50/50 : 40,00 €\n" % ("{:.2f}".format(fixed + 40).replace(".", ","))) if abs(fixed) > 0.004 else "Joint · 50/50 de novembre : 40,00 €\n"
    check(msg == want0 + "Restaurant 12 nov. : 40 €", "message : %r" % msg)
    pg.locator(".sheet .btn", has_text="C'est envoyé").click()
    a.wait(500)
    check(pg.locator("button.h50b").count() == 0 and a.js("return !!D.S().half50Sent['2026-11'];"), "après « C'est envoyé », le bandeau reste")
    # nouvelle dépense : le bandeau revient avec l'écart et ce qui a changé
    add("tx_t50_cine", "Cinéma", "2026-11-20", -50)
    a.wait(500)
    t = pg.locator("button.h50b").inner_text().replace("\u202f", " ").replace("\xa0", " ")
    check("65,00" in t and "+25,00" in t and "Dépense ajoutée : Cinéma 20 nov." in t, "bandeau après ajout : %s" % t)
    pg.locator("button.h50b").click()
    a.wait(500)
    sh = pg.locator(".sheet").inner_text().replace("\u202f", " ").replace("\xa0", " ")
    check("Dernier envoi" in sh and "40,00" in sh and "Nouveau" in sh, "détail : %s" % sh[:300])
    msg = pg.locator("#h50m").input_value().replace("\u202f", " ").replace("\xa0", " ")
    check(msg.endswith("Restaurant 12 nov. : 40 €\nCinéma 20 nov. : 25 €") and ("65,00 €" in msg), "message après ajout : %r" % msg)
    # copier : le presse-papiers reçoit le texte (modifié à la main)
    pg.context.grant_permissions(["clipboard-read", "clipboard-write"])
    pg.locator("#h50m").fill("Coucou !\n" + msg)
    pg.locator(".sheet .btn.pri", has_text="Copier le message").click()
    a.wait(400)
    clip = pg.evaluate("navigator.clipboard.readText()")
    check(clip.startswith("Coucou !") and "Cinéma 20 nov." in clip, "presse-papiers : %r" % clip[:80])
    a.js("D.SHEET.close();")
    a.wait(300)
    check(pg.locator("button.h50b").count() == 1, "« Copier » ne doit pas ranger le bandeau")
    # dépense décochée : signalée comme retirée
    a.js("D.Store.put({...D.A().tx.tx_t50_resto, half: false});")
    a.wait(500)
    t = pg.locator("button.h50b").inner_text()
    check("Dépense retirée : Restaurant 12 nov." in t and "Dépense ajoutée : Cinéma" in t, "bandeau après retrait : %s" % t)
    # ligne de la partenaire validée : plus d'alerte pour ce mois
    m = a.js("const {M} = D.halfBase('2026-11'); return M ? M.id : null;")
    if m:
        a.js("D.Store.put({...D.A().tx[arg], st: 'pointee'});", m)
        a.wait(500)
        check(pg.locator("button.h50b").count() == 0, "alerte alors que la ligne est validée")
    a.no_errors("alerte 50/50")
    a.close()


@test
def previsions_variation(br, url):
    """Prévisions : sous le montant, l'écart avec la barre d'avant (entourée), ou avec aujourd'hui pour la première."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    pg.evaluate("location.hash = '#prev'")
    a.wait(800)
    num = lambda s: float(re.sub(r"[^0-9,−-]", "", s.split(":")[-1]).replace("−", "-").replace(",", "."))
    bars = pg.locator("main section.card button[aria-pressed]")
    n = bars.count()
    check(n >= 12, "barres : %d" % n)
    def line():
        return pg.locator(".pvar").inner_text().replace("\u202f", " ").replace("\xa0", " ")
    for i in (3, 1):
        bars.nth(i).click()
        a.wait(300)
        v, w = num(bars.nth(i).get_attribute("aria-label")), num(bars.nth(i - 1).get_attribute("aria-label"))
        d = round(v - w, 2)
        txt = line()
        want = ("+" if d > 0 else "−") + "{:,.2f}".format(abs(d)).replace(",", " ").replace(".", ",")
        prev = bars.nth(i - 1).get_attribute("aria-label").split(" : ")[0]
        check(want in txt and ("par rapport au " + prev) in txt, "barre %d : %r (attendu %s par rapport au %s)" % (i, txt, want, prev))
        check(pg.locator(".pring").count() == 1, "barre d'avant non entourée")
    bars.nth(0).click()
    a.wait(300)
    check("aujourd'hui" in line() and pg.locator(".pring").count() == 0, "première barre : %r" % line())
    a.no_errors("prévisions, écart entre deux mois")
    a.close()


@test
def reglages_clarte(br, url):
    """Réglages (passage clarté) : carte d'état en haut, Organisation / Partage / Données, menus Compte joint et Importer."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    a.js("D.NAV.push({v: 'settings'});")
    a.wait(600)
    main = pg.inner_text("main")
    for h in ["Organisation", "Partage", "Données", "Compte joint", "Sauvegarde", "Importer", "Préférences", "Guide"]:
        check(h.lower() in main.lower(), "Réglages : « %s » absent" % h)
    nh = a.js("return D.healthChecks().length;")
    st = pg.locator("button.st-card").inner_text()
    check(("à regarder" in st) if nh else ("Sauvegarde à faire" in st), "carte d'état (%d points) : %r" % (nh, st))
    # sauvegarde récente et rien à regarder : tout est en ordre
    a.js("D.Store.put({...D.S(), k: 'set', id: 'set_main', lastExport: D.today(), hIgnore: D.healthChecks().map(c => D.hKey(c))});")
    a.wait(400)
    if a.js("return D.healthChecks().length;") == 0:
        check("Tout est en ordre" in pg.locator("button.st-card").inner_text(), "tout est en ordre : %r" % pg.locator("button.st-card").inner_text())
    pg.locator("main button.row", has_text="Compte joint").click()
    a.wait(400)
    sh = pg.inner_text(".sheet")
    check("Remboursement 50/50" in sh and "Au prorata des salaires" in sh, "menu Compte joint : %r" % sh[:200])
    pg.locator(".sheet button.row", has_text="Remboursement 50/50").click()
    a.wait(500)
    check(pg.locator(".sheet").count() >= 1 and "50/50" in pg.inner_text(".sheet"), "fiche 50/50 non ouverte")
    a.js("D.SHEET.close();")
    a.wait(300)
    pg.locator("main button.row", has_text="Importer").click()
    a.wait(400)
    sh = pg.inner_text(".sheet")
    check("Depuis iCompta" in sh and "Fichier de ta banque" in sh, "menu Importer : %r" % sh[:200])
    a.no_errors("réglages")
    a.close()


@test
def accueil_debit_cartes(br, url):
    """Accueil (maquette K) : solde validé puis une tuile par carte du compte (son débit différé, sa date) ; toucher une carte l'ouvre."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    norm = lambda x: x.replace("\u202f", " ").replace("\xa0", " ")
    a.pick("Joint")
    j = a.acc_id("Joint")
    want = a.js("return D.kidsOf(D.accList(), arg).filter(c => c.type === 'carte').map(c => [c.name, (D.cardNext(c) || {}).amt || 0, (D.cardNext(c) || {}).date]);", j)
    tiles = pg.locator("button.ktile")
    check(tiles.count() == len(want) == 2, "tuiles de cartes du Joint : %d (attendu %d)" % (tiles.count(), len(want)))
    for i, (nm, amt, d) in enumerate(want):
        t = norm(tiles.nth(i).inner_text())
        sv = "−" + "{:,.2f}".format(amt).replace(",", " ").replace(".", ",")
        check(sv in t and "débit le" in t and nm in t, "tuile %s : %r (attendu %s)" % (nm, t, sv))
    check("Solde validé" in pg.inner_text("main section") and "rentrée" not in pg.inner_text("main section").lower(), "solde validé absent ou rentrée affichée")
    cc = a.acc_id("Carte Claire")
    a.js("D.Store.put({...D.A().acc[arg], name: 'CB 3241'});", cc)
    a.wait(400)
    check("CB 3241" in pg.locator("button.ktile").nth(1).inner_text(), "nom de carte raccourci : %r" % pg.locator("button.ktile").nth(1).inner_text())
    a.js("D.Store.put({...D.A().acc[arg], name: 'Carte Claire'});", cc)
    a.wait(400)
    pg.locator('button.ktile[aria-label^="Carte Claire"]').click()
    a.wait(500)
    check(a.js("return D.A().acc[D.S().mainAcc].name;") == "Carte Claire", "toucher la tuile n'ouvre pas la carte")
    # un compte sans carte : une seule tuile, le solde validé
    liv = a.js("return D.accList().find(x => x.type !== 'carte' && x.type !== 'pret' && !D.kidsOf(D.accList(), x.id).some(k => k.type === 'carte')).name;")
    a.pick(liv)
    check(pg.locator("button.ktile").count() == 0 and "Solde validé" in pg.inner_text("main section"), "compte sans carte (%s)" % liv)
    a.no_errors("accueil, une tuile par carte")
    a.close()


@test
def echeance_date_de_fin(br, url):
    """Date de fin d'une échéance : plus aucune occurrence après (prévisions, prochaine date, terminée) ; visible dans l'échéancier ; au prorata, une échéance ne compte ni avant son début (mensuelle, annuelle lissée, ancienne échéance jamais passée) ni après sa fin."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    j, cour = a.acc_id("Joint"), a.acc_id("Courant")
    a.js("""D.Store.put({k: 'sch', id: 'sch_t_fin', account: arg, name: 'Cours de piano', amount: -50, splits: [{amount: -50, cat: null}], freq: {n: 1, u: 'm'}, next: '2026-12-05', anchor: 5, start: '2026-12-05', end: '2027-02-05', left: null, auto: false, paused: false, skips: [], pay: 'prelevement'});""", cour)
    a.wait(300)
    occ = a.js("return D.occurrences('2027-12-31').filter(o => o.name === 'Cours de piano').map(o => o.date);")
    check(occ == ["2026-12-05", "2027-01-05", "2027-02-05"], "occurrences après la fin : %s" % occ)
    a.js("D.Store.put({...D.A().sch.sch_t_fin, next: '2027-03-05'});"); a.wait(200)
    check(a.js("return D.nextPending(D.A().sch.sch_t_fin);") is None, "prochaine date après la fin")
    a.js("D.Store.put({...D.A().sch.sch_t_fin, next: '2026-12-05'});"); a.wait(200)
    a.js("D.NAV.tab('echeancier')"); a.wait(400)
    a.pick("Courant")
    row = pg.locator("main button.row", has_text="Cours de piano").first
    check(row.count() == 1 and "jusqu'au 5 févr. 2027" in row.inner_text(), "fin absente de la ligne : %r" % (row.inner_text() if row.count() else None))
    row.click(); a.wait(400)
    check("Jusqu'au 5 févr. 2027" in pg.locator(".sheet").inner_text(), "fin absente de la fiche")
    a.js("D.SHEET.close()"); a.wait(200)
    # prorata : échéance du joint de janvier à mars 2027
    a.js("D.NAV.push({v: 'joint'});"); a.wait(400)
    pg.click('main button:has-text("Configurer")'); a.wait(400)
    sh = pg.locator(".sheet")
    sh.locator("#pr-f").select_option(cour); sh.locator("#pr-n").fill("Claire"); sh.locator("#pr-m").fill("3000"); sh.locator("#pr-p").fill("2000")
    sh.locator('button:has-text("Enregistrer")').last.click(); a.wait(500)
    a.js("""D.Store.put({k: 'sch', id: 'sch_t_pr', account: arg, name: 'Travaux', amount: -300, splits: [{amount: -300, cat: null}], freq: {n: 1, u: 'm'}, next: '2027-01-10', anchor: 10, start: '2027-01-10', end: '2027-03-10', left: null, auto: false, paused: false, skips: [], prorata: true, pay: 'prelevement'});""", j)
    a.wait(300)
    has = lambda k: a.js("return D.prItems(arg).some(x => x.id === 'sch_t_pr');", k)
    check(not has("2026-12") and has("2027-01") and has("2027-03") and not has("2027-04"), "prorata hors période : déc %s janv %s mars %s avr %s" % (has("2026-12"), has("2027-01"), has("2027-03"), has("2027-04")))
    # annuelle qui commence en juin : lissée à partir de juin seulement ; mensuelle créée sans date de début et jamais passée : à partir de sa 1re date
    a.js("""D.Store.put({...D.A().sch.sch_t_pr, id: 'sch_t_an', name: 'Assurance', amount: -120, splits: [{amount: -120, cat: null}], freq: {n: 1, u: 'y'}, next: '2027-06-05', anchor: 5, start: '2027-06-05', end: null});
      D.Store.put({...D.A().sch.sch_t_pr, id: 'sch_t_old', name: 'Ancienne', next: '2027-06-05', anchor: 5, start: undefined, end: null});""")
    a.wait(300)
    has = lambda k, sid: a.js("return D.prItems(arg[0]).some(x => x.id === arg[1]);", [k, sid])
    check(not has("2027-05", "sch_t_an") and has("2027-06", "sch_t_an") and has("2027-09", "sch_t_an"), "annuelle lissée avant son début")
    check(not has("2026-12", "sch_t_old") and not has("2027-05", "sch_t_old") and has("2027-06", "sch_t_old"), "ancienne échéance comptée avant sa 1re date")
    a.no_errors("date de fin")
    a.close()


@test
def echeance_sauf_mois(br, url):
    """« Sauf en » : une échéance mensuelle qui saute août chaque année (Navigo) ; réglée dans Modifier ; aucune occurrence en août (prévisions, prochaine date après validation) ; ligne et fiche de l'échéancier ; au prorata, août compte 0 €."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    j, cour = a.acc_id("Joint"), a.acc_id("Courant")
    a.js("""D.Store.put({k: 'sch', id: 'sch_t_nav', account: arg, name: 'Navigo', amount: -88.8, splits: [{amount: -88.8, cat: null}], freq: {n: 1, u: 'm'}, next: '2026-12-05', anchor: 5, start: '2026-12-05', end: null, left: null, auto: false, paused: false, skips: [], pay: 'prelevement'});""", cour)
    a.wait(300)
    a.js("D.SHEET.open({type: 'op', kind: 'sch', src: D.A().sch.sch_t_nav});"); a.wait(500)
    blk = pg.locator(".sheet .offm")
    check(blk.count() == 1 and "Sauf en" in blk.inner_text(), "bloc « Sauf en » absent")
    blk.locator('button[aria-label="Sauter août"]').click(); a.wait(200)
    check("11 mois sur 12" in blk.inner_text() and "Pas de prélèvement en août" in blk.inner_text(), "bloc après le choix : %r" % blk.inner_text())
    pg.locator('.sheet button:has-text("Enregistrer")').last.click(); a.wait(500)
    check(a.js("return D.A().sch.sch_t_nav.offM;") == [8], "mois sautés non enregistrés")
    check(pg.locator(".undobar").count() == 0 and "Annuler" not in pg.locator('[role="status"]').all_inner_texts().__str__(), "bandeau « Annuler » affiché après une modification")
    check(a.js("return D.Hist.list.length > 0;"), "modification absente de l'historique des annulations")
    occ = a.js("return D.occurrences('2027-12-31').filter(o => o.name === 'Navigo').map(o => o.date);")
    check(len(occ) == 12 and "2027-08-05" not in occ and "2027-07-05" in occ and "2027-09-05" in occ, "occurrences : %s" % occ)
    a.js("D.NAV.tab('echeancier')"); a.wait(400)
    a.pick("Courant")
    row = pg.locator("main button.row", has_text="Navigo").first
    check("chaque mois sauf août" in row.inner_text(), "ligne : %r" % row.inner_text())
    check("Navigo compté pour 11 mois sur 12" in pg.locator("main").inner_text(), "total du mois sans la mention 11/12")
    row.click(); a.wait(400)
    sh = pg.locator(".sheet")
    check("de chaque mois, sauf août" in sh.inner_text() and sh.locator(".ystrip .c.off").count() == 1 and sh.locator(".ystrip .c").count() == 12, "fiche : %r" % sh.inner_text()[:200])
    a.js("D.SHEET.close()"); a.wait(200)
    # valider juillet : la prochaine date passe directement à septembre
    a.js("D.Store.put({...D.A().sch.sch_t_nav, next: '2027-07-05'});"); a.wait(200)
    a.js("D.Store.put({k: 'tx', id: 'tx_s_sch_t_nav_2027-07-05', account: arg, date: '2027-07-05', name: 'Navigo', amount: -88.8, splits: [{amount: -88.8, cat: null}], st: 'saisie'});", cour); a.wait(200)
    check(a.js("return D.nextPending(D.A().sch.sch_t_nav);") == "2027-09-05", "prochaine date après juillet : %s" % a.js("return D.nextPending(D.A().sch.sch_t_nav);"))
    # prorata : une échéance du joint sauf août compte 0 € en août
    a.js("D.NAV.push({v: 'joint'});"); a.wait(400)
    pg.click('main button:has-text("Configurer")'); a.wait(400)
    sh = pg.locator(".sheet")
    sh.locator("#pr-f").select_option(cour); sh.locator("#pr-n").fill("Claire"); sh.locator("#pr-m").fill("3000"); sh.locator("#pr-p").fill("2000")
    sh.locator('button:has-text("Enregistrer")').last.click(); a.wait(500)
    a.js("D.Store.put({...D.A().sch.sch_t_nav, id: 'sch_t_navj', account: arg, next: '2026-12-05', prorata: true});", j); a.wait(300)
    has = lambda k: a.js("return D.prItems(arg).some(x => x.id === 'sch_t_navj');", k)
    check(has("2027-07") and not has("2027-08") and has("2027-09"), "prorata en août : juil %s août %s sept %s" % (has("2027-07"), has("2027-08"), has("2027-09")))
    a.no_errors("sauf en")
    a.close()


@test
def barre_du_bas_apres_saisie(br, url):
    """Après une saisie validée, la barre du bas est entière ; si iOS laisse la page plus haute que l'écran (bogue après le clavier), la barre remonte d'autant ; clavier ouvert : rien ne bouge."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    a.pick("Courant")
    pg.click('[aria-label="Saisir"]'); a.wait(300)
    pg.click(".wiz >> text=Dépense"); a.wait(300)
    for k in "12": pg.click('.wiz button:text-is("%s")' % k)
    for _ in range(6):
        if pg.locator(".sheet").count() == 0: break
        b = pg.locator('.wgo').last
        if b.count() and b.is_enabled(): b.click()
        else: pg.locator(".wiz .wbig").first.click()
        a.wait(300)
    check(pg.locator(".sheet").count() == 0, "la saisie ne s'est pas fermée")
    a.wait(800)
    vh = a.js("return window.visualViewport.height;")
    box = pg.locator(".tabbar").bounding_box()
    check(abs(box["y"] + box["height"] - vh) < 1.5, "barre du bas coupée après la saisie : %s / %s" % (box, vh))
    a.js("Object.defineProperty(window, 'innerHeight', {value: window.visualViewport.height + 40, configurable: true}); D.fitViewport();"); a.wait(300)
    box = pg.locator(".tabbar").bounding_box()
    check(a.js("return getComputedStyle(document.documentElement).getPropertyValue('--vvb').trim();") == "40px" and abs(box["y"] + box["height"] - (vh - 40)) < 1.5, "barre non recalée sur la partie visible : %s" % box)
    a.js("Object.defineProperty(window, 'innerHeight', {value: window.visualViewport.height + 300, configurable: true}); D.fitViewport();"); a.wait(300)
    check(a.js("return getComputedStyle(document.documentElement).getPropertyValue('--vvb').trim();") == "0px", "barre déplacée alors que le clavier est ouvert")
    a.js("delete window.innerHeight; D.fitViewport();"); a.wait(300)
    a.no_errors("barre du bas")
    a.close()


@test
def mise_a_jour(br, url):
    """Mise à jour : numéro identique dans l'app et sw.js ; « Rechercher » quand tout est à jour ; bandeau sur l'accueil et ligne des Réglages quand une version plus récente est en ligne ; « Mettre à jour » recharge l'app ; fenêtre « Nouveautés » une seule fois."""
    import re as _re
    sw = open(os.path.join(ROOT, "sw.js")).read()
    a = App(br, url)
    pg = a.pg
    ver = a.js("return D.APP_VERSION;")
    check(("pecule-v%d" % ver) in sw, "APP_VERSION %s différent de sw.js" % ver)
    a.import_ic(FICTIF)
    a.pick("Joint")
    a.wait(2600)
    check(pg.locator(".updb").count() == 0, "bandeau affiché sans nouvelle version")
    a.js("D.NAV.push({v: 'settings'})"); a.wait(500)
    check("Version %d" % ver in pg.locator(".upd-row").inner_text(), "ligne Mise à jour : %r" % pg.locator(".upd-row").inner_text())
    pg.locator(".upd-row .updr").click(); a.wait(800)
    check("Pécule est à jour (version %d)" % ver in a.body(), "pas de confirmation « à jour »")
    pg.route(_re.compile(r".*/sw\.js\?check=.*"), lambda r: r.fulfill(status=200, content_type="text/javascript", body='const CACHE = "pecule-v999";'))
    a.js("return D.updCheck(false);"); a.wait(500)
    t = pg.locator(".upd-row").inner_text()
    check("Version 999 disponible" in t and "Mettre à jour" in t, "nouvelle version dans Réglages : %r" % t)
    a.js("D.NAV.tab('comptes')"); a.wait(500)
    check(pg.locator("main .updb").count() == 1 and "Mettre à jour" in pg.locator("main .updb").inner_text(), "bandeau absent")
    check(pg.locator("main .updb").bounding_box()["y"] < pg.locator("main .rblock").bounding_box()["y"], "bandeau pas au-dessus du restant")
    pg.locator('.updb-x').click(); a.wait(300)
    check(pg.locator("main .updb").count() == 0, "« Plus tard » ne cache pas le bandeau")
    a.js("window.__avant = 1; D.UPD.hide = false; D.NAV.push({v: 'settings'})"); a.wait(400)
    pg.locator(".upd-row .updr").click(); a.wait(2500)
    check(a.js("return window.__avant === undefined;"), "« Mettre à jour » n'a pas rechargé l'app")
    pg.unroute(_re.compile(r".*/sw\.js\?check=.*"))
    check(pg.locator(".sheet").count() == 0, "fenêtre ouverte après le rechargement sans changement de version")
    a.js("localStorage.setItem('pecule-ver', String(arg - 1));", ver)
    pg.reload(); a.wait(1800)
    check(pg.locator('.sheet[aria-label="Pécule est à jour"]').count() == 1 and "Nouveautés" in a.body(), "fenêtre Nouveautés absente")
    pg.locator('.sheet .btn.pri', has_text="OK").click(); a.wait(400)
    check(pg.locator(".sheet").count() == 0, "OK ne ferme pas les nouveautés")
    pg.reload(); a.wait(1800)
    check(pg.locator(".sheet").count() == 0, "nouveautés montrées deux fois")
    a.no_errors("mise à jour")
    a.close()


@test
def design_enveloppes(br, url):
    """Maquette R : restant dans un bloc jaune, tuiles et budgets pastel, Prévisions dans le bloc jaune ; en clair et en sombre, le texte des boutons principaux reste lisible."""
    for dark in (False, True):
        a = App(br, url, dark=dark)
        a.import_ic(FICTIF)
        pg = a.pg
        a.pick("Joint")
        blk = pg.locator("main .rblock").first
        check(blk.count() == 1 and "Tu peux encore dépenser" in blk.inner_text(), "bloc du restant absent")
        check(pg.locator("main .ktile.pc").count() == 3, "tuiles pastel : %d" % pg.locator("main .ktile.pc").count())
        check(pg.locator("main .bpill.pc").count() >= 1, "budgets de l'accueil sans couleur")
        a.js("D.NAV.tab('budgets')"); a.wait(600)
        check(pg.locator(".benv.pc").count() == pg.locator(".benv-main").count() > 0, "enveloppes pastel")
        a.js("D.NAV.tab('prev')"); a.wait(600)
        check(pg.locator("main section.card.rblock button[aria-pressed]").count() > 0, "prévisions hors du bloc jaune")
        bg, fg = a.js("const b = document.querySelector('.tab-plus button'), c = getComputedStyle(b); return [c.backgroundColor, c.color];")
        check(bg != fg, "bouton + illisible (%s sur %s)" % (fg, bg))
        a.no_errors("design R" + (" sombre" if dark else ""))
        a.close()


@test
def enveloppe_datee(br, url):
    """Enveloppe datée (option A) : une ligne passée en « Date de dépense » ; ce qui reste est retiré à cette date dans les prévisions, une dépense rattachée s'en déduit (les 500 € ne comptent qu'une fois) ; une fois la période finie, le reste est réservé et Pécule demande quoi en faire : garder pour l'an prochain, mettre sur une autre enveloppe."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    liv = a.acc_id("Livret")
    norm = lambda x: x.replace("\u202f", " ").replace("\xa0", " ")
    a.js("""return D.Store.bulk([['put', {k: 'cat', id: 'cat_t_a', name: 'Ski', type: 'dep', parent: null}], ['put', {k: 'cat', id: 'cat_t_b', name: 'Cadeaux test', type: 'dep', parent: null}], ['put', {k: 'cat', id: 'cat_t_c', name: 'Fleurs test', type: 'dep', parent: null}],
      ['put', {k: 'bdg', id: 'bdg_t_v', name: 'Projets', accounts: [arg], remaining: true, remAcc: arg, hidden: false, order: 30}],
      ['put', {k: 'bud', id: 'bud_t_a', budget: 'bdg_t_v', cat: 'cat_t_a', amount: 500, income: false, start: D.today().slice(0, 8) + '01', end: null, dur: {n: 1, u: 'm'}, carry: false, hist: []}],
      ['put', {...D.S(), k: 'set', id: 'set_main', bdgAcc: arg}]]);""", liv); a.wait(400)
    due = a.js("return D.addDays(D.today(), 60);")
    # la fiche de la ligne : « Date de dépense », chaque année
    a.js("D.SHEET.open({type: 'line', id: 'bud_t_a'})"); a.wait(400)
    sh = pg.locator(".sheet").last
    sh.locator(".seg button", has_text="Date de dépense").click(); a.wait(200)
    check("Montant par an" in sh.inner_text() and sh.locator("#ln-n").count() == 0, "fiche : mode date de dépense mal affiché")
    sh.locator("#ln-d").fill(due); a.wait(200)
    sh.locator(".btn.pri", has_text="Enregistrer").click(); a.wait(600)
    l = a.js("return D.A().bud.bud_t_a;")
    check(l["first"] == due and l["dur"]["u"] == "y" and not l["once"] and l["dueSince"] == a.js("return D.today();") and l["start"] == a.js("return D.dueStart(arg, false);", due), "ligne datée : %s" % l)
    env = lambda: a.js("return D.occurrences(D.addDays(arg, 5)).filter(o => o.kind === 'bud' && o.line === 'bud_t_a').map(o => [o.date, o.amount, o.name]);", due)
    e0 = env()
    check(len(e0) == 1 and e0[0][0] == due and e0[0][1] == -500 and "(enveloppe)" in e0[0][2], "reste retiré à la date : %s" % e0)
    bal = lambda: a.js("const s = D.dailySeries([arg], D.today(), D.addDays(D.today(), 70)); return s.find(p => p[0] === D.addDays(D.today(), 60))?.[1];", liv)
    b0 = bal()
    a.js("return D.Store.put({k: 'tx', id: 'tx_t_loc', account: arg, date: D.addDays(D.today(), 50), name: 'Location test', amount: -200, splits: [{amount: -200, cat: 'cat_t_a'}], st: 'prevue', pay: 'virement'});", liv); a.wait(400)
    e1 = env()
    check(len(e1) == 1 and e1[0][1] == -300, "la dépense rattachée doit se déduire de l'enveloppe : %s" % e1)
    check(abs(bal() - b0) < 0.01, "les 500 € doivent compter une seule fois : %s puis %s" % (b0, bal()))
    a.js("D.NAV.tab('budgets')"); a.wait(600)
    check("le " in pg.locator("main .bwide", has_text="Projets").first.inner_text(), "date absente de la carte du budget")
    # deux enveloppes dont la période est finie, avec un reste
    a.js("""const t = D.today(), f = D.addDays(t, -40), ops = [];
      for (const [id, c, amt, sp] of [['b', 'cat_t_b', 500, 380], ['c', 'cat_t_c', 100, 30]]) {
        ops.push(['put', {k: 'bud', id: 'bud_t_' + id, budget: 'bdg_t_v', cat: c, amount: amt, income: false, first: f, once: false, start: D.dueStart(f, false), end: null, dur: {n: 1, u: 'y'}, carry: false, hist: [], dueSince: D.addDays(t, -400)}]);
        ops.push(['put', {k: 'tx', id: 'tx_t_' + id, account: arg, date: D.addDays(f, -3), name: 'Achat ' + id, amount: -sp, splits: [{amount: -sp, cat: c}], st: 'pointee', pay: 'cb'}]); }
      return D.Store.bulk(ops);""", liv); a.wait(500)
    asks = a.js("return D.dueAsks().map(q => [q.l.id, q.rem]);")
    check(sorted(asks) == [["bud_t_b", 120], ["bud_t_c", 70]], "restes à décider : %s" % asks)
    pend = a.js("return D.occurrences(D.addDays(D.today(), 5)).filter(o => o.kind === 'bud' && /à décider/.test(o.name)).map(o => [o.line, o.date === D.today(), o.amount]);")
    check(sorted(pend) == [["bud_t_b", True, -120], ["bud_t_c", True, -70]], "restes réservés dès aujourd'hui : %s" % pend)
    a.js("D.NAV.tab('comptes')"); a.wait(200); a.js("D.NAV.tab('budgets')"); a.wait(600)
    ban = pg.locator(".prow", has_text="Cadeaux test")
    check(ban.count() == 1 and "120 € non dépensés" in norm(ban.inner_text()), "bandeau du reste absent : %s" % [norm(x) for x in pg.locator(".prow").all_inner_texts()])
    ban.click(); a.wait(500)
    sh = pg.locator(".sheet").last
    check(sh.locator('[role=radio][aria-checked=true]', has_text="Les remettre sur le").count() == 1, "le choix par défaut doit être de rendre l'argent au compte")
    sh.locator('[role=radio]', has_text="Les garder pour l'an prochain").click(); a.wait(200)
    sh.locator(".btn.pri", has_text="Valider").click(); a.wait(600)
    st = a.js("const l = D.lineById('bud_t_b'), x = D.lineAt(l, D.today()); return {done: l.done, mv: l.mv, bud: x.bud, ps: x.ps};")
    check(list(st["done"].values()) == ["keep"] and st["mv"] == {st["ps"]: 120} and st["bud"] == 620, "garder pour l'an prochain : %s" % st)
    pg.locator(".prow", has_text="Fleurs test").click(); a.wait(500)
    sh = pg.locator(".sheet").last
    sh.locator('[role=radio]', has_text="Les mettre sur une autre enveloppe").click(); a.wait(200)
    sh.locator("#rs-to").select_option("bud_t_b"); a.wait(100)
    sh.locator(".btn.pri", has_text="Valider").click(); a.wait(600)
    st2 = a.js("const b = D.lineById('bud_t_b'), c = D.lineById('bud_t_c'); return {bud: D.lineAt(b, D.today()).bud, done: Object.values(c.done || {}), asks: D.dueAsks().length};")
    check(st2 == {"bud": 690, "done": ["move:bud_t_b"], "asks": 0}, "mettre sur une autre enveloppe : %s" % st2)
    check(pg.locator(".prow", has_text="non dépensés").count() == 0, "bandeau encore affiché après la décision")
    a.no_errors("enveloppe datée")
    a.close()


@test
def budget_annuel_date(br, url):
    """Budget annuel sans date : la vignette propose « Ajouter une date » (pas les budgets mensuels) ; la fiche part de la fin de la période, enregistre la date (chaque année), le reste est retiré à cette date et le budget s'affiche en ligne avec sa puce ; « Une seule fois » refuse une date passée."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    liv = a.acc_id("Livret")
    norm = lambda x: x.replace("\u202f", " ").replace("\xa0", " ")
    a.js("""const t = D.today(), m0 = D.addDays(t.slice(0, 8) + '01', -40).slice(0, 8) + '01', ops = [['put', {...D.S(), k: 'set', id: 'set_main', bdgAcc: arg}]];
      [['a', 'Cadeaux annuels', 300, 'y'], ['b', 'Sorties annuelles', 150, 'y'], ['c', 'Courses test', 400, 'm']].forEach(([id, n, amt, u], i) => {
        ops.push(['put', {k: 'cat', id: 'cat_y_' + id, name: n + ' cat', type: 'dep', parent: null}]);
        ops.push(['put', {k: 'bdg', id: 'bdg_y_' + id, name: n, accounts: [arg], remaining: true, remAcc: arg, hidden: false, order: 60 + i}]);
        ops.push(['put', {k: 'bud', id: 'bud_y_' + id, budget: 'bdg_y_' + id, cat: 'cat_y_' + id, amount: amt, income: false, start: m0, end: null, dur: {n: 1, u}, carry: false, hist: []}]); });
      return D.Store.bulk(ops);""", liv); a.wait(500)
    a.js("D.NAV.tab('budgets')"); a.wait(600)
    btn = lambda n: pg.locator('[aria-label="Ajouter une date à %s"]' % n)
    check(btn("Cadeaux annuels").count() == 1 and btn("Sorties annuelles").count() == 1, "bouton « Ajouter une date » absent des budgets annuels")
    check(btn("Courses test").count() == 0, "un budget mensuel ne doit pas proposer de date")
    btn("Cadeaux annuels").click(); a.wait(500)
    sh = pg.locator(".sheet").last
    pe = a.js("return D.lineAt(D.lineById('bud_y_a'), D.today()).pe;")
    check(sh.locator("#du-d").input_value() == pe, "la date doit partir de la fin de la période : %s / %s" % (sh.locator("#du-d").input_value(), pe))
    check("300 € par an" in norm(sh.inner_text()), "fiche : montant par an absent")
    due = a.js("return D.addDays(D.today(), 40);")
    sh.locator("#du-d").fill(due); a.wait(200)
    sh.locator(".btn.pri", has_text="Enregistrer").click(); a.wait(600)
    l = a.js("return D.A().bud.bud_y_a;")
    check(l["first"] == due and not l["once"] and l["dur"] == {"n": 1, "u": "y"} and not l["carry"] and l["dueSince"] == a.js("return D.today();") and l["start"] == a.js("return D.dueStart(arg, false);", due), "ligne datée : %s" % l)
    occ = a.js("return D.occurrences(D.addDays(arg, 5)).filter(o => o.kind === 'bud' && o.line === 'bud_y_a').map(o => [o.date, o.amount]);", due)
    check(occ == [[due, -300]], "reste retiré à la date : %s" % occ)
    check(btn("Cadeaux annuels").count() == 0, "le bouton doit disparaître une fois la date choisie")
    card = pg.locator("main .bwide", has_text="Cadeaux annuels")
    check(card.count() == 1 and card.locator(".bw-one .bw-chip").count() == 1 and "sur 300 €" in norm(card.inner_text()), "carte en ligne avec puce absente : %s" % [norm(x) for x in card.all_inner_texts()])
    # « Une seule fois » : pas de date passée
    btn("Sorties annuelles").click(); a.wait(500)
    sh = pg.locator(".sheet").last
    sh.locator(".seg button", has_text="Une seule fois").click(); a.wait(150)
    sh.locator("#du-d").fill(a.js("return D.addDays(D.today().slice(0, 8) + '01', -20);")); a.wait(150)
    sh.locator(".btn.pri", has_text="Enregistrer").click(); a.wait(300)
    check("Choisis une date à venir" in sh.inner_text(), "date passée acceptée en « Une seule fois »")
    check(not a.js("return D.A().bud.bud_y_b.first;"), "la ligne ne doit pas changer après une erreur")
    a.no_errors("budget annuel daté")
    a.close()


@test
def budgets_ordre(br, url):
    """Budgets › « Réorganiser » : ↑/↓ change l'ordre des vignettes parmi celles du même compte, et l'ordre est gardé."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    liv = a.acc_id("Livret")
    a.js("""const ops = []; ['Alpha', 'Bravo', 'Charlie'].forEach((n, i) => { ops.push(['put', {k: 'bdg', id: 'bdg_o_' + i, name: n, accounts: [arg], remaining: true, hidden: false, order: 50 + i}]);
      ops.push(['put', {k: 'bud', id: 'bud_o_' + i, budget: 'bdg_o_' + i, cat: null, amount: 100, income: false, start: D.today().slice(0, 8) + '01', end: null, dur: {n: 1, u: 'm'}, carry: false, hist: []}]); });
      ops.push(['put', {...D.S(), k: 'set', id: 'set_main', bdgAcc: arg}]); return D.Store.bulk(ops);""", liv); a.wait(500)
    a.js("D.NAV.tab('budgets')"); a.wait(600)
    names = lambda: [x for x in a.js("return [...document.querySelectorAll('main .benv .t1, main .bwide-h .grow > span:first-child')].map(e => e.textContent.trim());") if x in ("Alpha", "Bravo", "Charlie")]
    check(names() == ["Alpha", "Bravo", "Charlie"], "ordre de départ : %s" % names())
    pg.locator("main button.link", has_text="Réorganiser").click(); a.wait(400)
    check(pg.locator('[aria-label="Monter Alpha"]').is_disabled(), "le premier budget peut encore monter")
    pg.click('[aria-label="Monter Charlie"]'); a.wait(400)
    pg.click('[aria-label="Monter Charlie"]'); a.wait(400)
    pg.click('[aria-label="Terminer la réorganisation"]'); a.wait(500)
    check(names() == ["Charlie", "Alpha", "Bravo"], "ordre après ↑ ↑ : %s" % names())
    a.js("D.NAV.tab('comptes')"); a.wait(300); a.js("D.NAV.tab('budgets')"); a.wait(600)
    check(names() == ["Charlie", "Alpha", "Bravo"], "ordre non gardé : %s" % names())
    a.no_errors("ordre des budgets")
    a.close()


@test
def regrouper_budgets(br, url):
    """Trois budgets annuels sur la même catégorie « Vacances » (cas de Nicolas) : une dépense sans budget précis est déduite des trois. Budgets › « Regrouper » : un budget, une sous-catégorie par ancien budget, l'échéance rattachée suit sa ligne, la dépense à classer va dans le séjour choisi, chaque enveloppe n'est plus consommée que par ses dépenses ; la fiche et le + demandent « Pour quel séjour ? »."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    liv = a.acc_id("Livret")
    a.js("""const t = D.today(), y = Number(t.slice(0, 4)), nx = D.addDays(t, 60), ops = [['put', {k: 'cat', id: 'cat_t_vac', name: 'Vacances', type: 'both', parent: null, color: '#5B6B8C'}]];
      for (const [id, n, m, amt] of [['h', 'Vacances d’hiver', 3, 500], ['p', 'Vacances de printemps', 5, 400], ['e', 'Vacances d’été', 7, 3000]]) {
        const st = (y - 1) + '-' + String(m).padStart(2, '0') + '-01';
        ops.push(['put', {k: 'bdg', id: 'bdg_t_' + id, name: n, accounts: [arg], remaining: true, remAcc: arg, hidden: false, order: 20, start: st, end: null}]);
        ops.push(['put', {k: 'bud', id: 'bud_t_' + id, budget: 'bdg_t_' + id, cat: 'cat_t_vac', amount: amt, income: false, start: st, end: null, dur: {n: 1, u: 'y'}, carry: false, hist: []}]); }
      ops.push(['put', {k: 'sch', id: 'sch_t_loc', account: arg, name: 'Location été', amount: -1200, splits: [{amount: -1200, cat: 'cat_t_vac', bdg: 'bdg_t_e'}], freq: {n: 1, u: 'y'}, next: nx, anchor: Number(nx.slice(8)), start: nx, end: null, left: null, auto: false, paused: false, skips: [], pay: 'virement'}]);
      ops.push(['put', {k: 'tx', id: 'tx_t_ski', account: arg, date: D.addDays(t, 20), name: 'Forfaits ski', amount: -100, splits: [{amount: -100, cat: 'cat_t_vac'}], st: 'prevue', pay: 'cb'}]);
      return D.Store.bulk(ops);""", liv); a.wait(600)
    rest = lambda: a.js("const t = D.today(); return Object.fromEntries(Object.values(D.A().bud).filter(l => /^bud_t_/.test(l.id)).map(l => [l.id.slice(6), D.lineAt(D.linesOf(l.budget).find(x => x.id === l.id), t)?.rest]));")
    r0 = rest()
    check(r0 == {"h": 400, "p": 300, "e": 1700}, "avant : la dépense sans budget devait être déduite des trois enveloppes : %s" % r0)
    check(len(a.js("return D.bdgGroups();")) == 1, "groupe de budgets non détecté")
    a.js("D.NAV.tab('budgets')"); a.wait(600)
    ban = pg.locator(".prow", has_text="3 budgets suivent la catégorie Vacances")
    check(ban.count() == 1, "bandeau « Regrouper » absent")
    ban.click(); a.wait(500)
    sh = pg.locator(".sheet").last
    names = [sh.locator(".grp-row input").nth(i).input_value() for i in range(sh.locator(".grp-row input").count())]
    check(sorted(names) == ["Hiver", "Printemps", "Été"], "noms proposés : %s" % names)
    sel = sh.locator('select[aria-label="Ligne pour Forfaits ski"]')
    check(sel.count() == 1 and sel.evaluate("e => e.selectedOptions[0].textContent") == "Hiver", "dépense à classer absente ou mauvais séjour proposé")
    sel.select_option(label="Printemps"); a.wait(200)
    sh.locator(".btn.pri", has_text="Regrouper").click(); a.wait(1200)
    st = a.js("""const A = D.A(), sub = Object.fromEntries(Object.values(A.cat).filter(c => c.parent === 'cat_t_vac').map(c => [c.name, c.id]));
      const b = Object.values(A.bdg).filter(x => x.name === 'Vacances');
      return {sub, nb: b.length, old: ['h', 'p', 'e'].filter(k => A.bdg['bdg_t_' + k]).length, lines: Object.values(A.bud).filter(l => /^bud_t_/.test(l.id)).map(l => [l.id.slice(6), l.budget === b[0]?.id, A.cat[l.cat]?.name]),
        loc: A.sch.sch_t_loc.splits[0], ski: A.tx.tx_t_ski.splits[0], grp: D.bdgGroups().length,
        rem: D.occurrences(D.addDays(D.today(), 400)).filter(o => o.kind === 'bud' && o.account === arg).map(o => o.name)};""", liv)
    check(sorted(st["sub"]) == ["Hiver", "Printemps", "Été"] and st["nb"] == 1 and st["old"] == 0 and st["grp"] == 0, "regroupement : %s" % st)
    check(sorted(st["lines"]) == [["e", True, "Été"], ["h", True, "Hiver"], ["p", True, "Printemps"]], "lignes : %s" % st["lines"])
    check(st["loc"]["cat"] == st["sub"]["Été"] and "bdg" not in st["loc"], "échéance rattachée : %s" % st["loc"])
    check(st["ski"]["cat"] == st["sub"]["Printemps"], "dépense classée : %s" % st["ski"])
    check(any(n == "Budget Vacances · Hiver (reste)" for n in st["rem"]), "nom des lignes de reste : %s" % st["rem"][:4])
    r1 = rest()
    check(r1 == {"h": 500, "p": 300, "e": 1800}, "après : chaque enveloppe ne compte que ses dépenses : %s" % r1)
    a.js("D.NAV.tab('budgets')"); a.wait(600)
    wide = pg.locator(".bwide", has_text="Vacances")
    check(wide.count() == 1 and wide.locator(".bwide-r").count() == 3, "carte Vacances avec une ligne par séjour absente")
    # fiche : catégorie Vacances → « Pour quel séjour ? »
    a.js("D.SHEET.open({type: 'op', kind: 'tx', acc: arg})", liv); a.wait(500)
    sh = pg.locator(".sheet").last
    sh.locator("#op-amt").fill("45"); sh.locator("#op-name").fill("Luge")
    sh.locator("#op-cat").click(); a.wait(300)
    sh.locator(".frow-grid .catbtn", has_text="Vacances").first.click(); a.wait(300)
    check(sh.locator(".fkids").count() == 1 and "Pour quel séjour ?" in sh.locator(".fkids").inner_text(), "question « Pour quel séjour ? » absente de la fiche")
    sh.locator(".chip-k", has_text="Hiver").click(); a.wait(200)
    check("Vacances › Hiver" in sh.locator(".fkh").inner_text(), "séjour non choisi dans la fiche")
    sh.locator(".btn.pri").last.click(); a.wait(600)
    t = a.js("return Object.values(D.A().tx).find(t => t.name === 'Luge');")
    check(t and t["splits"][0]["cat"] == st["sub"]["Hiver"], "fiche : opération enregistrée %s" % t)
    # + : Vacances, puis le séjour ; les séjours ne sont pas dans la grille
    pg.click('[aria-label="Saisir"]'); a.wait(300)
    pg.click(".wiz >> text=Dépense"); a.wait(300)
    for k in "28": pg.click('.wiz button:text-is("%s")' % k)
    pg.locator("#wz-acc, .wiz select[aria-label=Compte]").first.select_option(liv); a.wait(200)
    pg.click(".wgo"); a.wait(300)
    check(pg.locator('.wiz .wbig:text-is("Hiver")').count() == 0, "les séjours sont dans la grille des catégories")
    pg.locator('.wiz .wbig:has-text("Vacances")').first.click(); a.wait(300)
    check("Pour quel séjour ?" in pg.locator(".wiz").inner_text(), "+ : question « Pour quel séjour ? » absente")
    pg.locator('.wiz .wbig:has-text("Été")').first.click(); a.wait(300)
    check("Quand" in pg.locator(".wiz").inner_text(), "+ : étape Quand absente après le séjour")
    pg.click(".wgo"); a.wait(300)
    check("Vacances › Été" in pg.locator(".wiz").inner_text(), "+ : récapitulatif sans le séjour")
    pg.click('.wgo:has-text("Enregistrer")'); a.wait(700)
    t = a.js("return Object.values(D.A().tx).find(t => t.amount === -28);")
    check(t and t["splits"][0]["cat"] == st["sub"]["Été"], "+ : opération enregistrée %s" % t)
    a.no_errors("regrouper")
    a.close()


@test
def soldes_courants_ordre(br, url):
    """Opérations d'un compte sur 12 mois, triées des plus anciennes aux plus récentes puis l'inverse : le solde « puis … » de chaque ligne = celui de la ligne d'avant + son montant, même avec plusieurs opérations prévues le même jour (cas du LDDS de Nicolas)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    liv = a.acc_id("Livret")
    a.js("""for (const [id, n, v] of [['sch_t_o1', 'Épargne', 1000], ['sch_t_o2', 'Voyage', -900], ['sch_t_o3', 'Cadeau', -50]])
      D.Store.put({k: 'sch', id, account: arg, name: n, amount: v, splits: [{amount: v, cat: null}], freq: {n: 1, u: 'm'}, next: '2026-12-01', anchor: 1, start: '2026-12-01', end: null, left: null, auto: false, paused: false, skips: [], pay: 'prelevement'});""", liv)
    num = lambda x: None if not x else float(x.replace("puis", "").replace("\u202f", "").replace("\xa0", "").replace(" ", "").replace("€", "").replace("−", "-").replace("+", "").replace(",", "."))
    for srt in ("da", "dd"):
        a.js("D.Store.put({...D.S(), accSort: arg, planUntil: 'm12'});", srt); a.wait(300)
        a.js("D.NAV.push({v: 'acc', id: arg});", liv); a.wait(500)
        pg.locator(".seg button", has_text="Opérations").first.click(); a.wait(600)
        rows = a.js("return [...document.querySelectorAll('main .card .row')].map(r => { const am = r.querySelector('.amt'); return [(r.querySelector('.t1') || r).textContent.trim(), am ? am.childNodes[0].textContent : null, am?.querySelector('small')?.textContent || null]; }).filter(x => x[2]);")
        if srt == "dd": rows = rows[::-1]
        check(len(rows) > 20, "trop peu de lignes (%d)" % len(rows))
        bad = [(n, v, r) for (pn, pv, pr), (n, v, r) in zip(rows, rows[1:]) if abs(num(pr) + num(v) - num(r)) > 0.005]
        check(not bad, "tri %s : sous-totaux incohérents %s" % (srt, bad[:3]))
        a.js("D.NAV.tab('comptes')"); a.wait(300)
    a.no_errors("ordre des soldes")
    a.close()


@test
def accueil_a_valider_solde(br, url):
    """Accueil, « À valider » : chaque opération en retard affiche le solde « puis … », du solde validé à la dernière, qui s'enchaîne avec les suivantes."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    a.pick("Joint")
    j = a.acc_id("Joint")
    a.js("""const t = D.today();
      D.Store.put({k: 'tx', id: 'tx_t_av1', account: arg, date: D.addDays(t, -6), name: 'Cantine test', amount: -45, splits: [{amount: -45, cat: null}], st: 'saisie'});
      D.Store.put({k: 'tx', id: 'tx_t_av2', account: arg, date: D.addDays(t, -2), name: 'Boulangerie test', amount: -12.5, splits: [{amount: -12.5, cat: null}], st: 'saisie'});""", j)
    a.wait(600)
    num = lambda x: float(x.replace("puis", "").replace("\u202f", "").replace("\xa0", "").replace(" ", "").replace("€", "").replace("−", "-").replace("+", "").replace(",", "."))
    groups = a.js("""const out = [];
      for (const g of document.querySelectorAll('main .fgh')) { const c = g.nextElementSibling; if (!c) continue;
        out.push([g.textContent.trim(), [...c.querySelectorAll('.amt')].map(s => [s.childNodes[0].textContent, s.querySelector('small')?.textContent || null])]); }
      return out;""")
    names = [g[0] for g in groups]
    check("À valider" in names, "groupe À valider absent : %r" % names)
    gi = names.index("À valider")
    rows = groups[gi][1]
    check(len(rows) >= 2 and all(r[1] and r[1].startswith("puis") for r in rows), "À valider sans solde : %r" % rows)
    r = a.js("return D.balance(arg, {cleared: true});", j)
    for amt, run in rows:
        r = round(r + num(amt), 2)
        check(near(num(run), r), "À valider : %s puis %s (attendu %.2f)" % (amt, run, r))
    if gi + 1 < len(groups) and groups[gi + 1][1]:
        amt, run = groups[gi + 1][1][0]
        check(run and near(num(run), round(r + num(amt), 2)), "la suite ne s'enchaîne pas : %s puis %s après %.2f" % (amt, run, r))
    a.no_errors("accueil, solde des opérations à valider")
    a.close()


@test
def comptes_solde_valide_cartes(br, url):
    """Écran Comptes : les cartes affichent leur solde validé, comme les comptes (pas l'encours avec les opérations à valider)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    c = a.acc_id("Carte Joint")
    a.js("D.Store.put({k: 'tx', id: 'tx_t_cb_val', account: arg, date: D.today(), name: 'Achat à valider', amount: -12.34, splits: [{amount: -12.34, cat: null}], st: 'saisie'});", c)
    a.wait(400)
    val, enc = a.js("return [D.balance(arg, {cleared: true}), D.balance(arg)];", c)
    check(abs(val - enc) > 0.004, "la carte devrait avoir une opération à valider")
    pg.evaluate("location.hash = '#mes'")
    a.wait(600)
    row = pg.locator("button.row", has_text="Carte Joint").first.inner_text().replace("\u202f", " ").replace("\xa0", " ")
    want = ("−" if val < 0 else "") + "{:,.2f}".format(abs(val)).replace(",", " ").replace(".", ",")
    check(want in row, "ligne de la carte : %r (attendu le solde validé %s)" % (row, want))
    a.js("D.NAV.push({v: 'acc', id: arg});", c)
    a.wait(500)
    hero = pg.inner_text("section.acc-hero").replace("\u202f", " ").replace("\xa0", " ")
    check("Solde validé" in hero and want in hero and "Encours" in hero, "en-tête de la carte : %r" % hero[:200])
    a.no_errors("comptes, solde validé des cartes")
    a.close()


@test
def budgets_enveloppe(br, url):
    """Budgets façon enveloppe : ce qui reste en premier ; le montant se change d'un geste (ce mois-ci seulement, à partir de ce mois, à partir du mois prochain)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    pg = a.pg
    a.js("D.Store.put({...D.A().set.set_main, start: '2026-09-01'});")
    pg.evaluate("location.hash = '#budgets'")
    a.wait(800)
    body = a.body()
    check("Il te reste à dépenser ce mois-ci" in body and "Il reste" in body, "liste des budgets : %s" % body[:300])
    a.no_errors("liste des budgets")
    lid = a.js("return Object.values(D.A().bud).find(l => D.A().cat[l.cat]?.name === 'Loisirs').id;")
    amt = lambda d: a.js("return D.baseAmt(D.A().bud[arg[0]], arg[1]);", [lid, d])
    habit = a.js("return D.baseAmt({...D.A().bud[arg], ov: {}}, '2026-11-01');", lid)
    oct0 = amt("2026-10-01")

    def sheet(val, mode):
        pg.locator(".sheet #am-v").fill(str(val))
        pg.locator(".sheet [role=radio]", has_text=mode).click()
        pg.locator(".sheet .btn.pri", has_text="Enregistrer").click()
        a.wait(500)

    # depuis la liste : le crayon du montant ouvre directement la fenêtre
    pg.locator('.benv-amt[aria-label*="Loisirs"]').click()
    a.wait(400)
    check("À partir de quand ?" in a.body(), "fenêtre du montant absente")
    a.no_errors("fenêtre du montant")
    sheet(250, "Ce mois-ci seulement")
    check(near(amt("2026-11-01"), 250) and near(amt("2026-12-01"), habit) and near(amt("2026-10-01"), oct0), "ce mois-ci seulement : nov. %s, déc. %s (attendu 250 puis %s)" % (amt("2026-11-01"), amt("2026-12-01"), habit))
    # depuis l'écran du budget : la ligne « Montant »
    pg.locator('.benv-main', has_text="Loisirs").click()
    a.wait(600)
    check("Dépenses de novembre" in a.body() and "Réglages" in a.body(), "écran du budget : %s" % a.body()[:300])
    a.no_errors("écran du budget")
    pg.locator(".benv-mrow").click()
    a.wait(400)
    check("Revenir à %d" % round(habit) in a.body().replace("\u202f", " ").replace("\xa0", " "), "bouton « Revenir » absent alors que novembre est ajusté")
    sheet(220, "À partir de ce mois")
    check(near(amt("2026-11-01"), 220) and near(amt("2027-01-01"), 220) and near(amt("2026-10-01"), oct0), "à partir de ce mois : oct. %s, nov. %s, janv. %s" % (amt("2026-10-01"), amt("2026-11-01"), amt("2027-01-01")))
    pg.locator(".benv-mrow").click()
    a.wait(400)
    sheet(300, "À partir du mois prochain")
    check(near(amt("2026-11-01"), 220) and near(amt("2026-12-01"), 300) and near(amt("2027-03-01"), 300) and near(amt("2026-10-01"), oct0), "à partir du mois prochain : nov. %s, déc. %s" % (amt("2026-11-01"), amt("2026-12-01")))
    # même montant que d'habitude, une seule fois : pas d'ajustement inutile
    pg.locator(".benv-mrow").click()
    a.wait(400)
    sheet(220, "Ce mois-ci seulement")
    check(a.js("return D.A().bud[arg].ov?.['2026-11-01'] == null;", lid), "ajustement inutile gardé")
    # historique : une barre par mois depuis septembre ; toucher octobre affiche octobre
    check(pg.locator(".hb").count() == 3, "barres : %d au lieu de 3" % pg.locator(".hb").count())
    pg.locator('.hb[aria-label^="Octobre"]').click()
    a.wait(400)
    check("Octobre · " in a.body() and "Dépenses d'octobre" in a.body(), "toucher octobre : %s" % a.body()[:200])
    pg.locator("text=Revenir à ce mois-ci").click()
    a.wait(300)
    check("Novembre · " in a.body(), "retour à novembre")
    # reporter le reste
    pg.locator('.setcard [role=switch]', has_text="Reporter le reste").click()
    a.wait(300)
    check(a.js("return !!D.A().bud[arg].carry;", lid), "report non activé")
    a.no_errors("réglages du budget")
    a.js("D.NAV.pop();")
    a.wait(300)
    # budget à plusieurs catégories : une ligne par catégorie, chacune avec son montant
    bid = a.js("return D.A().bud[arg].budget;", lid)
    a.js("D.Store.put({k: 'bud', id: 'bud_t2', budget: arg, cat: '__other__', amount: 50, income: false, carry: false, start: '2026-11-01', end: null, dur: {n: 1, u: 'm'}, hist: []});", bid)
    a.wait(400)
    pg.locator('.benv-amt[aria-label*="Loisirs"]').click()
    a.wait(500)
    check(pg.locator(".benv-li").count() == 2 and "Catégories" in a.body(), "budget à 2 catégories : %d lignes" % pg.locator(".benv-li").count())
    pg.locator('.benv-amt[aria-label*="autres catégories"]').click()
    a.wait(400)
    sheet(80, "À partir de ce mois")
    check(near(a.js("return D.baseAmt(D.A().bud.bud_t2, '2026-12-01');"), 80), "montant de la 2e catégorie")
    a.no_errors("budget à plusieurs catégories")
    a.js("D.NAV.pop();")
    a.wait(300)
    # autre mois : décembre
    pg.locator('input[type=month]').evaluate("(e) => { e.value = '2026-12'; e.dispatchEvent(new Event('input', {bubbles: true})); }")
    a.wait(500)
    check("À dépenser en décembre" in a.body(), "mois de décembre : %s" % a.body()[:200])
    a.no_errors("budgets en décembre")
    a.close()


@test
def budgets_report(br, url):
    """Report du reste : rien n'est reporté depuis les mois d'avant Pécule (dépenses restées dans iCompta) ; un dépassement du mois en cours se reporte."""
    a = App(br, url)
    a.import_ic(FICTIF)
    lid = a.js("return Object.values(D.A().bud).find(l => D.A().cat[l.cat]?.name === 'Courses').id;")
    nov0 = a.js("return D.lineBudget(D.A().bud[arg], '2026-11-01');", lid)
    a.js("D.Store.put({...D.A().bud[arg], carry: true});", lid)
    a.wait(400)
    nov1 = a.js("return D.lineBudget(D.A().bud[arg], '2026-11-01');", lid)
    check(near(nov1, nov0), "novembre gonflé par le report des mois d'avant Pécule : %s au lieu de %s" % (nov1, nov0))
    rest = a.js("return D.lineBudget(D.A().bud[arg], '2026-11-01') - D.lineActual(D.A().bud[arg], '2026-11-01', '2026-11-30');", lid)
    dec = a.js("return D.lineBudget(D.A().bud[arg], '2026-12-01');", lid)
    check(near(dec, 600 + min(0, rest)), "décembre : %s (attendu %s : novembre pas fini, seul un dépassement se reporte)" % (dec, 600 + min(0, rest)))
    a.no_errors("report des budgets")
    a.close()


@test
def budgets_suivent_les_saisies(br, url):
    """Une dépense par carte consomme le budget de sa catégorie ; changer la catégorie d'une opération change de budget (rattachement périmé ignoré)."""
    a = App(br, url)
    a.import_ic(FICTIF)
    d0, l0 = rest(a, "Divers"), rest(a, "Loisirs")
    wizard(a, "Dépense", "20", cat="Divers", acc="Carte Perso")
    check(near(rest(a, "Divers"), d0 - 20), "dépense par carte (Divers) : reste %s → %s (attendu −20)" % (d0, rest(a, "Divers")))
    t = a.js("return Object.values(D.A().tx).find(t => t.name === 'Concert');")
    check(t and t["splits"][0].get("bdg"), "« Concert » importé sans rattachement de budget : %s" % t)
    a.js("D.SHEET.open({type: 'op', kind: 'tx', src: D.A().tx[arg]});", t["id"])
    a.wait(500)
    a.pg.locator(".sheet").get_by_text("Loisirs", exact=False).first.click()
    a.wait(300)
    a.pg.locator('.sheet .catbtn:has-text("Divers")').first.click()
    a.wait(300)
    a.pg.locator('.sheet button:has-text("Enregistrer")').first.click()
    a.wait(600)
    check(near(rest(a, "Divers"), d0 - 20 - 45) and near(rest(a, "Loisirs"), l0 + 45), "changement de catégorie Loisirs → Divers : Divers %s, Loisirs %s" % (rest(a, "Divers"), rest(a, "Loisirs")))
    # données anciennes : un rattachement périmé déjà enregistré est ignoré
    a.js("const t = D.A().tx[arg]; D.Store.put({...t, splits: [{...t.splits[0], bdg: Object.values(D.A().bdg).find(b => b.name === 'Loisirs').id}]});", t["id"])
    a.wait(300)
    check(near(rest(a, "Divers"), d0 - 20 - 45), "rattachement périmé à Loisirs : Divers %s" % rest(a, "Divers"))
    a.no_errors("budgets")
    a.close()


@test
def valider_echeance(br, url):
    """Valider une échéance à venir (loyer du 5 déc.) : elle devient une opération, l'échéancier passe à janvier, les soldes projetés ne bougent pas."""
    a = App(br, url)
    a.import_ic(FICTIF)
    p0 = a.projected(DATES)
    a.pick("Joint")
    a.pg.select_option("main select", label="3 mois")
    a.wait(500)
    row = a.pg.locator("main .row").filter(has_text="Loyer").filter(has_text="5").first
    row.locator("[aria-label=\"Valider l'échéance\"]").click()
    a.wait(800)
    if a.pg.locator(".sheet").count():
        a.pg.locator('.sheet button:has-text("Enregistrer"), .sheet button:has-text("Valider")').first.click()
        a.wait(800)
    t = a.js("return Object.values(D.A().tx).find(t => t.name === 'Loyer' && t.date === '2026-12-05');")
    check(t and near(t["amount"], -1200), "opération du loyer de décembre non créée : %s" % t)
    nxt = a.js("return Object.values(D.A().sch).find(s => s.name === 'Loyer').next;")
    check(nxt == "2027-01-05", "échéancier du loyer : prochaine date %s (attendu 2027-01-05)" % nxt)
    p1 = a.projected(DATES)
    check(p1 == p0, "soldes projetés modifiés par la validation : %s" % {k: (p0[k], p1[k]) for k in p0 if p0[k] != p1.get(k)})
    a.no_errors("valider échéance")
    a.close()


@test
def echeance_modifiee(br, url):
    """Modifier le montant d'une échéance : les prévisions suivent."""
    a = App(br, url)
    a.import_ic(FICTIF)
    s = a.js("return Object.values(D.A().sch).find(s => s.name === 'Loyer');")
    p0 = a.projected(["2027-01-31"])["Joint"][0]
    a.js("const s = {...D.A().sch[arg]}; s.amount = -1300; s.splits = [{...s.splits[0], amount: -1300}]; D.Store.put(s);", s["id"])
    a.wait(400)
    p1 = a.projected(["2027-01-31"])["Joint"][0]
    check(near(p1, p0 - 200), "Joint fin janv. : %s → %s (attendu −200, 2 loyers)" % (p0, p1))
    a.no_errors("échéance")
    a.close()


# ---------------------------------------------------------------- vrai fichier iCompta (facultatif, reste sur la machine)
def real_truth(path, dates):
    db = sqlite3.connect(path)
    names = {r[0]: r[1].strip() for r in db.execute("select ID, name from ICAccount where class='ICAccount'")}
    # montant d'une opération comme iCompta : le sien si « useSumOfSplits » vaut 0, sinon la somme de ses lignes
    q = ('select sum(case when t.useSumOfSplits = 0 and t.amount is not null and t.amount != "" then t.amount '
         'else (select coalesce(sum(s.amount), 0) from ICTransactionSplit s where s."transaction" = t.ID) end) '
         'from ICTransaction t where t.account=? and t.date<=? and t.status!="ICTransactionStatus.CanceledStatus"')
    return {nm: [round(db.execute(q, (aid, d)).fetchone()[0] or 0, 2) for d in dates] for aid, nm in names.items()}


def real_test(path):
    def vrai_fichier_icompta(br, url):
        """Vrai fichier iCompta : soldes projetés de chaque compte = iCompta (aujourd'hui → +13 mois)."""
        # « aujourd'hui » = le jour du fichier (dernière opération validée, jamais après la vraie date du jour), au plus tôt la date des tests
        db = sqlite3.connect(path)
        last = db.execute('select max(date) from ICTransaction where status in ("ICTransactionStatus.ClearedStatus", "ICTransactionStatus.ReconciledStatus") and date <= ?', (dt.date.today().isoformat(),)).fetchone()[0] or ""
        now = max(NOW, dt.datetime.fromisoformat(last[:10] + "T10:00")) if last else NOW
        dates = sorted({now.date().isoformat(), "2026-11-30", "2026-12-31", "2027-01-31", "2027-03-31", "2027-06-30", "2027-12-31"})
        dates = [d for d in dates if d >= now.date().isoformat()]
        # iCompta ne crée les opérations prévues que jusqu'à un certain point (parfois quelques mois pour une échéance) :
        # au-delà, ses soldes « s'arrêtent » alors que Pécule continue. On compare seulement jusqu'à cet horizon.
        lasts = [r[0] for r in db.execute('select (select max(t.date) from ICTransaction t where t.scheduledTransaction = s.ID) from ICScheduledTransaction s '
                                          'where s.numberOfOccurrences is null or s.numberOfOccurrences = ""') if r[0]]
        if lasts:
            horizon = min(lasts)
            if any(d > horizon for d in dates):
                print("      comparaison jusqu'au %s (au-delà, iCompta n'a pas créé toutes ses opérations prévues)" % horizon)
            dates = [d for d in dates if d <= horizon]
        want = real_truth(path, dates)
        a = App(br, url, now=now)
        a.import_ic(path)
        got = a.projected(dates)
        for acc, v in want.items():
            for d, x, y in zip(dates, got.get(acc, [None] * len(dates)), v):
                check(near(x, y), "%s au %s : Pécule %s, iCompta %s" % (acc, d, x, y))
        check_cards(a, n=0 if not a.js("return D.accList(true).some(c => c.type === 'carte');") else None)
        check_budgets(a, path, until="2026-11-30")
        all_screens(a)
        a.close()
    return vrai_fichier_icompta


# ---------------------------------------------------------------- lancement
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--icompta")
    ap.add_argument("-k")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    global FICTIF
    FICTIF = os.path.join(OUT, "fictif.cdb")
    fixture_icompta.build(FICTIF)
    tests = list(TESTS)
    if args.icompta:
        tests.append(real_test(args.icompta))
    if args.k:
        tests = [t for t in tests if args.k in t.__name__]
    srv, url = serve()
    ok = ko = 0
    with sync_playwright() as p:
        br = p.chromium.launch()
        for t in tests:
            try:
                t(br, url)
                ok += 1
                print("  ✓ %s" % t.__name__)
            except Exception as e:
                ko += 1
                msg = str(e) if isinstance(e, Fail) else "%s\n%s" % (e, traceback.format_exc(limit=3))
                print("  ✗ %s : %s" % (t.__name__, msg))
        br.close()
    srv.shutdown()
    print("\n%d réussi(s), %d échec(s) sur %d" % (ok, ko, ok + ko))
    sys.exit(1 if ko else 0)


if __name__ == "__main__":
    main()
