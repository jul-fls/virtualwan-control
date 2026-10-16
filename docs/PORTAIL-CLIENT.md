# Portail client et migration vers la configuration en base

L’application garde une seule image, un seul port **HTTP 8080** et SQLite dans
le volume `/data`. Uvicorn sert HTTP et WebSocket ; aucun certificat, Caddy,
PostgreSQL, serveur VNC public ou compte Proxmox client n’est requis.

## Mettre à jour l’installation existante

1. Sauvegarder le **volume complet**, y compris `master.key`, SQLite et journaux.
2. Déployer la nouvelle image en conservant le même volume. Pour cette première
   exécution, les anciennes variables JSON peuvent rester : le routeur ayant le
   même ID est déjà un enregistrement de la table `profiles`. Il est conservé,
   avec ses relations IP/VM, plutôt que recréé ou écrasé. Les modèles suivent le
   même principe. Les nouveaux objets d’un ancien JSON sont importés une fois.
3. Vérifier **Routeurs**, **Stock d’IP** et **VPS clients**. Retirer ensuite
   `VWAN_PROFILES_JSON` et `VWAN_MODELS_JSON` du Compose/.env, puis recréer le
   conteneur. Ne supprimer ni le profil ni le volume. Aucun nouvel achat,
   changement pfSense ou réinstallation de VM n’est déclenché par la migration.
4. Remplacer, si utilisée, `VWAN_IPV6_PROTECTED_NETWORKS_JSON` par
   `VWAN_IPV6_PROTECTED_NETWORKS=prefixe1/60,prefixe2/64`.
5. Les variables ciblées `VWAN_PVE_HOST`, `VWAN_PFSENSE_URL`, `VWAN_IPV6_*`, etc.
   restent des overrides explicites. Les retirer si l’UI doit aussi être la
   référence pour ces champs. Les mots de passe/tokens restent dans le coffre
   ou dans leurs variables de secrets ; ne pas les déplacer dans les profils.

La migration s’exécute **dans le conteneur qui possède votre volume**. Modifier
le dépôt ne modifie pas une base distante déjà déployée. Si les routeurs sont
visibles après retrait du JSON et redémarrage, la migration est vérifiée.

## Installation neuve : variables simples

Le plus simple est de créer le routeur et les modèles dans l’administration.
Le formulaire routeur contient des champs typés groupés par section, sans JSON.
Pour un bootstrap automatisé, `.env.example` énumère tous les champs scalaires.
Exemple à adapter entièrement à l’infrastructure réelle :

```dotenv
VWAN_ROUTER_BOOTSTRAP=true
VWAN_PROFILE_ID=router-1
VWAN_ROUTER_NAME=Routeur principal
VWAN_ROUTER_READY=false
VWAN_ROUTER_DOMAIN=hosting.example.net
VWAN_ROUTER_HOST=adresse-de-management
VWAN_ROUTER_PRIMARY_IP=adresse-de-management
VWAN_PVE_HOST=adresse-de-proxmox
VWAN_PVE_NODE=nom-du-noeud
VWAN_PVE_BRIDGE=bridge-public
VWAN_PFSENSE_URL=https://firewall.example.net
VWAN_GUEST_DNS=1.1.1.1,9.9.9.9
```

Renseigner aussi les interfaces, stockages, références de secrets et autres
valeurs réelles nécessaires dans `.env.example`. Un bootstrap n’installe rien
sur l’infrastructure et ne valide pas le réseau. Après création, mettre
`VWAN_ROUTER_BOOTSTRAP=false` et gérer les routeurs supplémentaires dans l’UI.
Le bootstrap conserve les changements faits en base, même si son drapeau reste
vrai. Les listes DNS/préfixes utilisent des virgules, les booléens true/false.

## Créer les accès clients

- **Comptes clients → Créer un compte** : nom, e-mail et mot de passe initial.
  Communiquer ces accès au client par votre canal habituel ; aucun e-mail n’est
  envoyé automatiquement. L’e-mail est un identifiant, pas une adresse vérifiée.
