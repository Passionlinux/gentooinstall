#!/usr/bin/env python3
import argparse
import os
import re
import shlex
import stat
import subprocess
import urllib.parse
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Assistant d'installation Gentoo")
    parser.add_argument("--disk", help="Chemin du disque cible, ex. /dev/sda")
    parser.add_argument("--boot-mode", choices=["auto", "uefi", "bios"], default="auto", help="Mode de démarrage ciblé")
    parser.add_argument("--stage3-url", help="URL directe du stage3 Gentoo")
    parser.add_argument("--target-dir", default="/mnt/gentoo", help="Point de montage cible")
    parser.add_argument(
        "--root-password",
        help="Mot de passe root défini sans interaction. Attention : visible dans l'historique du shell et 'ps'.",
    )
    parser.add_argument("--yes", action="store_true", help="Confirmer sans prompt interactif")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Autoriser l'effacement d'un disque qui héberge le système en cours d'exécution",
    )
    parser.add_argument("--dry-run", action="store_true", help="Affiche la planification sans exécuter les commandes")
    return parser.parse_args(argv)


def run(cmd, *, check=True, cwd=None):
    if isinstance(cmd, (list, tuple)):
        display = " ".join(shlex.quote(str(part)) for part in cmd)
    else:
        display = str(cmd)
    print(f"\n[Exécution] {display}")
    result = subprocess.run(cmd, cwd=cwd)
    if check and result.returncode != 0:
        raise RuntimeError(f"La commande a échoué ({result.returncode}) : {display}")
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


