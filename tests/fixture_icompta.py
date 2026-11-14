"""Fabrique un fichier iCompta FICTIF (données inventées) pour tester l'import et les calculs de Pécule.

Situation simulée : aujourd'hui = 11 novembre 2026, historique depuis septembre, opérations
prévues générées par l'échéancier jusqu'à fin février 2027.

Comptes : Courant, Joint, Livret, Carte Perso (débit différé sur Courant), Carte Joint (sur Joint),
Prêt (mensualité depuis le Joint). Échéances mensuelles, annuelle, limitée, virements, opération
prévue passée laissée « prévue », opération ponctuelle prévue, opération annulée, budgets avec
lignes de projection, paire 50/50 de fin de mois (« Claire »).

Usage : python3 tests/fixture_icompta.py chemin/sortie.cdb
"""
import datetime as dt
import json
import os
import sqlite3
import sys
import uuid

TODAY = dt.date(2026, 11, 11)
FIRST = dt.date(2026, 9, 1)
LAST = dt.date(2027, 2, 28)
K, C, P, X = ("ICTransactionStatus." + s for s in ("ClearedStatus", "CreatedStatus", "PlannedStatus", "CanceledStatus"))
HERE = os.path.dirname(os.path.abspath(__file__))


def uid():
    return str(uuid.uuid4()).upper()


def add_months(d, n, day=None):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(day or d.day, last))


def month_end(d):
    return add_months(d.replace(day=1), 1) - dt.timedelta(days=1)


def status_for(d, created=False):
    if d >= dt.date(2026, 12, 1):
        return P
    if d > TODAY:
        return C
    return C if created else K