- Affecter une VM existante dans **VPS clients → Détails → Compte client**, ou
  sélectionner le compte directement à la création du VPS.
- Le client se connecte à `https://votre-domaine/client`. Il ne voit que les VPS
  explicitement affectés. La référence commerciale libre d’un VPS ne donne
  jamais de droits par elle-même.
- Suspendre un compte bloque ses sessions, sans arrêter ses VM. Réinitialiser
  le mot de passe révoque les sessions. Une réinitialisation MFA/passkeys par
  l’admin requiert une vérification humaine d’identité préalable.

## Fonctionnement du portail

- **Alimentation** : start, shutdown ACPI, reboot invité, stop et reset matériel.
  Stop/reset demandent confirmation. Reset n’est pas une réinstallation.
- **Statut** : état réel, CPU, mémoire et disponibilité via Proxmox.
- **Adresses & DNS** : IPv4/IPv6, copie, reverse IPv4. Le client crée d’abord un
  A vers son IPv4 ; l’application vérifie cette résolution publique et le
  rattachement OVH avant modification. Le reverse IPv6 du réseau Free n’est pas
  exposé : l’application ne possède pas une délégation DNS de ce préfixe.
- **Pare-feu** : règles entrantes TCP/UDP/ICMP, sources CIDR, ports/plages,
  action et politique par défaut. Les règles administrateur restent prioritaires.
  Le contrôleur n’active pas les firewalls cluster/nœud ; si le global est arrêté,
  il refuse d’activer celui de la VM. Les états existants ne sont pas purgés.
  Une coupure en cours d’application peut laisser une génération partielle :
  relire les règles et réessayer, sans supprimer les règles de l’hébergeur.
- **Console** : noVNC local au portail, WebSocket authentifié, grant à usage unique
  valable 60 s, puis canal SSH épinglé jusqu’au socket VNC de la bonne VM. Aucune
  IP/clé de management n’est fournie au client. Une session est limitée à une
  heure et vérifiée toutes les 10 s contre révocation/changement d’affectation.
- **Rescue** : configurer `VWAN_RESCUE_ISO=iso:iso/virtualwan-rescue-debian13-amd64-v3.iso` avec une ISO
  existante disponible sur tous les nœuds concernés. Le basculement arrête automatiquement le VPS, vérifie les médias et l’ordre de démarrage, enregistre les identifiants puis le redémarre. Le retour au disque suit le même cycle.
  Les lecteurs `ide0` et `ide1` doivent être libres ou contenir les seuls médias rescue vérifiés de cette VM ; le disque cloud-init existant reste intact.
- **Personnalisation** : nom d’affichage/description propres au client ; le nom
  technique, les IP et l’identité d’isolation ne changent pas.

La console propose le plein écran, un onglet dédié ou une fenêtre séparée avec
les mêmes contrôles. Ctrl, Alt et Windows sont trois boutons indépendants à
maintien activable : un clic enfonce, un second relâche. Tab, Échap et
Ctrl+Alt+Suppr sont envoyés directement. « Relâcher les touches », la fermeture
et la perte de focus évitent de laisser un modificateur enfoncé.

Le sélecteur **Clavier du VPS** propose Français AZERTY (défaut), QWERTY US,
QWERTY UK, QWERTZ allemand et touches physiques sans adaptation. Le choix est
mémorisé par VM dans ce navigateur et retrouvé dans l’onglet dédié. Il indique
la disposition **du système invité**, pas celle du poste : si une ancienne VM
est encore en QWERTY US, choisir Anglais US permet de taper depuis un AZERTY.
Les caractères sont adaptés aux scancodes QEMU, y compris Shift/AltGr ; les
raccourcis et les relâchements de touches sont conservés. Pour une disposition
personnalisée, une méthode de saisie ou des touches mortes non standard,
utiliser le mode physique avec les mêmes réglages local/invité.

