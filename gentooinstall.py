#!/usr/bin/env python3
import argparse
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

TARGET_DIR = Path("/mnt/gentoo")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Assistant d'installation Gentoo")
    parser.add_argument("--disk", help="Chemin du disque cible, ex. /dev/sda")
    parser.add_argument("--boot-mode", choices=["auto", "uefi", "bios"], default="auto", help="Mode de démarrage ciblé")
    parser.add_argument("--init", choices=["openrc", "systemd"], default="openrc", help="Système d'init à installer")
    parser.add_argument("--stage3-url", help="URL directe du stage3 Gentoo")
    parser.add_argument("--target-dir", default="/mnt/gentoo", help="Point de montage cible")
    parser.add_argument("--yes", action="store_true", help="Confirmer sans prompt interactif")
    parser.add_argument("--dry-run", action="store_true", help="Affiche la planification sans exécuter les commandes")
    return parser.parse_args(argv)


def run(cmd, *, check=True, dry_run=False):
    print(f"\n[Exécution] {cmd}")
    if dry_run:
        print("[SIMULATION] commande non exécutée.")
        return 0
    result = subprocess.run(cmd, shell=True, executable="/bin/bash")
    if check and result.returncode != 0:
        raise RuntimeError(f"La commande a échoué ({result.returncode}) : {cmd}")
    return result.returncode