class Fixture:
    def __init__(self, path):
        if os.path.exists(path):
            os.remove(path)
        self.db = sqlite3.connect(path)
        self.db.executescript(open(os.path.join(HERE, "icompta-schema.sql")).read())
        self.n = 0
        self.tx = []  # (account, date, status, [(amount, cat)])  pour calculer la vérité

    def ins(self, table, **kw):
        cols = ",".join('"%s"' % k for k in kw)
        self.db.execute("insert into %s (%s) values (%s)" % (table, cols, ",".join("?" * len(kw))), list(kw.values()))

    # --- structure ---------------------------------------------------------
    def account(self, name, typ):
        i = uid()
        self.ins("ICAccount", ID=i, name=name, type="ICAccountType." + typ, hidden=0, parent="", index=self.n, currency="EUR", warningBalance=0, includedInTotal=1, **{"class": "ICAccount"})
        self.n += 1
        return i

    def category(self, name, expense=1, income=1, parent=""):
        i = uid()
        self.ins("ICCategory", ID=i, name=name, parent=parent, index=self.n, expense=expense, income=income, color="0.4 0.5 0.9 1.0")
        self.n += 1
        return i

    # --- opérations --------------------------------------------------------
    def split(self, tid, amount, cat=None, link=None, bip=None, idx=0, refund=0):
        sid = uid()
        self.ins("ICTransactionSplit", ID=sid, transaction=tid, amount="%.2f" % amount, category=cat, linkedSplit=link, budgetItemPeriod=bip,
                 index=idx, ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=refund, usesAccountOwners=0)
        return sid

    def op(self, account, d, name, parts, status=None, sch=None, occ=-1):
        """parts : liste de (montant, catégorie)"""
        status = status or status_for(d)
        tid = uid()
        self.ins("ICTransaction", ID=tid, account=account, date=d.isoformat(), name=name, status=status, scheduledTransaction=sch, occurrence=occ, index=0, useSumOfSplits=1)
        for i, (a, c) in enumerate(parts):
            self.split(tid, a, c, bip=self.bip_for(account, c, d, a), idx=i)
        self.tx.append((account, d, status, [(a, c) for a, c in parts]))
        return tid

    def transfer(self, frm, to, d, name_from, name_to, amount, status=None, cat=None, sch=(None, None), occ=-1):
        status = status or status_for(d)
        t1, t2 = uid(), uid()
        self.ins("ICTransaction", ID=t1, account=frm, date=d.isoformat(), name=name_from, status=status, scheduledTransaction=sch[0], occurrence=occ, index=0, useSumOfSplits=1)
        self.ins("ICTransaction", ID=t2, account=to, date=d.isoformat(), name=name_to, status=status, scheduledTransaction=sch[1], occurrence=occ, index=0, useSumOfSplits=1)
        s1, s2 = uid(), uid()
        for sid, tid, a in ((s1, t1, -amount), (s2, t2, amount)):
            self.ins("ICTransactionSplit", ID=sid, transaction=tid, amount="%.2f" % a, category=cat, linkedSplit=(s2 if sid == s1 else s1), index=0,
                     ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=0, usesAccountOwners=0)
        self.tx.append((frm, d, status, [(-amount, cat)]))
        self.tx.append((to, d, status, [(amount, cat)]))

    # --- échéancier --------------------------------------------------------
    def schedule(self, account, start, name, splits, unit=2, nb=0, sid=None, nxt=0):
        i = sid or uid()
        self.ins("ICScheduledTransaction", ID=i, account=account, date=start.isoformat(), name=name, amount="%.2f" % sum(a for a, *_ in splits),
                 frequency=1, frequencyUnit=unit, numberOfOccurrences=nb or None, nextOccurrence=nxt, index=0, useSumOfSplits=1)
        ids = []
        for k, (a, c, *lk) in enumerate(splits):
            ss = uid()
            self.ins("ICScheduledTransactionSplit", ID=ss, transaction=i, amount="%.2f" % a, category=c, linkedSplit=(lk[0] if lk else None), index=k,
                     ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=0, usesAccountOwners=0)
            ids.append(ss)
        return i, ids

    def set_next(self, sid, n):
        self.db.execute('update ICScheduledTransaction set nextOccurrence=? where ID=?', (n, sid))

    # --- budgets -----------------------------------------------------------
    def budgets(self, specs):
        """specs : [(nom, catégorie, montant mensuel, [comptes], compte du reste)]"""
        self.bud = []
        for k, (name, cat, amount, accs, rem) in enumerate(specs):
            b, it = uid(), uid()
            self.ins("ICBudget", ID=b, name=name, hidden=0, parent="", index=k, currency="EUR", accounts=json.dumps(accs), remainingTransactionsAccounts=json.dumps([rem]), **{"class": "ICBudget"})
            self.ins("ICBudgetItem", ID=it, budget=b, index=0, category=cat, amount="%.2f" % -amount, income=0, startDate=FIRST.isoformat(), endDate=None, duration=1, durationUnit=2)
            periods = {}
            m = FIRST
            for j in range(12):
                pid = "%s#%d" % (it, j)
                self.ins("ICBudgetItemPeriod", ID=pid, item=it, index=j, amount="%.2f" % -amount, startDate=m.isoformat(), endDate=month_end(m).isoformat())
                periods[(m.year, m.month)] = pid
                m = add_months(m, 1, 1)
            self.bud.append(dict(name=name, cat=cat, amount=amount, accs=set(accs), rem=rem, periods=periods))

    def bip_for(self, account, cat, d, amount):
        for b in getattr(self, "bud", []):
            if cat and account in b["accs"] and self.cat_in(cat, b["cat"]) and amount < 0:
                return b["periods"].get((d.year, d.month))
        return None

    def cat_in(self, c, parent):
        return c == parent or self.parents.get(c) == parent


