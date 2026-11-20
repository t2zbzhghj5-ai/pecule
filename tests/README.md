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
| `cinquante_cinquante_depuis_le_courant` | 50/50 : un livret qui vire parfois vers le joint en fin de mois n'est jamais pris pour le compte d'où part ta moitié |
| `cinquante_cinquante_sans_compte_joint` | Compte joint indiqué explicitement, jamais deviné par son nom : sans compte joint (cas de Marie), rien du 50/50 ; l'interrupteur « Compte joint » de la fiche du compte l'active |
| `compte_joint_import_et_mise_a_jour` | Import iCompta : réponse à « Un de ces comptes est-il un compte joint ? » enregistrée ; mise à jour : le compte où des dépenses 50/50 sont déjà cochées est marqué joint |
| `ventilation_part_negative` | Ventilation : part négative (bouton ±), équilibre sur la somme signée, signe conservé à la réouverture |
| `ventilation_avec_virement` | Ventilation : « + Ajouter un virement » (part vers un autre compte, dépense ou revenu, signe ±), soldes des deux comptes, compte conservé |
| `compte_joint_prorata` | Prorata des salaires : réglage, cases « Au prorata » (échéance du joint, enveloppe du joint, pas hors joint), total (annuelle lissée, rentrée déduite), deux lignes par mois, ligne validée figée, écran |
| `verifications_donnees` | Question 50/50 posée une fois sur l'accueil ; Vérifications : 50/50 sur un livret, carte sans compte débité, opération liée à un compte supprimé, ventilation fausse, livret négatif, budget périmé (« Nettoyer ») ; aucun signalement sur un fichier propre |
| `import_echeances_semaines_et_montant` | Import iCompta : échéance toutes les 4 semaines (unité iCompta 1 = semaine) et montant porté par l'échéance elle-même (« useSumOfSplits » à 0) |
| `valider_supprimer_annuler` | Valider une opération, la supprimer, annuler |
| `valider_virement` | Virement entre tes comptes : un geste valide (ou dé-valide) les deux côtés |
| `reorganiser_comptes` | Comptes › Réorganiser : cartes sous leur compte, ↑/↓ entre voisines affichées, l'accueil suit |
| `menus_comptes_dans_l_ordre` | Les menus déroulants de comptes suivent l'ordre de l'écran Comptes (chaque compte suivi de ses cartes), même si l'ordre enregistré est désordonné |
| `echeancier_choix_du_compte` | Échéancier : même menu « Choisir un compte » que l'accueil (cartes sous leur compte), choix propre à l'échéancier |
| `rentrees_et_solde_de_depart` | Accueil sans « Prochaine rentrée » ; avertissement quand on change le solde de départ (signe) |
| `icones_categories` | Icônes des catégories choisies d'après le nom, modifiables, pictogrammes à la saisie et dans les réglages |
| `etat_prevue` | État « Prévue » choisi à la main (formulaire, modification groupée), rond de statut qui la repasse en créée, comptée « à venir » dans les budgets avec le même total |
| `budgets_par_compte` | Budgets : menu « Choisir un compte » (le compte et ses cartes, ou « Tous les comptes »), prêts absents du menu, choix retenu après réouverture |
| `restant_mois_suivant` | Fin du restant « le 24 du mois suivant » (fenêtre « jusqu'au … ») : accueil = solde prévu ce jour-là, débit carte du 4 compris ; dates des mois suivants ; les autres comptes ne changent pas |
| `alerte_cinquante_cinquante` | Alerte 50/50 : bandeau sur l'accueil quand la ligne de la partenaire change, détail (dernier envoi, nouveau, retiré), message prêt à copier (presse-papiers), « C'est envoyé », plus d'alerte une fois la ligne validée |
| `previsions_variation` | Prévisions : sous le montant, l'écart avec la barre d'avant (entourée) ou avec aujourd'hui pour la première, mis à jour quand on touche une barre |
| `reglages_clarte` | Réglages (passage clarté) : carte d'état (points à regarder, sauvegarde, tout est en ordre), parties Organisation / Partage / Données, menus Compte joint et Importer |
| `accueil_debit_cartes` | Accueil (maquette K) : plus de « Prochaine rentrée » ; solde validé puis une tuile par carte du compte (débit différé, date) ; toucher une carte l'ouvre ; compte sans carte : une seule tuile |
| `comptes_solde_valide_cartes` | Écran Comptes et en-tête d'une carte : solde validé (comme les comptes), l'encours en complément |
| `echeance_date_de_fin` | Plus aucune occurrence après la date de fin (prochaine date, prévisions) ; fin visible dans l'échéancier ; au prorata, une échéance ne compte ni avant son début ni après sa fin |
| `mise_a_jour` | Même numéro dans l'app et sw.js ; « Rechercher » quand tout est à jour ; bandeau sur l'accueil et ligne des Réglages quand une version plus récente est en ligne ; « Mettre à jour » recharge ; « Nouveautés » une seule fois |
| `design_enveloppes` | Maquette R : restant dans un bloc jaune (rose sous zéro), tuiles et budgets pastel, Prévisions dans le bloc jaune ; clair et sombre lisibles |
| `accueil_a_valider_solde` | Accueil, « À valider » : chaque opération en retard affiche « puis … », du solde validé à la dernière, enchaîné avec les suivantes |
| `budgets_enveloppe` | Budgets façon enveloppe : liste avec « Il te reste à dépenser », montant changé d'un geste (ce mois-ci seulement, à partir de ce mois, à partir du mois prochain), barres des derniers mois, report, budget à plusieurs catégories, autre mois |
| `budgets_report` | Report du reste : rien n'est reporté depuis les mois d'avant Pécule ; un dépassement du mois en cours se reporte |
| `budgets_suivent_les_saisies` | Dépense par carte → budget de sa catégorie ; changer de catégorie change de budget ; rattachement périmé ignoré |
| `valider_echeance` | Valider une échéance à venir : opération créée, échéancier avancé, soldes inchangés |
| `echeance_modifiee` | Modifier une échéance : les prévisions suivent |
| `sauvegarde_aller_retour` | Télécharger la sauvegarde puis la restaurer sur une app vide : tout est identique |
| `restauration_exacte` | Restaurer depuis Réglages : retour exact à l'état sauvegardé |
| `previsions_et_themes` | Prévisions 12/24/36 mois sur chaque compte ; thèmes |
| `vrai_fichier_icompta` | (avec `--icompta`) soldes projetés = iCompta, à la date du fichier et jusqu'à l'horizon où iCompta a créé ses opérations prévues ; cartes (s'il y en a), budgets du mois, tous les écrans |

Les textes interdits dans la version installée (« Claude », « tous tes appareils »…) font aussi échouer les tests d'écrans.

Pour accéder aux calculs, le serveur de test ajoute `window.__dbg` à la page servie ; le fichier
`index.html` du dépôt n'est pas modifié.