def get_disks():
    try:
        output = subprocess.check_output(
            ["lsblk", "-d", "-n", "-o", "NAME,TYPE"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    disks = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        name, disk_type = parts
        if disk_type == "disk":
            disks.append(name)
    return disks


def partition_suffix_for_device(device_path):
    lower = device_path.lower()
    if "nvme" in lower or "mmcblk" in lower:
        return "p"
    return ""


def build_partition_paths(target_disk):
    suffix = partition_suffix_for_device(target_disk)
    if suffix:
        return f"{target_disk}{suffix}1", f"{target_disk}{suffix}2"
    return f"{target_disk}1", f"{target_disk}2"


def choose_boot_mode(is_uefi):
    return "uefi" if is_uefi else "bios"


def validate_stage3_url(url):
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("L'URL du stage3 est invalide.")

    if not re.search(r"stage3.*\.tar\.(?:xz|bz2|gz)", parsed.path, flags=re.IGNORECASE):
        raise ValueError("L'URL ne semble pas pointer vers un stage3 tar.xz/tar.bz2/tar.gz valide.")

    return url


def ensure_root():
    if os.geteuid() != 0:
        print("Ce script doit être exécuté en tant que root.")
        raise SystemExit(1)


def ensure_clean_target(target):
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        print(f"Le dossier {target} existe et n'est pas vide. Vérifiez-le avant de poursuivre.")
        raise SystemExit(1)


def write_fstab(part_root, mount_root, boot_mode, part_boot=None):
    etc_dir = mount_root / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)

    uuid_root = subprocess.check_output(
        ["blkid", "-s", "UUID", "-o", "value", part_root],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()

    lines = [
        "# /etc/fstab: Static file system information.\n",
        f"UUID={uuid_root}\t/\text4\tnoatime\t0 1\n",
    ]

    if boot_mode == "uefi":
        uuid_boot = subprocess.check_output(
            ["blkid", "-s", "UUID", "-o", "value", part_boot],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        lines.append(f"UUID={uuid_boot}\t/efi\tvfat\tdefaults\t0 2\n")

    target_fstab = etc_dir / "fstab"
    target_fstab.write_text("".join(lines))


def write_binhost_config(mount_root):
    conf_dir = mount_root / "etc" / "portage" / "binrepos.conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    (conf_dir / "gentoo.conf").write_text(
        "[gentoobinhost]\n"
        "priority = 10\n"
        "sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/17.1/x86-64\n"
    )

    make_conf = mount_root / "etc" / "portage" / "make.conf"
    line = 'EMERGE_DEFAULT_OPTS="${EMERGE_DEFAULT_OPTS} --getbinpkg"\n'
    if make_conf.exists():
        existing = make_conf.read_text()
        if "--getbinpkg" not in existing:
            with make_conf.open("a", encoding="utf-8") as f:
                f.write("\n" + line)
    else:
        make_conf.write_text(line)


def build_chroot_script(init_type, boot_mode, target_disk):
    if init_type == "systemd":
        init_block = "# systemd is selected by default in Gentoo when the stage3 target uses it"
    else:
        init_block = "# OpenRC remains the default in Gentoo stage3 archives"

    if boot_mode == "uefi":
        grub_line = "grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo"
    else:
        grub_line = f"grub-install --target=i386-pc {target_disk}"

    return f'''#!/bin/bash
set -e
source /etc/profile
export PS1="(chroot) $PS1"

{init_block}

echo "Mise à jour de @world (synchro emerge)..."
emerge-webrsync
emerge --sync

echo "Récupération des clés OpenPGP pour les paquets binaires..."
getuto

echo "Installation du noyau précompilé (gentoo-kernel-bin)..."
emerge --oneshot sys-kernel/gentoo-kernel-bin

echo "Installation de l'environnement XFCE (xfce-base/xfce4-meta)..."
emerge xfce-base/xfce4-meta

echo "Configuration du fuseau horaire..."
echo "Europe/Paris" > /etc/timezone
emerge --config sys-libs/timezone-data

echo "Configuration des locales..."
sed -i 's/#en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
sed -i 's/#fr_FR.UTF-8 UTF-8/fr_FR.UTF-8 UTF-8/' /etc/locale.gen
locale-gen
eselect locale set fr_FR.UTF-8

env-update && source /etc/profile

echo "Configuration du nom d'hôte..."
echo "gentoo-pc" > /etc/hostname

echo "Définition du mot de passe root..."
passwd

echo "Installation de GRUB..."
emerge --oneshot sys-boot/grub
{grub_line}
grub-mkconfig -o /boot/grub/grub.cfg

echo "Installation terminée avec succès !"
'''


def main(argv=None):
    args = parse_args(argv)
    ensure_root()

    dry_run = args.dry_run
    target_dir = Path(args.target_dir)
    if dry_run:
        print("=== Mode simulation Gentoo install (dry-run) ===")
        print(f"Disque cible : {args.disk or 'auto-detect'}")
        print(f"Mode boot : {args.boot_mode}")
        print(f"Init : {args.init}")
        print(f"Stage3 : {args.stage3_url or 'à fournir'}")
        print(f"Point de montage : {target_dir}")
        print("Aucune commande réelle n'a été exécutée.")
        return

    print("=== Assistant d'Installation Gentoo (Style Archinstall) ===\n")

    disks = get_disks()
    if not disks:
        print("Aucun disque détecté via lsblk. Vérifiez votre système avant de relancer le script.")
        raise SystemExit(1)

    if args.disk:
        target_disk = args.disk
        if not target_disk.startswith("/dev/"):
            target_disk = f"/dev/{target_disk}"
    else:
        print("Disques disponibles :")
        for index, disk in enumerate(disks, start=1):
            print(f"  {index}) /dev/{disk}")

        while True:
            try:
                raw_choice = input("Choisissez le numéro du disque cible : ")
                choice_index = int(raw_choice) - 1
                if 0 <= choice_index < len(disks):
                    break
                print("Choix invalide. Réessayez.")
            except ValueError:
                print("Veuillez saisir un nombre valide.")

        target_disk = f"/dev/{disks[choice_index]}"

    print(f"Disque sélectionné : {target_disk}")

    if not args.yes:
        confirm = input(f"ATTENTION : Toutes les données sur {target_disk} seront effacées ! Continuer ? (oui/non) : ")
        if confirm.strip().lower() != "oui":
            print("Annulation.")
            return

    detected_uefi = os.path.isdir("/sys/firmware/efi")
    if args.boot_mode == "auto":
        boot_mode = choose_boot_mode(detected_uefi)
    else:
        boot_mode = args.boot_mode

    print("\n--- Choix du mode de démarrage ---")
    print(f"Mode sélectionné : {boot_mode.upper()}")

    print("\n--- Partitionnement du disque ---")
    if boot_mode == "uefi":
        part_boot, part_root = build_partition_paths(target_disk)
        run(f"wipefs -a {target_disk}")
        run(f"parted -s {target_disk} mklabel gpt")
        run(f"parted -s {target_disk} mkpart ESP fat32 1MiB 512MiB")
        run(f"parted -s {target_disk} set 1 boot on")
        run(f"parted -s {target_disk} mkpart primary ext4 512MiB 100%")
        run(f"mkfs.fat -F 32 {part_boot}")
    else:
        suffix = partition_suffix_for_device(target_disk)
        part_root = f"{target_disk}{suffix}1"
        part_boot = None
        run(f"wipefs -a {target_disk}")
        run(f"parted -s {target_disk} mklabel msdos")
        run(f"parted -s {target_disk} mkpart primary ext4 1MiB 100%")

    run(f"mkfs.ext4 -F {part_root}")

    print("\n--- Montage des partitions ---")
    target_dir.mkdir(parents=True, exist_ok=True)
    run(f"mount {part_root} {target_dir}")
    if boot_mode == "uefi":
        (target_dir / "efi").mkdir(parents=True, exist_ok=True)
        run(f"mount {part_boot} {target_dir / 'efi'}")

    init_type = args.init

    print("\n--- Téléchargement du Stage 3 ---")
    if args.stage3_url:
        stage3_url = args.stage3_url
    else:
        print("Veuillez vous rendre sur https://www.gentoo.org/downloads/mirrors/ pour copier le lien direct du Stage 3.")
        while True:
            try:
                stage3_url = input("Entrez l'URL complète du fichier stage3 tar.xz/tar.bz2/tar.gz : ").strip()
                validate_stage3_url(stage3_url)
                break
            except ValueError as exc:
                print(f"Erreur : {exc}")

    archive_name = stage3_url.rsplit("/", 1)[-1]
    archive_path = target_dir / archive_name
    run(f"wget -O {archive_path} {stage3_url}")

    print(f"\n--- Vérification de l'intégrité (SHA256) ---")
    sig_url = f"{stage3_url}.sha256"
    sig_path = target_dir / f"{archive_name}.sha256"
    run(f"wget -O {sig_path} {sig_url}", check=False)
    if sig_path.exists():
        run(f"cd {target_dir} && sha256sum -c {archive_name}.sha256")
    else:
        print("[Avertissement] Fichier de hachage non trouvé, continuation sans vérification.")

    print(f"\n--- Extraction de {archive_name} ---")
    run(f"cd {target_dir} && tar --xattrs-include='*.*' --numeric-owner -xf {archive_name}")

    print("\n--- Configuration initiale ---")
    run(f"cp --dereference /etc/resolv.conf {target_dir / 'etc'}")
    run(f"mount --types proc /proc {target_dir / 'proc'}")
    run(f"mount --rbind /sys {target_dir / 'sys'}")
    run(f"mount --make-rslave {target_dir / 'sys'}")
    run(f"mount --rbind /dev {target_dir / 'dev'}")
    run(f"mount --make-rslave {target_dir / 'dev'}")

    write_fstab(part_root, target_dir, boot_mode, part_boot)
    write_binhost_config(target_dir)

    print("\n--- Génération du script de chroot ---")
    chroot_script = build_chroot_script(init_type, boot_mode, target_disk)
    script_path = target_dir / "install_chroot.sh"
    script_path.write_text(chroot_script)
    run(f"chmod +x {script_path}")

    print("\n--- Entrée dans le chroot pour finaliser l'installation ---")
    run(f"chroot {target_dir} /bin/bash /install_chroot.sh")

    script_path.unlink(missing_ok=True)
    print("\n🎉 Installation terminée ! Vous pouvez redémarrer sur votre nouveau système Gentoo.")
    print("N'oubliez pas de faire 'umount -R /mnt/gentoo' avant de reboot.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[Erreur fatale] {exc}")
        raise SystemExit(1)