def build(path):
    f = Fixture(path)
    cour = f.account("Courant", "CheckingAccount")
    joint = f.account("Joint", "CheckingAccount")
    livret = f.account("Livret", "SavingsAccount")
    k1 = f.account("Carte Perso", "CreditCardAccount")
    k2 = f.account("Carte Joint", "CreditCardAccount")
    k3 = f.account("Carte Claire", "CreditCardAccount")
    pret = f.account("Prêt", "LoanAccount")

    sal = f.category("Salaire", expense=0, income=1)
    loyer = f.category("Loyer")
    courses = f.category("Courses")
    loisirs = f.category("Loisirs")
    cine = f.category("Cinéma", parent=loisirs)
    assur = f.category("Assurance")
    divers = f.category("Divers")
    cantine = f.category("Cantine")
    f.parents = {cine: loisirs}

    f.budgets([("Courses", courses, 600, [joint, k2], joint), ("Loisirs", loisirs, 200, [cour, k1], cour), ("Divers", divers, 300, [cour, k1], cour)])

    # soldes de départ (validés)
    for a, v in ((cour, 2000), (joint, 500), (livret, 3000), (pret, -20000)):
        f.op(a, FIRST, "Solde initial", [(v, None)], status=K)

    def monthly(start, until=LAST):
        d = start
        while d <= until:
            yield d
            d = add_months(d, 1)

    nexts = {}

    # Salaire (le 28)
    s_sal, _ = f.schedule(cour, dt.date(2026, 1, 28), "Salaire", [(2500, sal)])
    for i, d in enumerate(monthly(dt.date(2026, 9, 28))):
        f.op(cour, d, "Salaire", [(2500, sal)], sch=s_sal if d.month in (12, 1, 2) else None, occ=8 + i)
    nexts[s_sal] = 14
    # Loyer (le 5, joint)
    s_loy, _ = f.schedule(joint, dt.date(2026, 1, 5), "Loyer", [(-1200, loyer)])
    for i, d in enumerate(monthly(dt.date(2026, 9, 5))):
        f.op(joint, d, "Loyer", [(-1200, loyer)], sch=s_loy if d >= dt.date(2026, 12, 1) else None, occ=8 + i)
    nexts[s_loy] = 14
    # Cantine (le 3, joint) : novembre laissé « prévu » alors que la date est passée
    s_can, _ = f.schedule(joint, dt.date(2026, 9, 3), "Cantine", [(-45, cantine)])
    for i, d in enumerate(monthly(dt.date(2026, 9, 3))):
        st = P if d.month == 11 else None
        f.op(joint, d, "Cantine", [(-45, cantine)], status=st, sch=s_can if (d >= dt.date(2026, 11, 1)) else None, occ=i)
    nexts[s_can] = 6
    # Virement de participation Courant -> Joint (le 1er) : deux échéances liées
    sa, (sa1,) = f.schedule(cour, dt.date(2026, 1, 1), "Virement au Joint", [(-2000, None)])
    sb, (sb1,) = f.schedule(joint, dt.date(2026, 1, 1), "Virement du Courant", [(2000, None, sa1)])
    f.db.execute("update ICScheduledTransactionSplit set linkedSplit=? where ID=?", (sb1, sa1))
    for i, d in enumerate(monthly(dt.date(2026, 9, 1))):
        f.transfer(cour, joint, d, "Virement au Joint", "Virement du Courant", 2000, sch=(sa, sb) if d >= dt.date(2026, 12, 1) else (None, None), occ=8 + i)
    nexts[sa] = nexts[sb] = 14
    # Épargne Courant -> Livret (le 2)
    ea, (ea1,) = f.schedule(cour, dt.date(2026, 1, 2), "Épargne", [(-150, None)])
    eb, (eb1,) = f.schedule(livret, dt.date(2026, 1, 2), "Épargne", [(150, None, ea1)])
    f.db.execute("update ICScheduledTransactionSplit set linkedSplit=? where ID=?", (eb1, ea1))
    for i, d in enumerate(monthly(dt.date(2026, 9, 2))):
        f.transfer(cour, livret, d, "Épargne", "Épargne", 150, sch=(ea, eb) if d >= dt.date(2026, 12, 1) else (None, None), occ=8 + i)
    nexts[ea] = nexts[eb] = 14
    # Assurance annuelle (10 mars) : aucune occurrence dans la fenêtre
    s_ass, _ = f.schedule(cour, dt.date(2025, 3, 10), "Assurance habitation", [(-360, assur)], unit=3)
    nexts[s_ass] = 2
    # Électroménager : 4 fois (oct. -> janv.)
    s_el, _ = f.schedule(cour, dt.date(2026, 10, 20), "Électroménager", [(-100, divers)], nb=4)
    for i, d in enumerate(monthly(dt.date(2026, 10, 20), dt.date(2027, 1, 20))):
        f.op(cour, d, "Électroménager", [(-100, divers)], sch=s_el if d >= dt.date(2026, 12, 1) else None, occ=i)
    nexts[s_el] = 4
    # Abonnement sur la Carte Perso (le 15)
    s_ab, _ = f.schedule(k1, dt.date(2026, 1, 15), "Café & Co abonnement", [(-9.99, loisirs)])
    for i, d in enumerate(monthly(dt.date(2026, 9, 15))):
        f.op(k1, d, "Café & Co abonnement", [(-9.99, loisirs)], sch=s_ab if d >= dt.date(2026, 12, 1) else None, occ=8 + i)
    nexts[s_ab] = 14

    # Achats par carte (inventés), débités le 4 du mois suivant
    buys = {
        k1: [(9, 6, "Librairie", -32.5, loisirs), (9, 21, "Cinéma Lux", -11, cine), (10, 8, "Restaurant", -48.2, loisirs), (10, 25, "Pharmacie", -12.4, None),
             (11, 3, "Cinéma Lux", -11, cine), (11, 9, "Concert", -45, loisirs, C)],
        k2: [(9, 10, "Marché", -84.3, courses), (9, 24, "Supermarché", -152.9, courses), (10, 9, "Supermarché", -131.15, courses), (10, 22, "Boulangerie", -18.6, courses),
             (11, 6, "Supermarché", -120.45, courses), (11, 10, "Marché", -36.8, courses, C)],
        k3: [(10, 14, "Fleuriste", -25, divers), (11, 7, "Pharmacie", -18.3, None, C)],
    }
    for card, lst in buys.items():
        for m, day, name, a, c, *st in lst:
            d = dt.date(2026, m, day)
            f.op(card, d, name, [(a, c)], status=(st[0] if st else None))
    # débits différés : le 4 du mois suivant, montant = achats du cycle (tous statuts sauf annulés)
    link = {k1: (cour, f.schedule(cour, dt.date(2026, 1, 4), "DEBIT DIFFERE", [(0, None)], sid=k1 + "-CreditCardAccount-LinkedTransaction")[0]),
            k2: (joint, f.schedule(joint, dt.date(2026, 1, 4), "DEBIT DIFFERE JOINT", [(0, None)], sid=k2 + "-CreditCardAccount-LinkedTransaction")[0]),
            k3: (joint, f.schedule(joint, dt.date(2026, 1, 4), "DEBIT DIFFERE CLAIRE", [(0, None)], sid=k3 + "-CreditCardAccount-LinkedTransaction")[0])}
    for card, (acc, sch) in link.items():
        nexts[sch] = 14
        for i, d in enumerate(monthly(dt.date(2026, 10, 4))):
            cyc_start, cyc_end = add_months(d.replace(day=1), -1), d.replace(day=1) - dt.timedelta(days=1)
            amt = -round(sum(a for (ac, dd, s, parts) in f.tx if ac == card and cyc_start <= dd <= cyc_end and s != X for a, _ in parts if a < 0), 2)
            if amt:
                f.transfer(acc, card, d, "DEBIT DIFFERE", "Transfert", amt, sch=(sch, None) if d >= dt.date(2026, 12, 1) else (None, None), occ=9 + i)

    # Prêt : 500 €/mois le 7 depuis le Joint, taux 0
    f.ins("ICLoanAccountInfo", ID=pret + "-LoanAccount", account=pret, rate="0", transfer=pret + "-LoanAccount-Transfer", linkedTransaction=pret + "-LoanAccount-LinkedTransaction", linkedTransactionAmount="-500")
    s_pr, _ = f.schedule(joint, dt.date(2026, 1, 7), "Prêt", [(0, None)], sid=pret + "-LoanAccount-LinkedTransaction")
    nexts[s_pr] = 14
    for i, d in enumerate(monthly(dt.date(2026, 9, 7))):
        f.transfer(joint, pret, d, "Prêt", "Remboursement", 500, sch=(s_pr, None) if d >= dt.date(2026, 12, 1) else (None, None), occ=8 + i)

    # Opération dont le montant est porté par l'opération (ligne de catégorie vide), comme dans certains fichiers réels
    tid = uid()
    f.ins("ICTransaction", ID=tid, account=cour, date="2026-09-20", name="Prime exceptionnelle", amount="150", status=K, occurrence=-1, index=0, useSumOfSplits=0)
    f.ins("ICTransactionSplit", ID=uid(), transaction=tid, amount=None, category=sal, index=0, ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=0, usesAccountOwners=0)
    f.tx.append((cour, dt.date(2026, 9, 20), K, [(150, sal)]))
    # Divers
    f.op(cour, dt.date(2026, 11, 8), "Erreur annulée", [(-999, divers)], status=X)
    f.op(cour, dt.date(2026, 11, 6), "Frais bancaires", [(-6.5, divers)], status=C)
    f.op(cour, dt.date(2027, 1, 15), "Impôts", [(-300, None)], status=P)
    # 50/50 du mois (avant-dernier jour) : virement Courant -> Joint et part de Claire
    f.transfer(cour, joint, dt.date(2026, 11, 29), "Virement au Compte joint", "Virement du Compte Courant", 30, cat=divers, status=C)
    f.op(joint, dt.date(2026, 11, 29), "Claire", [(30, None)], status=C)

    # Projections de budget d'iCompta (reste de l'enveloppe, dernier jour du mois) de novembre à février
    m = dt.date(2026, 11, 1)
    while m <= LAST:
        for b in f.bud:
            used = -sum(a for (ac, dd, s, parts) in f.tx if ac in b["accs"] and m <= dd <= month_end(m) and s != X for a, c in parts if a < 0 and c and f.cat_in(c, b["cat"]))
            rest = round(b["amount"] - used, 2)
            if rest > 0:
                name = "%s (%s, %s - %s)" % (b["name"], b["name"], m.strftime("%d/%m/%Y"), month_end(m).strftime("%d/%m/%Y"))
                tid = uid()
                f.ins("ICTransaction", ID=tid, account=b["rem"], date=month_end(m).isoformat(), name=name, status=P, occurrence=-1, index=0, useSumOfSplits=1)
                f.split(tid, -rest, b["cat"])
                f.tx.append((b["rem"], month_end(m), P, [(-rest, b["cat"])]))
        m = add_months(m, 1, 1)

    for s, n in nexts.items():
        f.set_next(s, n)
    f.db.commit()
    names = {cour: "Courant", joint: "Joint", livret: "Livret", k1: "Carte Perso", k2: "Carte Joint", k3: "Carte Claire", pret: "Prêt"}
    return f, names


