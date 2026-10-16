# gentooinstall

Assistant d'installation Gentoo guidé, inspiré de `setup-alpine` (Alpine Linux) et
d'`archinstall` (Arch Linux).

## Objectif

L'assistant pose une suite de questions (avec valeurs par défaut) puis automatise
l'installation complète :

- clavier, nom d'hôte, fuseau horaire, locale
- réseau (DHCP ou statique, via netifrc ou NetworkManager)
- partitionnement **simple ou avancé** (swap, `/home` séparé, LUKS, LVM)
- choix du système de fichiers (ext4, btrfs, xfs)
- téléchargement, vérification SHA256 et extraction du stage3
- sélection du profil / bureau (minimal, Xfce, GNOME, KDE Plasma)
- noyau `gentoo-kernel-bin` et initramfs adapté (LUKS/LVM)
- création d'un utilisateur non-root, mot de passe, `sudo`
- chargeur d'amorçage GRUB
- finalisation dans le chroot

## Prérequis

- Linux x86_64 avec les outils suivants :
  - `lsblk`, `parted`, `wipefs`, `mkfs.fat`, `mkfs.ext4`/`mkfs.btrfs`/`mkfs.xfs`, `blkid`
  - `wget`, `tar`, `chroot`, `openssl`
  - `cryptsetup` et `lvm2` si vous activez LUKS/LVM
- droits root
- une connexion réseau active

## Utilisation

```bash
sudo python3 gentooinstall.py            # assistant interactif
sudo python3 gentooinstall.py --dry-run  # simulation (récapitulatif seul)
```

L'assistant vous guide pas à pas. À titre d'exemple de flux :

```text
Disposition clavier (keymap) [fr] :
Nom d'hôte [gentoo] :
Fuseau horaire [Europe/Paris] :
Locale principale [fr_FR.UTF-8] :
Service réseau
  1) netifrc (OpenRC, simple) (défaut)
  2) NetworkManager
Choix [netifrc] :
Profil / bureau
  1) Minimal (console) (défaut)
  2) Xfce
  3) GNOME
  4) KDE Plasma
...
Chiffrer la racine avec LUKS ? [o/N] :
```

## Options principales

Toutes les réponses peuvent être fournies en ligne de commande pour un
fonctionnement **non interactif** (`--yes`).

- `--disk /dev/sdX` : disque cible (sinon sélection interactive).
- `--boot-mode {auto,uefi,bios}` : mode de démarrage (défaut `auto`).
- `--stage3-url URL` : URL directe du stage3.
- `--target-dir CHEMIN` : point de montage (défaut `/mnt/gentoo`).
- `--hostname`, `--keymap`, `--timezone`, `--locale` : identité du système.
- `--username` : utilisateur non-root à créer.
- `--desktop {minimal,xfce,gnome,kde}` : profil de bureau.
- `--filesystem {ext4,btrfs,xfs}` : système de fichiers racine.
- `--swap {none,partition,file}` : type de swap.
- `--separate-home` / `--no-separate-home` : partition `/home` dédiée.
- `--luks` / `--no-luks` : chiffrement LUKS de la racine.
- `--lvm` / `--no-lvm` : gestion par LVM.
- `--network-service {netifrc,networkmanager}`, `--network-mode {dhcp,static}`.
- `--ip`, `--gateway`, `--dns` : réseau statique.
- `--root-password`, `--user-password`, `--luks-passphrase` : secrets (non interactif).
- `--yes` : non interactif (accepte défauts/flags).
- `--force` : autorise l'effacement du disque hébergeant le système en cours.
- `--dry-run` : affiche le récapitulatif sans exécuter de commande.

Exemple non interactif (UEFI, Xfce, LUKS + LVM) :

```bash
sudo python3 gentooinstall.py \
  --disk /dev/nvme0n1 --boot-mode uefi --desktop xfce \
  --luks --lvm --separate-home --swap partition \
  --username bob --root-password '...' --user-password '...' --luks-passphrase '...' \
  --stage3-url https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-openrc/stage3-amd64-openrc-20261011T170049Z.tar.xz \
  --yes
```

## Points importants

- Le script **efface** le disque sélectionné.
- Il refuse d'effacer un disque qui héberge le système en cours, sauf avec `--force`.
- Le disque cible est vérifié comme périphérique bloc valide avant toute opération.
- Seul **OpenRC** est pris en charge (systemd n'est pas automatisé).
- Avec LUKS, un `/boot` non chiffré est créé automatiquement (GRUB doit pouvoir
  lire le noyau sans déchiffrer la racine).
- Les options de stockage avancé (LUKS/LVM) sont **expérimentales** : testez-les en
  machine virtuelle avant un usage réel.

## Limites actuelles

- pensé pour un environnement Gentoo standard x86_64
- seul OpenRC est pris en charge
- la signature PGP du stage3 n'est pas vérifiée (seul le SHA256 l'est)
- les chemins LUKS/LVM et le rendu du chargeur GRUB méritent une validation en VM
- les valeurs `swap_size`/`home_size` sont des défauts internes (`4G`/`50G`)

## Licence

Ce projet est livré sans licence explicite : vérifiez avant de le réutiliser dans un contexte professionnel ou public.
