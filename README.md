# Virtualwan Control

**Apache-2.0 · Self-hosted · OVHcloud + pfSense + Proxmox + Cloudflare**

Une console privée pour provisionner des VM clientes avec une IPv4 publique
dédiée, routée depuis un VPS vers votre infrastructure par WireGuard, sans
NAT de l'adresse cliente. Le réseau de transit privé reste côté pfSense ;
l'invité Debian reçoit uniquement son IPv4 publique /32 via Cloud-init.

![Interface en mode démonstration](docs/images/overview.png)

Le code de ce dossier est générique et destiné à devenir la racine du dépôt
public. Les documents d'exploitation du dossier parent et ses données
personnelles ne font pas partie de la distribution.

## Ce que fait l'application

- Inventaire des routeurs OVH, IP disponibles/réservées/affectées et VM ;
  affichage public /32, MAC, prochain saut ARP privé, VMID, nœud, hostname.
- Synchronisation des IP possédées/routées depuis OVH, lecture des VM
  enregistrées sur Proxmox, import d'une installation existante.
- Modèles nommés et gammes : CPU, RAM, disque, template Proxmox ; ces paramètres
  sont figés au lancement d'un déploiement.
- Création depuis une IP en stock : réservation atomique, VM arrêtée, DNS A
  Cloudflare DNS-only, PTR OVH, routes VPS/pfSense, Kea/ARP permanent, isolation,
  démarrage et contrôles. Les méthodes SSH clé/mot de passe/les deux existent.
- Achat d'IP en avance, ou achat d'une IP suivi d'une VM avec clé SSH dans le
  même parcours. Le stock doit rester routé vers un service OVH.
- Duplication d'un profil de routeur au plafond des 16 IP : nouveau segment,
  nouveau service, alias distinct, devis de VPS soumis à approbation. Les
  nouvelles IP se commandent une fois ce nouveau service livré.
- Confirmation humaine de tout achat : prix/contrats du devis OVH, expiration,
  phrase de confirmation, paiement automatique facultatif. Notification SMTP
  possible ; le lien ouvre une interface authentifiée et n'approuve rien.
- Journaux par opération, coffre de secrets chiffré, états d'échec/reprise.

Les adaptateurs de provisionnement supportent aujourd'hui OVH, Cloudflare,
pfSense WebGUI/Kea et Proxmox. Les identifiants, interfaces, subnets, stockages,
templates, DNS et paramètres SSH sont configurables. « Générique » ne signifie
pas compatibilité déjà implémentée avec tous les fournisseurs/hyperviseurs.
Le mode fournisseur manuel gère l'inventaire ; DNS/PTR automatiques du
provisionnement restent ceux d'OVH/Cloudflare dans cette version.

## Démarrage Docker sur votre VM de management

Prérequis : Docker Engine et le plugin Compose. Aucun déploiement automatique
sur votre hôte n'est déclenché par ce dépôt.

```sh
cp .env.example .env
# Personnaliser VWAN_ADMIN_PASSWORD avec un mot de passe long et unique.
docker compose up -d --build
docker compose logs -f control
```

Par défaut : écoute **127.0.0.1:8080** sur la VM, achat et provisionnement
réels désactivés. Pour accéder depuis votre poste sans exposer la console :

```sh
ssh -L 8080:127.0.0.1:8080 utilisateur@VM_MANAGEMENT
```

Ouvrir `http://localhost:8080`, utilisateur `admin`, mot de passe de `.env`.
L'app démarre avec un inventaire vide en mode réel. Le volume `control-data`
contient la base, la clé du coffre, les clés hôtes SSH et les journaux.
Sauvegarder le volume chiffré ; la base seule ne suffit pas pour restaurer les
secrets, et la base + sa clé permettent de les déchiffrer.

### HTTPS sur le réseau de management

Le supplément `compose.https.yaml` fournit Caddy avec une CA interne.
Configurer dans `.env` : `VWAN_CONTROL_HOST=control.example.net` et
`VWAN_HTTPS_BIND_ADDRESS=IP_MANAGEMENT`. Faire résoudre ce nom vers la VM,
installer le certificat racine Caddy sur vos postes, puis :

