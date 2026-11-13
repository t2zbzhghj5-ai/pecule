# Batterie de tests de Pécule

À lancer **avant chaque mise à jour** de l'app. Elle ouvre Pécule dans un navigateur automatisé
(Chromium), au 11 novembre 2026, et vérifie l'app de bout en bout.

```
pip install playwright && playwright install chromium   # une seule fois
python3 tests/run_tests.py                              # tout, avec le fichier iCompta fictif
python3 tests/run_tests.py -k cartes                    # seulement les tests dont le nom contient « cartes »
python3 tests/run_tests.py --icompta /chemin/fichier.cdb  # en plus : comparaison avec un vrai fichier iCompta
```

Aucune donnée personnelle ici : `fixture_icompta.py` fabrique un fichier iCompta **inventé**
(Courant, Joint, Livret, trois cartes à débit différé, un prêt, échéances, budgets, 50/50 avec « Claire »)
à partir de la seule structure des tables (`icompta-schema.sql`). Un vrai fichier passé avec `--icompta`
reste sur la machine ; rien n'est écrit dans le dépôt (`tests/out/` est ignoré par Git).

## Ce qui est vérifié

| Test | Vérifie |
|---|---|
| `import_fictif_soldes` | Import « opérations en cours » : solde projeté de chaque compte = iCompta, à 5 dates |
| `import_fictif_historique` | Import complet : mêmes soldes |
| `import_fictif_details` | Échéance annuelle hors fenêtre, échéance limitée, prévue passée, annulée, ponctuelle, catégories mixtes |
| `budgets_reste` | Reste de chaque budget = projection d'iCompta, de novembre à février |
| `cartes_coherence` | Validé + à valider + prévus = prochain débit ; même montant sur l'accueil et dans « Choisir un compte » |
| `ecrans_iphone` | Tous les onglets, écrans et fenêtres sur iPhone : aucune erreur, aucun NaN, pas de débordement |
| `ecrans_mac_sombre` | Idem sur grand écran, thème sombre, menu latéral |
| `saisie_assistant` | Bouton + : dépense, revenu, virement ; catégorie et soldes |
| `saisie_texte_et_suggestions` | Texte collé « Boulangerie 4,20 € hier » ; libellés proposés dès la 1re lettre |
| `saisie_echeance` | Bouton + › Échéance : création et occurrences |
| `cinquante_cinquante` | 50/50 ajouté au virement et à l'opération de la partenaire ; décocher ; mois suivant en prévu |
| `valider_supprimer_annuler` | Valider une opération, la supprimer, annuler |
| `valider_virement` | Virement entre tes comptes : un geste valide (ou dé-valide) les deux côtés |
| `reorganiser_comptes` | Comptes › Réorganiser : cartes sous leur compte, ↑/↓ entre voisines affichées, l'accueil suit |
| `menus_comptes_dans_l_ordre` | Les menus déroulants de comptes suivent l'ordre de l'écran Comptes (chaque compte suivi de ses cartes), même si l'ordre enregistré est désordonné |
| `rentrees_et_solde_de_depart` | Rentrées du même jour additionnées ; avertissement quand on change le solde de départ (signe) |
| `icones_categories` | Icônes des catégories choisies d'après le nom, modifiables, pictogrammes à la saisie et dans les réglages |
| `budgets_suivent_les_saisies` | Dépense par carte → budget de sa catégorie ; changer de catégorie change de budget ; rattachement périmé ignoré |
| `valider_echeance` | Valider une échéance à venir : opération créée, échéancier avancé, soldes inchangés |
| `echeance_modifiee` | Modifier une échéance : les prévisions suivent |
| `sauvegarde_aller_retour` | Télécharger la sauvegarde puis la restaurer sur une app vide : tout est identique |
| `restauration_exacte` | Restaurer depuis Réglages : retour exact à l'état sauvegardé |
| `previsions_et_themes` | Prévisions 12/24/36 mois sur chaque compte ; thèmes |
| `vrai_fichier_icompta` | (avec `--icompta`) soldes projetés à +13 mois, cartes, budgets du mois, tous les écrans |

Les textes interdits dans la version installée (« Claude », « tous tes appareils »…) font aussi échouer les tests d'écrans.

Pour accéder aux calculs, le serveur de test ajoute `window.__dbg` à la page servie ; le fichier
`index.html` du dépôt n'est pas modifié.