Les nouveaux clones Debian sont configurés en AZERTY français de façon
persistante. Cette mise à jour ne modifie pas les claviers des VM existantes.
Le réglage cloud-init suit [la configuration clavier Debian](https://wiki.debian.org/Keyboard).

« Préparer l’affichage graphique » demande confirmation puis exécute en
arrière-plan : arrêt ACPI propre, attente de la tâche Proxmox **et** de l’état
arrêté, suppression de serial0/configuration VGA std, vérification et démarrage.
Le suivi affiche chaque étape ; actualiser permet de retrouver une préparation
en cours. Les actions concurrentes sur ce VPS sont refusées. Un arrêt qui
échoue après 120 s ne devient jamais un arrêt forcé. Un échec de conversion
après arrêt déclenche une tentative de remise en marche si le VPS était allumé.
Un échec réel affiche une explication de l’étape et une référence de journal,
sans exposer les accès de management. Un redémarrage du contrôleur marque une
préparation en cours comme interrompue ; elle n’est pas rejouée automatiquement.

Les nouvelles VM utilisent VGA std/noVNC. Pour les anciennes VM série, lire
[la procédure du template](TEMPLATE.md). Une mise à jour du contrôleur n’arrête
aucune VM existante. Les clients SSH par clé peuvent définir eux-mêmes un mot
de passe local s’ils veulent se connecter à la session texte de la console.

## Compte, MFA et passkeys

Le client peut changer ses informations et son mot de passe, activer la MFA
TOTP par QR code ou clé manuelle, conserver dix codes de secours à usage unique,
utiliser des passkeys et révoquer les autres sessions. Les opérations de sécurité
exigent le mot de passe actuel et la MFA si active, ou une connexion par passkey
datant de moins de cinq minutes. Les codes TOTP déjà utilisés ne sont pas rejoués :
attendre le prochain code pour une nouvelle vérification sensible.

Les passkeys nécessitent une URL publique HTTPS stable, configurée par
`VWAN_PUBLIC_URL`. `http://localhost` est accepté pour les tests navigateur.
Ne changer pas de domaine public sans prévoir le réenregistrement des passkeys.
Pas d’auto-inscription, de vérification e-mail ni de réinstallation destructive
self-service dans cette version ; la remise d’accès passe par l’administrateur.

## Reverse proxy et accès

Conserver l’en-tête Host et autoriser **WebSocket** sur `/ws/console`.
L’amont est toujours `http://control:8080`. Dans Nginx Proxy Manager, activer
Websockets Support. Avec Cloudflare Tunnel, conserver le même domaine et
configurer les règles Access pour les clients autorisés. Protéger les routes
administrateur séparément ; le portail utilise `/client`, `/api/portal/*`,
`/ws/console` et les ressources `/static/*`.

Configurer `VWAN_PUBLIC_URL=https://votre-domaine` pour les cookies Secure et
contrôles d’origine. `VWAN_TRUSTED_PROXY` ne doit viser que le proxy direct réel.
L’interface reste authentifiée même derrière un proxy ou Cloudflare Access.

## Validation avant livraison réelle

Les tests automatisés couvrent les droits inter-clients, cookies/CSRF, MFA,
codes de secours, passkeys dans Chrome avec authentificateur virtuel, commandes
validées et WebSocket HTTP réel sur loopback avec un faux backend SSH. Les
parcours démo ne redémarrent aucune VM réelle et ne modifient pas le réseau.

Sur une VM de test réellement affectée, vérifier ensuite start/shutdown/reboot,
noVNC, une règle SSH avec une nouvelle connexion extérieure IPv4/IPv6, le retour
de rescue, la migration de nœud et la révocation du compte pendant une console.
Garder la console Proxmox administrateur pendant cette validation. Une console
ou un pare-feu simulé ne valide pas votre QEMU/kernel/bridge installé.

Sources officielles : [Proxmox qm / vncproxy](https://github.com/proxmox/qemu-server/blob/master/src/PVE/CLI/qm.pm),
[API des règles de pare-feu](https://github.com/proxmox/pve-firewall/blob/master/src/PVE/API2/Firewall/Rules.pm),
[noVNC](https://github.com/novnc/noVNC),
[python-fido2](https://developers.yubico.com/python-fido2/),
[génération QR SVG](https://pypi.org/project/qrcode/).

Le compte SSH Proxmox utilisé par le portail doit pouvoir exécuter les commandes
PVE sans interaction (root, ou privilèges `sudo -n` préparés par l’administrateur).
Les erreurs détaillées de connexion sont réservées au journal administrateur ;
le client reçoit une référence, sans adresse de management.

Un ISO Debian Live commun et sa configuration réseau automatique sont fournis
dans [rescue/README.md](../rescue/README.md). Il reprend le média NoCloud de
chaque VM ; le compte `rescue` accepte sa clé SSH habituelle et un mot de passe propre à chaque activation. Ce mot de passe comporte 12 caractères alphanumériques sans caractères ambigus. Il reste consultable via « Voir les identifiants rescue » tant que ce mode est actif, est chiffré dans la base et supprimé au retour au disque système. Un badge jaune Rescue identifie ce mode dans la liste et la fiche VPS.

Le live rescue reprend le FQDN technique avec `-rescue` ajouté au premier label : `vps-141951093-rescue.hosting.flusin.fr`. Ce nom est local au live ; le hostname du disque système, le DNS et le reverse ne changent pas. Cette fonction utilise l’ISO commune v3.

## Administration des VPS

Dans l’inventaire administrateur, **Administrer** ouvre `/admin/vps/<id>`.
Cet espace utilise la session admin et propose les mêmes commandes que le
client : alimentation, état, personnalisation, noVNC intégré/détaché, clavier,
préparation VGA, rescue et ses identifiants, reverse DNS et pare-feu. Il fonctionne
aussi sans compte client affecté. L’onglet **Administration** ajoute :

- Changement de gamme : choisir un modèle CPU/RAM/disque, confirmer l’arrêt
  automatique, suivre l’opération et sa reprise après interruption. Le système
  et ses données ne sont pas réinstallés depuis le template du modèle. Un VPS
  initialement arrêté reste arrêté ; un VPS allumé est redémarré.
- Downgrade : CPU et RAM diminuent ; un disque déjà plus grand est conservé.
  Aucun rétrécissement destructeur n’est tenté. Une extension agrandit le disque
  virtuel ; la partition et le système de fichiers invités peuvent encore
  nécessiter une extension dans l’OS. L’espace disponible est vérifié avant
  une extension. Le plan et le modèle sont conservés pour permettre une reprise.
- Verrouillage client par VPS, avec motif interne : bloque les API de gestion,
  noVNC et les identifiants rescue du client. Le compte et ses autres VPS restent
  utilisables. Les consoles clientes déjà ouvertes sont révoquées sous 10 s.
  Les tâches déjà soumises terminent leur opération ; le verrou n’est pas une
  annulation d’une commande déjà acceptée par Proxmox. L’admin garde son accès.
- Suspension réseau indépendante : active `link_down` sur la carte virtuelle
  sans changer sa MAC, son bridge, son VLAN ou son isolation. Elle coupe aussi
  les connexions Internet et SSH, contrairement au seul verrou du portail. La
  console admin reste utilisable. Pour retirer tous ces accès au client,
  verrouiller la gestion ET suspendre le réseau, ou arrêter le VPS.
- Démarrage automatique avec le nœud Proxmox.
- Synchronisation des notes Proxmox : nom/e-mail du compte affecté, référence
  client, VMID, gamme, ressources réelles, IPv4/IPv6, MAC, next-hop et état de
  suspension. Le bloc Virtualwan est remplacé sans effacer les notes personnelles
  ni le marqueur d’appartenance. Aucune clé, mot de passe ou jeton n’est ajouté.

Les notes sont synchronisées après provisionnement, affectation ou modification
du compte client, changement de gamme et d’état administratif. Si une opération
est déjà en cours ou si la synchronisation échoue, **Synchroniser les notes**
permet de reprendre. Dans Détails de l’inventaire, **Synchroniser les notes des
VPS** traite aussi les VPS existants ; les VM occupées restent en attente et
peuvent être relancées après la fin de leur opération. Les accès admin et client
ne sont jamais interchangeables, même si le navigateur possède les deux cookies.
Les consoles admin ont un canal et un ticket liés à la session administrateur.

Les verrouillages et déverrouillages sont historisés avec l’acteur et le motif interne. Dans Activité de l’espace administrateur, le bouton Journal ouvre les étapes détaillées de chaque opération.


## Réinstallation complète (client et administrateur)

Dans **Console & système → Réinstallation complète**, saisir les nouveaux accès
SSH (clé, mot de passe de 12 caractères minimum, ou les deux), le FQDN exact,
`REINSTALLER`, puis confirmer la suppression des données. L’administrateur dispose
du même formulaire, même sans compte client affecté. Le verrou de gestion interdit
cette action au client ; l’administrateur conserve son accès.

L’opération arrête le VPS (arrêt forcé après 60 secondes si nécessaire), prépare
une copie indépendante du disque du template du modèle actuel, remplace `scsi0`,
supprime l’ancien volume Proxmox, régénère cloud-init et redémarre automatiquement.
Elle conserve VMID, MAC, IPv4 /32, IPv6 /128 si affectée, routes, DNS/rDNS,
ressources réelles (y compris un disque conservé après downgrade), compte client,
notes, règles de pare-feu, isolation, suspension réseau et démarrage automatique.
Le système et les comptes précédents sont remplacés. Le template doit être propre
et préparé pour cloud-init, comme pour un nouveau VPS. La console reste en VGA,
avec clavier français.

Les nouvelles clés hôtes SSH sont générées au premier démarrage : vérifier leur
nouvelle empreinte en console avant de remplacer l’ancienne entrée `known_hosts`.
Le résultat « démarré » confirme le remplacement et l’état Proxmox ; cloud-init
peut encore terminer l’installation des paquets et des accès dans le système.
Conserver les nouveaux identifiants saisis : ils ne sont pas réaffichés après la
réinstallation. Le mot de passe ne passe pas en argument de commande, est chiffré
dans la base pendant une reprise, est haché dans le snippet cloud-init, puis le
secret temporaire de l’application est supprimé après succès ou annulation.

Le plan de remplacement est enregistré avant chaque étape irréversible. Après un
redémarrage de l’application ou une erreur, **Reprendre la réinstallation** utilise
le même disque neuf et les mêmes identifiants. Une erreur de démarrage ne provoque
pas un nouvel effacement. Les autres modifications, l’affectation du compte et la
libération/suppression du VPS sont bloquées jusqu’à la fin. Le client peut suivre
la progression d’une réinstallation lancée par l’admin sur son VPS. Les événements
détaillés restent dans le journal administrateur. L’annulation n’est possible
qu’avant le début de préparation du nouveau disque ; ensuite, reprendre jusqu’à
la fin. Le verrouillage client reste disponible à l’administrateur.

Périmètre actuellement pris en charge : VM BIOS avec un unique disque système
`scsi0`, média cloud-init `ide2` et snippets Virtualwan canoniques. Le template doit
être sur le même nœud que la VM et son disque doit tenir dans la capacité allouée.
Il faut assez d’espace pour préparer le nouveau disque avant de supprimer l’ancien.
Une VM migrée doit disposer d’un template compatible sur son nœud : l’application
refuse un import depuis un stockage local inaccessible. Les snapshots, disques
supplémentaires/inutilisés non reconnus, EFI/TPM, passthrough et médias additionnels
nécessitent une intervention admin préalable ; ils ne sont jamais effacés à
l’aveugle. Sortir du rescue et terminer un changement de gamme avant de réinstaller.

Il s’agit d’une réinitialisation logique du disque virtuel, sans garantie
d’effacement physique des blocs SSD, ni suppression des sauvegardes externes.

Référence : [options disque et suppression des volumes Proxmox](https://github.com/proxmox/pve-docs/blob/master/generated/qm.1-synopsis.adoc).