```sh
docker compose -f compose.yaml -f compose.https.yaml up -d --build
```

L'interface utilise alors des cookies Secure. Un reverse proxy HTTPS existant
peut être utilisé à la place, avec `VWAN_PUBLIC_URL` égal à l'URL extérieure.
Garder l'accès sur le réseau de management ; ceci n'est pas un portail client.
Ne pas monter le socket Docker ni donner au conteneur le mode privileged.

### Démonstration sans infrastructure ni achats

Dans un répertoire/projet Compose et un volume **distincts**, mettre
`VWAN_DEMO=true`. Des routeurs, modèles et IP de documentation sont créés.
Le bandeau et les résultats signalent explicitement la simulation. Les prix
de démonstration ne sont pas les prix OVH. Aucune API externe n'est appelée.
Le même volume ne peut pas être basculé entre modes démo et réel.

## Brancher l'infrastructure existante

1. Routeurs → Ajouter : éditer l'exemple JSON, renseigner les interfaces
   internes pfSense réelles, bridge, nœud, stockages et template PVE, domaine,
   endpoint API OVH/filiale et nom du service. Les valeurs de l'exemple sont
   documentaires : elles ne découvrent ni n'installent votre réseau.
2. Paramètres → Secrets : ajouter les valeurs sous les noms `*_env` utilisés
   par le profil. Secrets distincts possibles pour chaque routeur/compte.
   La clé privée SSH, si utilisée, se monte sous `/data` ; son chemin dans le
   profil est relatif à data. Les mots de passe sont aussi pris en charge.
3. Configurer une CA ou une empreinte SHA-256 du certificat pfSense. Vérifier
   et enregistrer les empreintes SSH via le profil, en les comparant hors bande.
4. Le réseau doit déjà avoir le tunnel, le scope Kea/ARP, les gateways et règles
   de routage ainsi que l'isolation des clients. Le preflight contrôle ces
   prérequis et refuse d'improviser une configuration physique.
5. Synchroniser les IP OVH et Lire les VM sur le routeur. Le primaire ne rejoint
   jamais le stock. Les IP manquantes sont mises en quarantaine, pas supprimées.
6. Configurer les modèles et activer `VWAN_ENABLE_PROVISIONING=true` après
   validation. Redémarrer le service pour appliquer les variables d'environnement.

L'import CLI `tools/import-legacy.py` accepte une configuration du CLI Virtualwan
et un registre d'isolation, ainsi qu'un JSON de secrets et un known_hosts
optionnels. **Arrêter l'app avant l'import**. Il ne contacte pas les serveurs,
n'achète rien et marque les VM importées comme à vérifier. Voir
[la migration](docs/MIGRATION.md).

## Acheter et affecter

Configurer les droits API supplémentaires dans [OVH-API.md](docs/OVH-API.md),
puis mettre `VWAN_ENABLE_PURCHASES=true`. Sans cette variable, un devis peut
être préparé mais aucun checkout réel ne peut être envoyé.

1. Stock d'IP → Acheter, choisir le routeur, charger le catalogue OVH, choisir
   explicitement le plan Additional IP, la quantité, la durée et sa tarification.
   Les configurations requises sont celles de l'offre OVH ; aucun SKU/prix/datacenter
   n'est codé en dur. Un devis incomplet affiche les champs requis et n'achète rien.
2. Pour créer également la VM, cocher la suite du parcours, choisir le modèle
   et fournir la clé SSH publique. Le modèle est conservé dans le devis.
3. Achats → Examiner : vérifier toutes les lignes, les taxes et les contrats.
   Saisir `COMMANDER <référence>` et accepter les contrats. Le backend recontrôle
   les prix et la version du profil ; un changement impose un nouveau devis.
4. Le paiement automatique avec le moyen préféré OVH est un choix explicite.
   Sinon, terminer le paiement via le lien de commande OVH.