def is_block_device(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def hosts_current_system(target_disk, mounts_file="/proc/mounts"):
    try:
        lines = Path(mounts_file).read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        if fields and fields[0].startswith(target_disk):
            return True
    return False


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

    if not re.search(r"stage3.*\.tar\.(?:xz|bz2|gz)$", parsed.path, flags=re.IGNORECASE):
        raise ValueError("L'URL ne semble pas pointer vers un stage3 tar.xz/tar.bz2/tar.gz valide.")

    return url


def hash_password(password):
    try:
        result = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=f"{password}\n",
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Impossible de hacher le mot de passe (openssl est requis).") from exc
    hashed = result.stdout.strip()
    if not hashed:
        raise RuntimeError("Le hachage du mot de passe a échoué (sortie vide).")
    return hashed


def ensure_root():
    if os.geteuid() != 0:
        print("Ce script doit être exécuté en tant que root.")
        raise SystemExit(1)


def ensure_clean_target(target):
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        print(f"Le dossier {target} existe et n'est pas vide. Vérifiez-le avant de poursuivre.")
        raise SystemExit(1)


def blkid_uuid(device):
    try:
        output = subprocess.check_output(
            ["blkid", "-s", "UUID", "-o", "value", device],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Impossible de lire l'UUID de {device}.") from exc
    if not output:
        raise RuntimeError(f"UUID introuvable pour {device}.")
    return output


def write_fstab(part_root, mount_root, boot_mode, part_boot=None):
    etc_dir = mount_root / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)

    uuid_root = blkid_uuid(part_root)

    lines = [
        "# /etc/fstab: Static file system information.\n",
        f"UUID={uuid_root}\t/\text4\tnoatime\t0 1\n",
    ]

    if boot_mode == "uefi":
        uuid_boot = blkid_uuid(part_boot)
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


def build_chroot_script(boot_mode, target_disk, root_password_hash=None):
    if boot_mode == "uefi":
        grub_line = "grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo"
    else:
        grub_line = f"grub-install --target=i386-pc {shlex.quote(target_disk)}"

    if root_password_hash:
        password_block = f"usermod -p {shlex.quote(root_password_hash)} root"
    else:
        password_block = (
            'echo "[Avertissement] Aucun mot de passe fourni : définition interactive ci-dessous."\n'
            "passwd"
        )

    return f'''#!/bin/bash
set -e
source /etc/profile
export PS1="(chroot) $PS1"

echo "Mise à jour de l'arbre de Portage (emerge-webrsync)..."
emerge-webrsync

echo "Récupération des clés OpenPGP pour les paquets binaires..."
if ! command -v getuto >/dev/null 2>&1; then
    emerge --oneshot app-portage/getuto
fi
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
{password_block}

echo "Installation de GRUB..."
emerge --oneshot sys-boot/grub
{grub_line}
grub-mkconfig -o /boot/grub/grub.cfg

sync
echo "Installation terminée avec succès !"
'''


def main(argv=None):
    args = parse_args(argv)

    target_dir = Path(args.target_dir)

    if args.dry_run:
        print("=== Mode simulation Gentoo install (dry-run) ===")
        print(f"Disque cible : {args.disk or 'auto-detect'}")
        print(f"Mode boot : {args.boot_mode}")
        print(f"Stage3 : {args.stage3_url or 'à fournir'}")
        print(f"Point de montage : {target_dir}")
        print(f"Mot de passe root : {'fourni (non interactif)' if args.root_password else 'interactif (passwd)'}")
        print("Aucune commande réelle n'a été exécutée.")
        return

    ensure_root()

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

    if not is_block_device(target_disk):
        print(f"{target_disk} n'est pas un périphérique bloc valide.")
        raise SystemExit(1)

    if target_disk not in {f"/dev/{name}" for name in disks}:
        print(f"[Avertissement] {target_disk} ne figure pas dans la liste des disques détectés.")

    if hosts_current_system(target_disk) and not args.force:
        print(f"[DANGER] {target_disk} semble héberger le système en cours d'exécution.")
        print("Refus d'effacer ce disque. Relancez avec --force si c'est réellement voulu.")
        raise SystemExit(1)

    print(f"Disque sélectionné : {target_disk}")

    ensure_clean_target(target_dir)

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
        run(["wipefs", "-a", target_disk])
        run(["parted", "-s", target_disk, "mklabel", "gpt"])
        run(["parted", "-s", target_disk, "mkpart", "ESP", "fat32", "1MiB", "512MiB"])
        run(["parted", "-s", target_disk, "set", "1", "boot", "on"])
        run(["parted", "-s", target_disk, "mkpart", "primary", "ext4", "512MiB", "100%"])
        run(["mkfs.fat", "-F", "32", part_boot])
    else:
        suffix = partition_suffix_for_device(target_disk)
        part_root = f"{target_disk}{suffix}1"
        part_boot = None
        run(["wipefs", "-a", target_disk])
        run(["parted", "-s", target_disk, "mklabel", "msdos"])
        run(["parted", "-s", target_disk, "mkpart", "primary", "ext4", "1MiB", "100%"])
        run(["parted", "-s", target_disk, "set", "1", "boot", "on"])

    run(["mkfs.ext4", "-F", part_root])

    print("\n--- Montage des partitions ---")
    run(["mount", part_root, str(target_dir)])
    if boot_mode == "uefi":
        efi_dir = target_dir / "efi"
        efi_dir.mkdir(parents=True, exist_ok=True)
        run(["mount", part_boot, str(efi_dir)])

    print("\n--- Téléchargement du Stage 3 ---")
    if args.stage3_url:
        stage3_url = args.stage3_url
        try:
            validate_stage3_url(stage3_url)
        except ValueError as exc:
            print(f"Erreur : {exc}")
            raise SystemExit(1)
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
    run(["wget", "-O", str(archive_path), stage3_url])

    print("\n--- Vérification de l'intégrité (SHA256) ---")
    sig_url = f"{stage3_url}.sha256"
    sig_path = target_dir / f"{archive_name}.sha256"
    run(["wget", "-O", str(sig_path), sig_url], check=False)
    if sig_path.exists():
        run(["sha256sum", "-c", sig_path.name], cwd=target_dir)
    else:
        print("[Avertissement] Fichier de hachage non trouvé, continuation sans vérification.")

    print(f"\n--- Extraction de {archive_name} ---")
    run(["tar", "--xattrs-include=*.*", "--numeric-owner", "-xf", archive_name], cwd=target_dir)

    print("\n--- Configuration initiale ---")
    run(["cp", "--dereference", "/etc/resolv.conf", str(target_dir / "etc")])
    run(["mount", "--types", "proc", "/proc", str(target_dir / "proc")])
    run(["mount", "--rbind", "/sys", str(target_dir / "sys")])
    run(["mount", "--make-rslave", str(target_dir / "sys")])
    run(["mount", "--rbind", "/dev", str(target_dir / "dev")])
    run(["mount", "--make-rslave", str(target_dir / "dev")])

    write_fstab(part_root, target_dir, boot_mode, part_boot)
    write_binhost_config(target_dir)

    print("\n--- Génération du script de chroot ---")
    root_password_hash = None
    if args.root_password:
        root_password_hash = hash_password(args.root_password)
    elif args.yes:
        print(
            "[Avertissement] Aucun --root-password fourni avec --yes : 'passwd' sera demandé "
            "dans le chroot (interaction requise)."
        )

    chroot_script = build_chroot_script(boot_mode, target_disk, root_password_hash)
    script_path = target_dir / "install_chroot.sh"
    script_path.write_text(chroot_script)
    run(["chmod", "+x", str(script_path)])

    print("\n--- Entrée dans le chroot pour finaliser l'installation ---")
    run(["chroot", str(target_dir), "/bin/bash", "/install_chroot.sh"])

    script_path.unlink(missing_ok=True)
    print("\n🎉 Installation terminée ! Vous pouvez redémarrer sur votre nouveau système Gentoo.")
    print("N'oubliez pas de faire 'umount -R /mnt/gentoo' avant de reboot.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[Erreur fatale] {exc}")
        raise SystemExit(1)
