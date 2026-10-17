# Systèmes, templates et ISO privés

## Choix du système

Le menu **Systèmes & templates** enregistre dans la base de données les versions proposées aux clients. Les familles prévues sont Debian, Ubuntu, Fedora, AlmaLinux, Rocky Linux et FreeBSD. Une gamme continue de définir CPU/RAM/disque ; le système définit le template cloud-init et l'interface réseau invitée. Plusieurs versions et plusieurs templates par nœud peuvent être enregistrés sans variable JSON.

Le template Debian du modèle reste le choix de compatibilité par défaut. Les autres systèmes ne sont pas téléchargés ni publiés implicitement : préparer un template, lui donner un nom affiché (par exemple Ubuntu 24.04 LTS), renseigner son VMID pour chaque nœud utilisable (les VMID sont uniques dans le cluster : utiliser un identifiant différent par nœud), puis publier après validation. Un template absent sur le nœud de la VM empêche une réinstallation avant tout remplacement du disque. Retirer une version du catalogue ne modifie pas ses VPS existants. L'image choisie est figée dans le job/plan de réinstallation, y compris lors d'une reprise.

La création et les réinstallations client/admin proposent les systèmes publiés. Les adresses publiques, MAC, DNS, reverse, hostname et gamme du VPS sont conservés lors d'une réinstallation ; les accès SSH sont renouvelés. Pour une installation standard, cloud-init configure le hostname, le compte, l'IPv4 /32, sa passerelle hors sous-réseau, le DNS, le MTU, et l'IPv6 /128 si activée. Les distributions RPM utilisent wheel et leurs outils clavier ; FreeBSD utilise vtnet0, /bin/sh et sa configuration rc.conf. Aucune adresse IPv4 du réseau de transit n'est attribuée à l'invité.

FreeBSD nécessite **le cloud-init Python avec NoCloud**, pas une image contenant seulement nuageinit. Les images FreeBSD BASIC-CLOUDINIT peuvent utiliser un autre mécanisme : leur nom ne suffit pas à confirmer la compatibilité.

## Préparer un template

Sur le nœud PVE cible, partir d'une image officielle vérifiée avec les checksums publiés par son éditeur. Ne jamais prendre un disque d'un VPS client comme template. `tools/prepare-os-template.py` crée uniquement un nouveau VMID arrêté : il refuse un VMID existant et ne touche pas à l'image originale.

Exemple à adapter aux identifiants réellement libres et à l'image téléchargée :

```sh
python3 prepare-os-template.py --family ubuntu \
  --image /chemin/image-officielle.qcow2 --sha256 EMPREINTE_SHA256_VERIFIEE \
  --vmid VMID_LIBRE --storage STOCKAGE_DISQUES --bridge BRIDGE_PUBLIC
```

Pour Debian et Ubuntu, le script nécessite `virt-customize` (libguestfs-tools) et prépare une copie temporaire avec cloud-init, QGA, curl, Python 3, sudo et les outils clavier. Pour Fedora, AlmaLinux et Rocky, préparer ces paquets dans une VM démarrée nativement, puis fournir son disque nettoyé avec `--prepared-rpm` : modifier les politiques SELinux hors ligne peut endommager leurs labels. Si les filtres QGA de l'éditeur bloquent guest-exec et guest-exec-status, autoriser uniquement ces deux appels supplémentaires en conservant les autres restrictions. Si nécessaire, rendre uniquement le domaine virt_qemu_ga_t permissif avec semanage ; SELinux reste Enforcing ailleurs. Cela autorise l'administrateur PVE à exécuter des commandes dans l'invité.

Pour FreeBSD, fournir une image déjà préparée avec `--prepared-freebsd`, cloud-init Python/NoCloud (désactiver nuageinit), Python 3, curl et QGA. Vérifier les services persistants dans rc.conf.d, le PATH incluant /usr/local/bin et les permissions 0755 de /home. Le démarrage QGA doit tolérer un agent déjà actif. Vérifier le nom réel de l'interface : ens18 est courant sur Debian/Ubuntu/Fedora, eth0 sur AlmaLinux/Rocky et vtnet0 sur FreeBSD.

Avant capture d'une image préparée, retirer les comptes et clés de test, nettoyer l'état cloud-init et l'identité machine, synchroniser les disques puis arrêter proprement la VM. Vérifier séparément le checksum de l'image officielle et celui du disque préparé. Le script importe ensuite le disque et crée un template avec scsi0, NoCloud ide2, VGA AZERTY et scsi1 vide.

Le disque virtuel du template doit tenir dans la plus petite gamme proposée. Pas de disque client sur scsi1 : ce slot est réservé au CD. Le template doit permettre une connexion console VGA, démarrer cloud-init et QGA, et contenir Python 3 et curl dès le premier démarrage. Sur FreeBSD, vérifier `/usr/local/bin/python3` et le service qemu-guest-agent. Le script ne démarre pas le template sur le VLAN public et ne le publie pas automatiquement.

**Validation avant publication** : cloner sur une VM de test, appliquer les snippets générés, vérifier hostname, unique IPv4 publique /32, DNS, route vers la passerelle, IPv6 publique /128/link-local EUI-64, QGA, SSH et VGA/AZERTY ; redémarrer et vérifier la persistance. Tester chaque version de cloud-init/OS et chaque nœud concernés. Le support du code et les tests de génération ne remplacent pas cette validation des images.

