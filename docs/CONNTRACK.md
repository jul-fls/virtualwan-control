# Refus TCP et pare-feu Proxmox

La politique d'isolation utilise deux tables nftables portant le même nom :

- `bridge virtualwan_isolation` conserve l'isolation L2, les identités MAC/IP, les contrôles ARP/ND et le blocage d'accès à l'hôte ; elle ne demande plus de suivi de connexion natif bridge.
- `inet virtualwan_isolation` attribue des zones conntrack distinctes à chaque VM et à chacun de ses deux passages (bridge public et fwbr), et vérifie les réponses stateful vers les sources d'administration protégées.

Pour plusieurs segments, les deux familles utilisent le nom dérivé du segment, comme auparavant pour la famille bridge. Les règles inet ne correspondent qu'aux interfaces et identités Virtualwan enregistrées. Elles ne contournent pas le pare-feu Proxmox ; ses règles DROP et son rejet des paquets INVALID restent actifs.

Un port autorisé sans service à l'écoute doit produire un refus TCP immédiat. Un port explicitement DROP doit produire un timeout. Avant cette correction, le RST d'une VM à pare-feu activé pouvait fermer la connexion lors d'un premier passage, puis être rejeté INVALID lors du passage suivant.

Prérequis : `br_netfilter` et `net.bridge.bridge-nf-call-iptables=1`, `net.bridge.bridge-nf-call-ip6tables=1`. Le service vérifie ces valeurs avant toute installation et refuse une politique qui déléguerait ses exceptions à des hooks inactifs. Il ne modifie pas ces paramètres globaux. Sur une nouvelle installation, leur activation doit être examinée avec le filtrage existant du nœud.

Chaque VM utilise par défaut la paire de zones `32768 + 2*(VMID % 16384)` et la valeur suivante. Les zones restent stables après ajout/retrait d'une autre VM et lors d'une migration. Le registre refuse les collisions, y compris entre segments. Pour des VMID en collision, définir explicitement un `conntrack_zone` pair entre 2 et 65534 dans l'identité concernée, réservé exclusivement à Virtualwan. Les autres configurations nftables/iptables du nœud ne doivent pas réutiliser ces zones ni écraser leur affectation.

Le service installe les deux tables en une transaction nftables et gère leur présence, leur rechargement et leur retrait pour les segments supprimés. Les hooks de démarrage existants continuent d'appeler le même service. Aucun redémarrage des VM n'est nécessaire à l'application ; les connexions déjà établies changent de contexte conntrack et peuvent devoir être rouvertes.

Contrôles :

```sh
/usr/local/sbin/virtualwan-isolation apply --force
nft list table bridge virtualwan_isolation
nft list table inet virtualwan_isolation
pve-firewall status
```

Tester les ports depuis un client extérieur, puis depuis une autre VM publique et une source d'administration autorisée. Vérifier séparément IPv4/IPv6, blocage L2, accès à l'infrastructure, et fonctionnement du pare-feu VM. Ne pas remplacer le rejet INVALID par un ACCEPT général.

Validation de la correction : laboratoire QEMU Debian 6.12 avec deux VM, bridge public et fwbr par VM, IPv4/IPv6, refus TCP, DROP, connexions d'administration et isolation L2. Déploiement réel validé sur le noyau Proxmox avec rechargement du service et test des refus externes et entre VM. L'absence de VM publique actuellement hébergée sur un nœud doit être distinguée d'un test de trafic réel sur ce nœud.

Après mise à jour des outils sur les nœuds, reconstruire aussi l'image de l'application avant une nouvelle opération de provisioning : une ancienne image embarque les anciennes versions des scripts et pourrait les réinstaller.