def build_echeances(path):
    """Petit fichier fictif : échéance toutes les 4 semaines (unité iCompta 1 = semaine) et échéance dont le montant
    est porté par l'échéance elle-même (« useSumOfSplits » à 0, ligne de catégorie sans montant), comme chez Elliott."""
    f = Fixture(path)
    cour = f.account("Courant", "CheckingAccount")
    sport = f.category("Sport")
    f.op(cour, FIRST, "Solde initial", [(500, None)], status=K)
    # Salle de sport : toutes les 4 semaines depuis le 16 oct. (16/10, 13/11 validées ; 11/12, 8/1… prévues)
    s1, _ = f.schedule(cour, dt.date(2026, 10, 16), "Salle de sport", [(-29.99, sport)], unit=1)
    f.db.execute("update ICScheduledTransaction set frequency=4 where ID=?", (s1,))
    d, k = dt.date(2026, 10, 16), 0
    while d <= LAST:
        f.op(cour, d, "Salle de sport", [(-29.99, sport)], status=(K if d <= TODAY else P), sch=(s1 if d > TODAY else None), occ=k)
        d += dt.timedelta(days=28)
        k += 1
    f.set_next(s1, k)
    # Argent de poche : 220 € le 1er du mois, montant porté par l'échéance (ligne sans montant)
    s2 = uid()
    f.ins("ICScheduledTransaction", ID=s2, account=cour, date="2026-10-01", name="Argent de poche", amount="220", frequency=1, frequencyUnit=2,
          numberOfOccurrences=None, nextOccurrence=0, index=0, useSumOfSplits=0)
    f.ins("ICScheduledTransactionSplit", ID=uid(), transaction=s2, amount=None, category=None, linkedSplit=None, index=0,
          ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=0, usesAccountOwners=0)
    d, k = dt.date(2026, 10, 1), 0
    while d <= LAST:
        tid = uid()
        st = K if d <= TODAY else P
        f.ins("ICTransaction", ID=tid, account=cour, date=d.isoformat(), name="Argent de poche", amount="220", status=st,
              scheduledTransaction=(s2 if d > TODAY else None), occurrence=k, index=0, useSumOfSplits=0)
        f.ins("ICTransactionSplit", ID=uid(), transaction=tid, amount=None, category=None, linkedSplit=None, index=0,
              ignoredInBudgets=0, ignoredInReports=0, ignoredInAverageBalance=0, refund=0, usesAccountOwners=0)
        f.tx.append((cour, d, st, [(220, None)]))
        d = add_months(d, 1, 1)
        k += 1
    f.set_next(s2, k)
    f.db.commit()
    return f, {cour: "Courant"}


def truth(f, names, dates):
    """Solde de chaque compte à chaque date : somme des opérations non annulées (comme iCompta)."""
    out = {}
    for acc, nm in names.items():
        out[nm] = [round(sum(a for (ac, d, s, parts) in f.tx if ac == acc and s != X and d <= dt.date.fromisoformat(D) for a, _ in parts), 2) for D in dates]
    return out


if __name__ == "__main__":
    fx, nm = build(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fictif.cdb")
    print(json.dumps(truth(fx, nm, ["2026-11-11", "2026-11-30", "2026-12-31", "2027-01-31", "2027-02-28"]), ensure_ascii=False, indent=1))