Sources de référence : [réseau cloud-init](https://docs.cloud-init.io/en/25.1/reference/network-config.html), [support FreeBSD cloud-init](https://www.freebsd.org/status/report-2024-01-2024-03/cloud-init/), [images officielles FreeBSD](https://download.freebsd.org/releases/VM-IMAGES/).

## ISO client et lecteur permanent

Dans **Console & système**, chaque VPS possède son propre emplacement ISO : **un fichier au maximum**, taille maximale **10 000 000 000 octets (10 Go)**, envoi incomplet compris. Le fichier existant doit être supprimé avant remplacement ; éjecter conserve le fichier et continue de compter dans le quota. Un envoi interrompu peut être repris en rechoisissant le même fichier. Un transfert interrompu pendant la copie vers PVE est marqué en échec ; supprimer ce média avant de recommencer.

Les envois passent par des requêtes JSON de 512 Kio de données binaires au maximum, pour conserver la limite HTTP existante et éviter un POST de 10 Go. Le backend impose quota, offset, base64 strict, propriété du VPS, session et CSRF sur chaque morceau. Le fichier est d'abord privé dans le volume applicatif, contrôlé ISO9660/UDF et SHA256, puis transféré sous un nom généré vers PVE. Son SHA256 est revérifié sur PVE. Le temporaire applicatif est supprimé après succès. Prévoir jusqu'à 10 Go temporaires par VPS en cours d'envoi, et la place ISO correspondante sur PVE. L'application ne publie ni URL publique de téléchargement, ni galerie des ISO d'autres clients.

Le lecteur **scsi1** est distinct de ide0/ide1 (rescue) et ide2 (cloud-init). Les opérations disponibles côté client et admin sont :

- Insérer : présentation du CD à chaud, sans reboot ni effacement du disque.
- Éjecter : lecteur vidé, fichier conservé.
- Prochain boot sur ISO : insertion et priorité scsi1;scsi0, sans redémarrer maintenant. Cette priorité reste jusqu'à éjection ; après installation, éjecter pour retrouver le boot disque.
- Supprimer : éjection puis suppression du seul fichier de ce VPS, y compris un transfert partiel.

Les clones et réinstallations préparent un lecteur scsi1 vide. L'insertion ajoute aussi ce lecteur aux anciennes VM si leur contrôleur SCSI/hotplug l'autorise ; si un disque ou média administrateur occupe le slot, l'outil refuse de l'écraser. Si PVE laisse un changement en attente plutôt que de l'appliquer à chaud, l'UI signale cette situation et ne redémarre jamais implicitement la VM.

Une réinstallation **ISO personnalisé** exige un ISO prêt : arrêt automatique, disque système neuf vierge, boot ISO puis démarrage. Les comptes, le réseau et l'installation sont alors manuels, car un installateur arbitraire ne comprend pas forcément cloud-init. Les paramètres publics à saisir sont affichés à côté du lecteur. Une création ISO personnalisée prépare une VM arrêtée avec un disque vierge ; envoyer l'ISO depuis sa fiche, choisir le prochain boot ISO puis démarrer. Le pare-feu et l'isolation restent actifs.

Une réinstallation standard éjecte le CD client, mais conserve le fichier privé pour réutilisation. Supprimer un VPS supprime son ISO ; libérer seulement l'IP garde la VM et son média. Un compte ne peut consulter ni manipuler les médias d'un VPS qui ne lui appartient pas, et le verrouillage de gestion client bloque ces actions.

## Configuration

```dotenv
# Facultatif ; sinon, stockage snippet_storage du profil.
VWAN_CLIENT_ISO_STORAGE=iso
# Copies simultanées de l'application vers PVE, de 1 à 8.
VWAN_CLIENT_ISO_TRANSFERS=2
```

Utiliser un stockage ISO partagé entre nœuds pour les VM migrables. Le répertoire doit être un stockage PVE déclarant le contenu iso. Une migration vers un nœud qui ne voit pas le stockage local provoque un refus explicite, jamais un accès à un fichier d'un autre client. Conserver le volume `/data` du compose ; ne pas y stocker les envois dans le tmpfs `/tmp`. Aucun reverse proxy ou serveur HTTPS supplémentaire n'est nécessaire dans l'image.

## Vérifications réalisées pendant le développement

- Tests API client/admin : appartenance, CSRF, blocage de gestion, quota, un seul emplacement, offsets, reprise, ISO invalide, choix d'OS et conservation d'identité.
- Génération cloud-init pour les six familles et sonde réseau FreeBSD.
- Suite Python complète et syntaxe JavaScript ; écrans catalogue/création/réinstallation vérifiés en démonstration locale.
- Test réel scsi1 sur deux VM existantes : transfert SHA256 d'un ISO de contrôle, insertion/éjection, ordre de boot conservé et PID inchangé ; ISO de test supprimé ensuite. Lecteur vide aussi préparé dans le template Debian existant.
- Les contrôles de génération ne remplacent pas les essais réels de chaque image sur les nœuds cibles avant publication.
