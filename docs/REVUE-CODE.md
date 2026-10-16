# Revue du code avant publication — 16 octobre 2026

Revue locale des sources Python, des outils autonomes, des formulaires et du suivi
JavaScript : décodage des réponses externes, traitement des erreurs, validation des
entrées, authentification, journaux et cycle des opérations. Les bibliothèques
vendoriées noVNC/pako ne sont pas modifiées par cette passe.

## Bugs corrigés et non-régression

| Cas constaté dans le code | Correction et vérification |
| --- | --- |
| Retour SSH Proxmox avec octets Latin-1 mélangés à de l’UTF-8 | Décodeur commun conservant les caractères UTF-8 et les seuls octets historiques ; vérification exacte des notes, accents et Unicode. |
| Provisionnement/rescue présentant le même risque de décodage | Même décodeur pour les retours SSH et outils distants ; les scripts transférés restent autonomes. |
| Itérateur stderr Paramiko levant une erreur dans son thread | Lecture binaire des lignes puis décodage ; les erreurs du lecteur sont remontées, les événements structurés sont conservés. |
| Messages de progression avant l’UPID lors d’une suppression | Accepter la dernière ligne JSON contenant un UPID valide, sans assouplir la lecture des données JSON ordinaires. |
| Lecture de fichiers JSON/journaux dépendante de la locale du PC | Lecture UTF-8 explicite avec acceptation du BOM ; encodage UTF-8 explicite des sous-processus Python du contrôleur. |
| Deux activations MFA préparées en parallèle affichant chacune un succès | Vérifier qu’une seule activation modifie réellement le compte ; conserver le secret et les codes de la première activation. |
| Code MFA du créneau futur réutilisable juste après activation | Enregistrer le créneau réellement accepté, au lieu du créneau courant. |
| Modification partielle d’un compte désactivé le réactivant | Conserver l’état existant lorsque le champ enabled est absent. |
| Secrets partageant un préfixe partiellement masqués | Masquer du plus long au plus court, y compris la représentation encodée dans les URL. |
| SMTP_FROM absent faisant échouer un devis déjà préparé | Traiter aussi les erreurs de construction de la notification comme un échec mail indépendant du devis. |
| Erreur temporaire du polling des livraisons tuant le worker | Protéger toute l’itération et reprendre après l’attente ; test d’une itération suivante réussie. |
| Corps JSON de mauvais type, identifiants invités invalides, CSRF/MFA non ASCII | Réponses de validation/authentification prévues, avant réservation ou opération distante ; absence d’erreur serveur générique. |
| Valeur null pour un entier ou un nom, ou ready="false" activant le profil | Validation explicite des nombres/noms et du booléen ready. |
| En-tête HTTP non UTF-8 faisant planter une ouverture WebSocket | Décoder les en-têtes ASGI selon leur représentation Latin-1 ; garder le contrôle d’origine et de session. |
| Erreur de polling admin ou de déconnexion non gérée dans le navigateur | Message visible et possibilité de réessayer ; tests Chrome avec réponses 503 simulées. |
| Fin d’une réinstallation mettant à jour le panneau d’un autre VPS | Vérifier le VPS sélectionné avant d’écrire la progression. |

Les décodeurs des secrets chiffrés et des clés/base64 restent stricts : ce sont des
données d’un format connu, pas des diagnostics issus de la locale d’un serveur.
Les contrôles d’appartenance, confirmations d’achat/effacement, clés hôtes SSH et
certificats pfSense ne sont pas relâchés.

## Validation

- Suite complète unittest et tests ciblés des cas ci-dessus.
- Syntaxe JavaScript ; tests de disposition clavier et d’affichage du catalogue.
- Chrome : erreurs transitoires de suivi/déconnexion, parcours client et admin,
  réinstallation en démonstration, comptes, MFA/passkeys, pare-feu et consoles.
- Construction Docker locale et contrôle du diff ; archive publique régénérée.

Cette passe utilise des fournisseurs et accès SSH simulés, ainsi qu’une application
locale de démonstration. Elle ne commande rien et ne modifie aucune infrastructure
OVH, pfSense ou PVE. Les scénarios externes dépendent toujours de leurs versions,
configurations et réponses réelles ; la revue ne constitue pas une garantie
d’absence de tout bug.