5. Le worker suit la livraison toutes les cinq minutes (ou à la demande).
   Il n'affecte que les IP liées sans ambiguïté aux lignes de cette commande,
   réellement livrées et retrouvées dans l'inventaire du bon routeur. Si OVH ne
   fournit pas cette correspondance, réconcilier manuellement le stock ; aucune
   adresse n'est devinée à partir d'un simple différentiel global.

Une interruption après checkout donne **Réponse incertaine** : comparer les
commandes OVH et ne pas recommander aveuglément. Les achats ne sont jamais
repris automatiquement. Un échec de déploiement garde l'IP en quarantaine,
avec reprise explicite du même run et des ressources déjà enregistrées.

## Au plafond : un routeur supplémentaire

Les IP en stock et celles des commandes en attente comptent dans la capacité.
OVH VPS limite actuellement celle-ci à 16 Additional IP ; les prévisions
locales ne remplacent pas les contrôles du fournisseur.

Routeurs → Dupliquer : nouveau réseau de transit sans chevauchement ; le profil
reste non prêt, ses IP/service et interfaces sont remis à renseigner, et ses
alias/segment/secret SSH sont distincts. Préparer le devis VPS depuis ce profil
vide, puis confirmer humainement. Une livraison identifiable renseigne son
service et son IP primaire ; l'état reste **Configuration requise**.

Télécharger le dossier « Préparer le réseau » : configuration du routeur et
fragment de registre multi-segment, outils Linux et procédure d'onboarding.
L'opérateur vérifie VLAN, uplink, MAC pfSense, /30 du tunnel et interfaces.
L'achat d'un serveur ne suffit pas à autoriser un remplacement aveugle du réseau.
Les changements initiaux pfSense/PVE ne sont pas appliqués automatiquement dans
cette version. Ensuite, les IP/VM du segment préparé se provisionnent par l'UI.
La limite est paramétrable à la baisse ; l'app ne commande jamais un nouveau
routeur à votre insu lorsque le seuil est atteint.

## Architecture et périmètre

Flask/Gunicorn, UI HTML/CSS/JS sans dépendances frontend externes, SQLite,
un worker de jobs. Utiliser **un seul processus / une seule instance** ; la
reprise et les verrous ne constituent pas une architecture multi-controller.
Pas de facturation client, suppression automatique, HA de la console ou portail
de clients dans cette version. Garder PVE/QEMU à jour ; l'isolation réseau ne
garantit pas contre une sortie de VM exploitant une faille d'hyperviseur.

Le service d'isolation multi-segment conserve la compatibilité avec le registre
historique plat. Pour plusieurs routeurs dans un même cluster, créer un bridge
et une table par segment ; valider les chemins tap/fwpr/fwbr, reboot et migration
sur votre matériel avant admission. Aucune mise à jour du service de production
existant n'est faite par le lancement de la console.

## Développement / tests

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Tests locaux : allocation concurrente, capacité/stock, profil dupliqué,
approbation/montant/expiration, timeout de paiement, secret chiffré, sessions,
CSRF et protection du mode démo. Ils ne constituent pas des achats ou des
déploiements réels. Le déploiement CLI historique a été testé séparément ;
les nouveaux chemins d'achat/onboarding nécessitent une recette fournisseur.
Voir [VALIDATION.md](docs/VALIDATION.md) et [SECURITY.md](SECURITY.md).

Licence : [Apache-2.0](LICENSE). Les dependencies conservent leurs licences.
Publier uniquement le contenu distribuable de ce dossier ; jamais son data,
le fichier `.env` ou le dossier parent d'exploitation. Voir le script
`tools/export-source.py` pour créer et contrôler une archive publique.

Sources : [limite Additional IP OVH](https://www.ovhcloud.com/fr/vps/vps-ip/),
[schéma Order API officiel](https://eu.api.ovh.com/1.0/order.json),
[API /me/order](https://eu.api.ovh.com/1.0/me.json),
[nftables bridge](https://wiki.nftables.org/wiki-nftables/index.php/Bridge_filtering),
[Cloud-init Proxmox](https://pve.proxmox.com/pve-docs/chapter-qm.html#qm_cloud_init),
[Gunicorn](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/).
