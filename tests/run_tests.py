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
        'halfBase, halfParts, halfName, glanceState, shownBal, cats, catIc, guessIc};\n')


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

    def __init__(self, browser, url, view=MOBILE, dark=False):
        self.ctx = browser.new_context(viewport=view, color_scheme="dark" if dark else "light", service_workers="block", accept_downloads=True)
        self.pg = self.ctx.new_page()
        self.errors = []
        self.pg.on("pageerror", lambda e: self.errors.append("Erreur JS : %s" % e))
        self.pg.on("console", lambda m: m.type == "error" and not self.external(m) and self.errors.append("Console : %s" % m.text))
        self.base = url
        self.pg.clock.install(time=NOW)
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

    def import_ic(self, path, pending=True):
        self.pg.click("text=Importer depuis iCompta")
        self.pg.set_input_files(".wiz input[type=file] >> nth=0", path)
        self.wait(2500)
        if not pending:
            self.pg.locator(".wiz").get_by_text("Seulement les opérations créées et prévues").click()
            self.wait(2500)
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
    check(any(t["name"] == "Impôts" and t["date"] == "2027-01-15" for t in tx), "opération ponctuelle prévue absente")
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
    check(cards and (n is None or len(cards) == n), "cartes : %s" % cards)
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
              {"type": "line", "id": ids["line"]}, {"type": "lineDates", "budget": ids["bdg"]}, {"type": "fillAvg", "budget": ids["bdg"]}, {"type": "cash"}, {"type": "reset", "id": ids["acc"]},
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
        w = a.pg.evaluate("document.documentElement.scrollWidth - innerWidth")
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
    """Bouton + › Échéance : dépense de 15 € le 12 de chaque mois, catégorie Divers."""
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
    a.pg.locator('.wiz .wbig:text-is("12")').first.click()
    a.wait(300)
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
    a.pick("Joint")
    before = [t.split("\n")[1] for t in a.pg.locator(".dchip").all_inner_texts()]
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
    a.pick("Joint")
    after = [t.split("\n")[1] for t in a.pg.locator(".dchip").all_inner_texts()]
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
def rentrees_et_solde_de_depart(br, url):
    """Deux rentrées le même jour : additionnées sur l'accueil. Changer le signe du solde de départ : avertissement avant d'enregistrer."""
    a = App(br, url)
    a.import_ic(FICTIF)
    c = a.acc_id("Courant")
    a.js("D.Store.put({k: 'tx', id: 'tx_prime', account: arg, date: '2026-11-28', name: 'Prime', amount: 300, splits: [{amount: 300}], st: 'saisie'});", c)
    a.pick("Courant")
    t = a.pg.inner_text("main section")
    check("2 rentrées · 28 nov." in t and "+2 800,00" in t.replace("\u202f", " ").replace("\xa0", " "), "rentrées du 28 nov. non additionnées : %s" % t.replace("\n", " | ")[:300])
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
        dates = [NOW.date().isoformat(), "2026-11-30", "2026-12-31", "2027-01-31", "2027-03-31", "2027-06-30", "2027-12-31"]
        want = real_truth(path, dates)
        a = App(br, url)
        a.import_ic(path)
        got = a.projected(dates)
        for acc, v in want.items():
            for d, x, y in zip(dates, got.get(acc, [None] * len(dates)), v):
                check(near(x, y), "%s au %s : Pécule %s, iCompta %s" % (acc, d, x, y))
        check_cards(a)
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
