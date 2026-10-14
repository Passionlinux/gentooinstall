# gentooinstall

Assistant d’installation Gentoo inspiré du style d’Archinstall.

## Objectif

Ce script automatise la majeure partie d’une installation Gentoo sur un disque cible :

- sélection du disque
- détection ou choix du mode de démarrage (UEFI ou BIOS/legacy)
- partitionnement compatible avec le mode sélectionné
- formatage des partitions
- montage dans /mnt/gentoo
- téléchargement d’un stage3
- extraction du système
- génération du fichier fstab
- création du script de chroot final

## Prérequis

- Linux avec les outils suivants disponibles :
  - lsblk
  - parted
  - wipefs
  - mkfs.fat
  - mkfs.ext4
  - blkid
  - wget
  - tar
  - chroot
- droits root
- une connexion réseau active

## Utilisation

1. Vérifiez que le disque cible est bien le bon disque.
2. Lancez le script en root :

```bash
sudo python3 gentooinstall.py
```

3. Suivez les prompts interactifs.
4. Copiez l’URL du stage3 Gentoo depuis les mirrors officiels.
5. Une fois la configuration faite, le script entre dans le chroot et lance la finalisation automatique.

## Points importants

- Le script efface le disque sélectionné.
- Il supporte désormais le mode UEFI et le mode BIOS/legacy.
- Il est recommandé de vérifier plusieurs fois le disque et le nom du périphérique avant de confirmer.
- Les commandes sont exécutées avec un risque de destruction si le disque cible est mal choisi.

## Exemple de flux

```text
=== Assistant d'Installation Gentoo (Style Archinstall) ===

Disques disponibles :
  1) /dev/sda
  2) /dev/nvme0n1
Choisissez le numéro du disque cible : 1
ATTENTION : Toutes les données sur /dev/sda seront effacées ! Continuer ? (oui/non) : oui
```

## Limites actuelles

- le script est pensé pour un environnement Gentoo standard et une machine x86_64
- la gestion des variantes OpenRC/systemd est encore limitée côté automatisation
- certaines étapes peuvent nécessiter une vérification manuelle selon la machine cible
- la configuration GRUB BIOS/UEFI est adaptée au mode choisi, mais il reste conseillé de vérifier le résultat final avant le reboot

## Licence

Ce projet est livré sans licence explicite : vérifiez avant de le réutiliser dans un contexte professionnel ou public.
