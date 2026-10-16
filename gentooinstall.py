#!/usr/bin/env python3
"""Installateur Gentoo guidé, inspiré de setup-alpine (Alpine) et archinstall (Arch).

L'assistant pose une suite de questions (avec valeurs par défaut), puis exécute
l'installation : partitionnement (simple ou avancé), montage, téléchargement et
extraction du stage3, génération de la configuration, puis finalisation dans le
chroot (noyau, profil/bureau, utilisateur, bootloader).

Seul OpenRC est pris en charge. La plupart des valeurs peuvent aussi être
fournies en ligne de commande pour un fonctionnement non interactif (--yes).
"""
import argparse
import getpass
import os
import re
import shlex
import stat
import subprocess
import urllib.parse
from pathlib import Path


# ---------------------------------------------------------------------------
# Profils de bureau
# ---------------------------------------------------------------------------

DESKTOPS = {
    "minimal": {
        "label": "Minimal (console)",
        "profile": "default/linux/amd64/17.1",
        "packages": [],
        "dm": None,
    },
    "xfce": {
        "label": "Xfce",
        "profile": "default/linux/amd64/17.1/desktop",
        "packages": ["xfce-base/xfce4-meta", "x11-misc/lightdm", "x11-themes/lightdm-gtk-greeter"],
        "dm": "lightdm",
    },
    "gnome": {
        "label": "GNOME",
        "profile": "default/linux/amd64/17.1/desktop/gnome",
        "packages": ["gnome-base/gnome", "gnome-base/gdm"],
        "dm": "gdm",
    },
    "kde": {
        "label": "KDE Plasma",
        "profile": "default/linux/amd64/17.1/desktop/plasma",
        "packages": ["kde-plasma/plasma-meta", "x11-misc/sddm"],
        "dm": "sddm",
    },
}


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Assistant d'installation Gentoo (style setup-alpine)")
    parser.add_argument("--disk", help="Chemin du disque cible, ex. /dev/sda")
    parser.add_argument("--boot-mode", choices=["auto", "uefi", "bios"], default="auto", help="Mode de démarrage ciblé")
    parser.add_argument("--stage3-url", help="URL directe du stage3 Gentoo")
    parser.add_argument("--target-dir", default="/mnt/gentoo", help="Point de montage cible")
    parser.add_argument("--hostname", help="Nom d'hôte (défaut : gentoo)")
    parser.add_argument("--keymap", help="Disposition clavier, ex. fr (défaut : fr)")
    parser.add_argument("--timezone", help="Fuseau horaire (défaut : Europe/Paris)")
    parser.add_argument("--locale", help="Locale principale (défaut : fr_FR.UTF-8)")
    parser.add_argument("--username", help="Nom de l'utilisateur non-root (défaut : gentoo)")
    parser.add_argument("--desktop", choices=sorted(DESKTOPS), help="Profil de bureau")
    parser.add_argument("--filesystem", choices=["ext4", "btrfs", "xfs"], help="Système de fichiers racine")
    parser.add_argument("--swap", choices=["none", "partition", "file"], help="Type de swap")
    parser.add_argument("--network-mode", choices=["dhcp", "static"], help="Configuration réseau")
    parser.add_argument("--network-service", choices=["netifrc", "networkmanager"], help="Service réseau")
    parser.add_argument("--luks", action=argparse.BooleanOptionalAction, default=None, help="Chiffrer la racine (LUKS)")
    parser.add_argument("--lvm", action=argparse.BooleanOptionalAction, default=None, help="Utiliser LVM")
    parser.add_argument(
        "--separate-home", action=argparse.BooleanOptionalAction, default=None, help="Partition /home séparée"
    )
    parser.add_argument("--ip", help="Adresse IP/CIDR en mode réseau statique, ex. 192.168.1.10/24")
    parser.add_argument("--gateway", help="Passerelle en mode réseau statique")
    parser.add_argument("--dns", help="Serveurs DNS en mode réseau statique")
    parser.add_argument("--root-password", help="Mot de passe root (non interactif). Visible dans l'historique et 'ps'.")
    parser.add_argument("--user-password", help="Mot de passe utilisateur (non interactif). Visible dans l'historique et 'ps'.")
    parser.add_argument("--luks-passphrase", help="Phrase secrète LUKS (non interactif)")
    parser.add_argument("--yes", action="store_true", help="Mode non interactif : accepte les valeurs par défaut/flags")
    parser.add_argument(
        "--force", action="store_true", help="Autoriser l'effacement d'un disque hébergeant le système en cours"
    )
    parser.add_argument("--dry-run", action="store_true", help="Affiche la planification sans exécuter les commandes")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Exécution de commandes
# ---------------------------------------------------------------------------

def run(cmd, *, check=True, cwd=None, input=None):
    if isinstance(cmd, (list, tuple)):
        display = " ".join(shlex.quote(str(part)) for part in cmd)
    else:
        display = str(cmd)
    print(f"\n[Exécution] {display}")
    result = subprocess.run(cmd, cwd=cwd, input=input, text=bool(input) or None)
    if check and result.returncode != 0:
        raise RuntimeError(f"La commande a échoué ({result.returncode}) : {display}")
    return result.returncode


# ---------------------------------------------------------------------------
# Détection matérielle
# ---------------------------------------------------------------------------

def get_disks():
    try:
        output = subprocess.check_output(
            ["lsblk", "-d", "-n", "-o", "NAME,TYPE"], text=True, stderr=subprocess.DEVNULL
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


def detect_interfaces():
    try:
        output = subprocess.check_output(["ip", "-o", "link", "show"], text=True, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    interfaces = []
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) >= 2:
            name = parts[1].strip().split("@")[0]
            if name and name != "lo":
                interfaces.append(name)
    return interfaces


def is_uefi():
    return os.path.isdir("/sys/firmware/efi")


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


def partition_device(disk, index):
    return f"{disk}{partition_suffix_for_device(disk)}{index}"


def choose_boot_mode(is_uefi_system):
    return "uefi" if is_uefi_system else "bios"


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def validate_stage3_url(url):
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("L'URL du stage3 est invalide.")

    if not re.search(r"stage3.*\.tar\.(?:xz|bz2|gz)$", parsed.path, flags=re.IGNORECASE):
        raise ValueError("L'URL ne semble pas pointer vers un stage3 tar.xz/tar.bz2/tar.gz valide.")

    return url


def size_to_mib(size):
    match = re.match(r"^(\d+)\s*([MmGg])$", size.strip())
    if not match:
        raise ValueError(f"Taille invalide : {size!r} (ex. attendu : '4G' ou '512M')")
    number = int(match.group(1))
    return number * 1024 if match.group(2).lower() == "g" else number


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


def blkid_value(device, tag="UUID"):
    try:
        output = subprocess.check_output(
            ["blkid", "-s", tag, "-o", "value", device], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Impossible de lire {tag} de {device}.") from exc
    if not output:
        raise RuntimeError(f"{tag} introuvable pour {device}.")
    return output


def blkid_uuid(device):
    return blkid_value(device, "UUID")


def write_target_file(target, relative, content, mode=None):
    path = target / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        os.chmod(path, mode)
    return path


def mkfs_cmd(filesystem, device):
    if filesystem == "ext4":
        return ["mkfs.ext4", "-F", device]
    if filesystem == "btrfs":
        return ["mkfs.btrfs", "-f", device]
    if filesystem == "xfs":
        return ["mkfs.xfs", "-f", device]
    raise ValueError(f"Système de fichiers non pris en charge : {filesystem}")


# ---------------------------------------------------------------------------
# Plan de partitionnement
# ---------------------------------------------------------------------------

def build_partition_plan(cfg):
    """Retourne la liste des partitions à créer (dict role/start/end/fs/flags)."""
    parts = []
    cursor = 1  # MiB
    uefi = cfg.boot_mode == "uefi"

    if uefi:
        parts.append({"role": "esp", "start": cursor, "end": cursor + 512, "fs": "fat32", "flags": ["esp", "boot"]})
        cursor += 512

    # Avec LUKS, il faut un /boot non chiffré lisible par GRUB.
    if cfg.luks:
        parts.append({"role": "boot", "start": cursor, "end": cursor + 512, "fs": cfg.filesystem,
                      "flags": ["boot"] if not uefi else []})
        cursor += 512

    if cfg.lvm:
        parts.append({"role": "data", "start": cursor, "end": "100%", "fs": None, "flags": []})
        return parts

    # Sans LVM : éventuelles partitions /home et swap, puis la racine en dernier.
    if cfg.separate_home and not cfg.luks:
        end = cursor + size_to_mib(cfg.home_size)
        parts.append({"role": "home", "start": cursor, "end": end, "fs": cfg.filesystem, "flags": []})
        cursor = end
    if cfg.swap == "partition":
        end = cursor + size_to_mib(cfg.swap_size)
        parts.append({"role": "swap", "start": cursor, "end": end, "fs": "linux-swap", "flags": []})
        cursor = end

    parts.append({"role": "root", "start": cursor, "end": "100%", "fs": cfg.filesystem, "flags": []})
    return parts


def create_partitions(cfg, plan):
    label = "gpt" if cfg.boot_mode == "uefi" else "msdos"
    run(["wipefs", "-a", cfg.disk])
    run(["parted", "-s", cfg.disk, "mklabel", label])
    for index, part in enumerate(plan, start=1):
        fs = part.get("fs") or "ext4"
        part_type = part["role"] if label == "gpt" else "primary"
        end = part["end"] if isinstance(part["end"], str) else f"{part['end']}MiB"
        run(["parted", "-s", cfg.disk, "mkpart", part_type, fs, f"{part['start']}MiB", end])
        for flag in part["flags"]:
            run(["parted", "-s", cfg.disk, "set", str(index), flag, "on"])


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, **kwargs):
        self.disk = kwargs.get("disk", "")
        self.boot_mode = kwargs.get("boot_mode", "auto")
        self.filesystem = kwargs.get("filesystem", "ext4")
        self.swap = kwargs.get("swap", "none")
        self.swap_size = kwargs.get("swap_size", "4G")
        self.separate_home = kwargs.get("separate_home", False)
        self.home_size = kwargs.get("home_size", "50G")
        self.luks = kwargs.get("luks", False)
        self.luks_passphrase = kwargs.get("luks_passphrase", "")
        self.lvm = kwargs.get("lvm", False)
        self.hostname = kwargs.get("hostname", "gentoo")
        self.keymap = kwargs.get("keymap", "fr")
        self.timezone = kwargs.get("timezone", "Europe/Paris")
        self.locale = kwargs.get("locale", "fr_FR.UTF-8")
        self.network_mode = kwargs.get("network_mode", "dhcp")
        self.network_service = kwargs.get("network_service", "netifrc")
        self.iface = kwargs.get("iface", "")
        self.ip_cidr = kwargs.get("ip_cidr", "")
        self.gateway = kwargs.get("gateway", "")
        self.dns = kwargs.get("dns", "")
        self.username = kwargs.get("username", "gentoo")
        self.desktop = kwargs.get("desktop", "minimal")
        self.root_password = kwargs.get("root_password", "")
        self.user_password = kwargs.get("user_password", "")
        self.stage3_url = kwargs.get("stage3_url", "")
        self.target_dir = kwargs.get("target_dir", "/mnt/gentoo")


# ---------------------------------------------------------------------------
# Prompts (style setup-alpine)
# ---------------------------------------------------------------------------

def prompt_value(question, default):
    suffix = f" [{default}]" if default not in (None, "") else ""
    raw = input(f"{question}{suffix} : ").strip()
    return raw or default


def prompt_choice(question, options, default):
    print(question)
    for index, (key, label) in enumerate(options, start=1):
        mark = " (défaut)" if key == default else ""
        print(f"  {index}) {label}{mark}")
    while True:
        raw = input(f"Choix [{default}] : ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for key, _ in options:
            if raw.lower() == key.lower():
                return key
        print("Choix invalide. Réessayez.")


def prompt_yes_no(question, default=True):
    hint = "O/n" if default else "o/N"
    while True:
        raw = input(f"{question} [{hint}] : ").strip().lower()
        if not raw:
            return default
        if raw in ("o", "oui", "y", "yes"):
            return True
        if raw in ("n", "non", "no"):
            return False
        print("Répondez par 'o' ou 'n'.")


def prompt_secret(question, required=True, confirm=False):
    while True:
        first = getpass.getpass(f"{question} : ")
        if confirm:
            second = getpass.getpass("Confirmez le mot de passe : ")
            if first != second:
                print("Les mots de passe ne correspondent pas. Réessayez.")
                continue
        if required and not first:
            print("Le mot de passe ne peut pas être vide.")
            continue
        return first


def resolve_value(args, attr, question, default):
    value = getattr(args, attr, None)
    if value is not None:
        return value
    if not args.interactive:
        return default
    return prompt_value(question, default)


def resolve_choice(args, attr, question, options, default):
    value = getattr(args, attr, None)
    if value is not None:
        return value
    if not args.interactive:
        return default
    return prompt_choice(question, options, default)


def resolve_bool(args, attr, question, default):
    value = getattr(args, attr, None)
    if value is not None:
        return value
    if not args.interactive:
        return default
    return prompt_yes_no(question, default)


def resolve_secret(args, attr, question, default="", required=True, confirm=False):
    value = getattr(args, attr, None)
    if value:
        return value
    if getattr(args, "dry_run", False):
        return default
    if not args.interactive:
        if required and not default:
            raise SystemExit(f"Un mot de passe est requis en mode non interactif (option manquante).")
        return default
    return prompt_secret(question, required=required, confirm=confirm)


# ---------------------------------------------------------------------------
# Assistant de configuration
# ---------------------------------------------------------------------------

def build_config(args):
    cfg = Config(target_dir=args.target_dir)

    # Clavier (posé tôt pour faciliter la saisie des questions suivantes).
    cfg.keymap = resolve_value(args, "keymap", "Disposition clavier (keymap)", "fr")
    if args.interactive:
        run(["loadkeys", cfg.keymap], check=False)

    # Système
    cfg.hostname = resolve_value(args, "hostname", "Nom d'hôte", "gentoo")
    cfg.timezone = resolve_value(args, "timezone", "Fuseau horaire", "Europe/Paris")
    cfg.locale = resolve_value(args, "locale", "Locale principale", "fr_FR.UTF-8")

    # Réseau
    interfaces = detect_interfaces()
    default_iface = interfaces[0] if interfaces else "eth0"
    if args.interactive and len(interfaces) > 1:
        cfg.iface = prompt_choice("Interface réseau", [(i, i) for i in interfaces], default_iface)
    else:
        cfg.iface = getattr(args, "iface", None) or default_iface
    cfg.network_service = resolve_choice(
        args, "network_service", "Service réseau",
        [("netifrc", "netifrc (OpenRC, simple)"), ("networkmanager", "NetworkManager")], "netifrc",
    )
    cfg.network_mode = resolve_choice(
        args, "network_mode", "Configuration réseau",
        [("dhcp", "DHCP"), ("static", "Statique")], "dhcp",
    )
    if cfg.network_mode == "static" and cfg.network_service == "netifrc":
        cfg.ip_cidr = resolve_value(args, "ip", "Adresse IP/CIDR (ex. 192.168.1.10/24)", "")
        cfg.gateway = resolve_value(args, "gateway", "Passerelle (ex. 192.168.1.1)", "")
        cfg.dns = resolve_value(args, "dns", "Serveurs DNS (séparés par des espaces)", "1.1.1.1")

    # Bureau
    desktop_options = [(key, value["label"]) for key, value in DESKTOPS.items()]
    cfg.desktop = resolve_choice(args, "desktop", "Profil / bureau", desktop_options, "minimal")

    # Utilisateur
    cfg.username = resolve_value(args, "username", "Nom de l'utilisateur", "gentoo")
    cfg.root_password = resolve_secret(args, "root_password", "Mot de passe root", confirm=True)
    cfg.user_password = resolve_secret(
        args, "user_password", f"Mot de passe de {cfg.username}", confirm=True
    )

    # Démarrage
    cfg.boot_mode = args.boot_mode if args.boot_mode != "auto" else choose_boot_mode(is_uefi())

    # Disque
    cfg.disk = resolve_disk(args)

    # Options de partitionnement
    cfg.filesystem = resolve_choice(
        args, "filesystem", "Système de fichiers racine",
        [("ext4", "ext4"), ("btrfs", "btrfs"), ("xfs", "xfs")], "ext4",
    )
    cfg.swap = resolve_choice(
        args, "swap", "Swap",
        [("none", "Aucune"), ("partition", "Partition de swap"), ("file", "Fichier de swap")], "none",
    )
    cfg.separate_home = resolve_bool(args, "separate_home", "Partition /home séparée ?", False)
    cfg.luks = resolve_bool(args, "luks", "Chiffrer la racine avec LUKS ?", False)
    if cfg.luks:
        cfg.luks_passphrase = resolve_secret(args, "luks_passphrase", "Phrase secrète LUKS", confirm=True)
    cfg.lvm = resolve_bool(args, "lvm", "Utiliser LVM ?", False)

    # Le chargeur d'amorçage est GRUB ; on ajuste les combinaisons incompatibles.
    normalize_config(cfg)

    # Stage3
    cfg.stage3_url = resolve_stage3_url(args)
    return cfg


def normalize_config(cfg):
    """Corrige les combinaisons incompatibles (avertit l'utilisateur)."""
    if cfg.luks and not cfg.lvm:
        if cfg.separate_home:
            print("[Info] Avec LUKS sans LVM, /home séparé n'est pas possible : option désactivée.")
            cfg.separate_home = False
        if cfg.swap == "partition":
            print("[Info] Avec LUKS sans LVM, la swap devient un fichier : option ajustée.")
            cfg.swap = "file"


def resolve_disk(args):
    if args.disk:
        disk = args.disk
        if not disk.startswith("/dev/"):
            disk = f"/dev/{disk}"
        return disk

    disks = get_disks()
    if not disks:
        print("Aucun disque détecté via lsblk. Vérifiez votre système avant de relancer le script.")
        raise SystemExit(1)

    if not args.interactive:
        raise SystemExit("Option --disk requise en mode non interactif.")

    print("Disques disponibles :")
    for index, name in enumerate(disks, start=1):
        print(f"  {index}) /dev/{name}")
    while True:
        raw = input("Choisissez le numéro du disque cible : ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(disks):
            return f"/dev/{disks[int(raw) - 1]}"
        print("Choix invalide. Réessayez.")


def resolve_stage3_url(args):
    if args.stage3_url:
        validate_stage3_url(args.stage3_url)
        return args.stage3_url
    if not args.interactive:
        raise SystemExit("Option --stage3-url requise en mode non interactif.")

    print("Copiez le lien direct d'un stage3 depuis https://www.gentoo.org/downloads/mirrors/")
    while True:
        try:
            url = input("URL du stage3 (tar.xz/tar.bz2) : ").strip()
            validate_stage3_url(url)
            return url
        except ValueError as exc:
            print(f"Erreur : {exc}")


# ---------------------------------------------------------------------------
# Disques : création, chiffrement, LVM, formatage, montage
# ---------------------------------------------------------------------------

def luks_format(cfg, device):
    run(
        ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--key-file", "-", device],
        input=cfg.luks_passphrase + "\n",
    )


def luks_open(cfg, device, name):
    run(["cryptsetup", "open", "--key-file", "-", device, name], input=cfg.luks_passphrase + "\n")
    return f"/dev/mapper/{name}"


def setup_storage(cfg, plan, target_dir):
    """Crée les systèmes de fichiers, monte le tout et renvoie l'état de l'installation."""
    devices = {part["role"]: partition_device(cfg.disk, i) for i, part in enumerate(plan, start=1)}
    state = {"root_dev": None, "luks_uuid": None, "fstab": [], "mounts": []}

    if cfg.boot_mode == "uefi":
        run(["mkfs.fat", "-F", "32", devices["esp"]])
    if "boot" in devices:
        run(mkfs_cmd(cfg.filesystem, devices["boot"]))

    if cfg.lvm:
        data = devices["data"]
        if cfg.luks:
            luks_format(cfg, data)
            state["luks_uuid"] = blkid_uuid(data)
            data = luks_open(cfg, data, "cryptdata")
        run(["pvcreate", "-ff", "-y", data])
        run(["vgcreate", "vg0", data])
        if cfg.separate_home:
            run(["lvcreate", "-L", cfg.home_size, "-n", "home", "vg0"])
        if cfg.swap == "partition":
            run(["lvcreate", "-L", cfg.swap_size, "-n", "swap", "vg0"])
        run(["lvcreate", "-l", "100%FREE", "-n", "root", "vg0"])
        root_dev = "/dev/vg0/root"
        run(mkfs_cmd(cfg.filesystem, root_dev))
        if cfg.separate_home:
            run(mkfs_cmd(cfg.filesystem, "/dev/vg0/home"))
        if cfg.swap == "partition":
            run(["mkswap", "/dev/vg0/swap"])
    else:
        root_dev = devices["root"]
        if cfg.luks:
            luks_format(cfg, root_dev)
            state["luks_uuid"] = blkid_uuid(root_dev)
            root_dev = luks_open(cfg, root_dev, "cryptroot")
        run(mkfs_cmd(cfg.filesystem, root_dev))
        if cfg.separate_home and not cfg.luks:
            run(mkfs_cmd(cfg.filesystem, devices["home"]))
        if cfg.swap == "partition":
            run(["mkswap", devices["swap"]])

    state["root_dev"] = root_dev

    # Montage
    run(["mount", root_dev, str(target_dir)])
    state["mounts"].append(str(target_dir))

    if cfg.boot_mode == "uefi":
        esp_dir = target_dir / "efi"
        esp_dir.mkdir(parents=True, exist_ok=True)
        run(["mount", devices["esp"], str(esp_dir)])
        state["fstab"].append((f"UUID={blkid_uuid(devices['esp'])}", "/efi", "vfat", "defaults", 0, 2))

    if "boot" in devices:
        boot_dir = target_dir / "boot"
        boot_dir.mkdir(parents=True, exist_ok=True)
        run(["mount", devices["boot"], str(boot_dir)])
        state["fstab"].append((f"UUID={blkid_uuid(devices['boot'])}", "/boot", cfg.filesystem, "noatime", 0, 2))

    home_dev = "/dev/vg0/home" if (cfg.lvm and cfg.separate_home) else devices.get("home")
    if home_dev:
        home_dir = target_dir / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        run(["mount", home_dev, str(home_dir)])
        state["fstab"].append((f"UUID={blkid_uuid(home_dev)}", "/home", cfg.filesystem, "noatime", 0, 2))

    state["fstab"].insert(0, (f"UUID={blkid_uuid(root_dev)}", "/", cfg.filesystem, "noatime", 0, 1))

    swap_dev = "/dev/vg0/swap" if (cfg.lvm and cfg.swap == "partition") else devices.get("swap")
    if swap_dev:
        run(["swapon", swap_dev])
        state["fstab"].append((f"UUID={blkid_uuid(swap_dev)}", "none", "swap", "sw", 0, 0))

    return state


def create_swap_file(cfg, target_dir):
    mib = size_to_mib(cfg.swap_size)
    swap_path = target_dir / "swapfile"
    # Sur btrfs, l'attribut « no COW » doit être posé AVANT toute écriture, sinon
    # swapon refuse le fichier. On crée donc le fichier vide, on pose l'attribut,
    # puis seulement on écrit les données.
    swap_path.touch()
    if cfg.filesystem == "btrfs":
        run(["chattr", "+C", str(swap_path)])
    run(["dd", "if=/dev/zero", f"of={swap_path}", "bs=1M", f"count={mib}", "status=none"])
    os.chmod(swap_path, 0o600)
    run(["mkswap", str(swap_path)])
    run(["swapon", str(swap_path)])


def build_fstab_entries(cfg, state):
    entries = list(state["fstab"])
    if cfg.swap == "file":
        entries.append(("/swapfile", "none", "swap", "sw", 0, 0))
    return entries


def write_fstab(mount_root, entries):
    etc_dir = mount_root / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# /etc/fstab: Static file system information.\n"]
    for spec, mountpoint, fstype, options, dump, pas in entries:
        lines.append(f"{spec}\t{mountpoint}\t{fstype}\t{options}\t{dump} {pas}\n")
    (etc_dir / "fstab").write_text("".join(lines))


# ---------------------------------------------------------------------------
# Fichiers de configuration système
# ---------------------------------------------------------------------------

def enable_locales(target, locales):
    path = target / "etc" / "locale.gen"
    if not path.exists():
        write_target_file(target, "etc/locale.gen", "\n".join(f"{loc} UTF-8" for loc in locales) + "\n")
        return

    wanted = set(locales)
    result = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        candidate = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        key = candidate.split()[0] if candidate else ""
        if key in wanted:
            result.append(f"{key} UTF-8")
            wanted.discard(key)
        else:
            result.append(line)
    result.extend(f"{loc} UTF-8" for loc in wanted)
    path.write_text("\n".join(result) + "\n")


def write_network_config(cfg, target):
    if cfg.network_service != "netifrc":
        return
    if cfg.network_mode == "dhcp":
        content = f'config_{cfg.iface}="dhcp"\n'
    else:
        content = (
            f'config_{cfg.iface}="{cfg.ip_cidr}"\n'
            f'routes_{cfg.iface}="default via {cfg.gateway}"\n'
            f'dns_servers_{cfg.iface}="{cfg.dns}"\n'
        )
    write_target_file(target, "etc/conf.d/net", content)


def write_base_config(cfg, target):
    write_target_file(target, "etc/hostname", f"{cfg.hostname}\n")
    write_target_file(target, "etc/conf.d/hostname", f'hostname="{cfg.hostname}"\n')
    write_target_file(target, "etc/timezone", f"{cfg.timezone}\n")
    write_target_file(target, "etc/conf.d/keymaps", f'keymap="{cfg.keymap}"\n')
    enable_locales(target, [cfg.locale, "en_US.UTF-8"])

    binhost_dir = target / "etc" / "portage" / "binrepos.conf"
    binhost_dir.mkdir(parents=True, exist_ok=True)
    (binhost_dir / "gentoo.conf").write_text(
        "[gentoobinhost]\n"
        "priority = 10\n"
        "sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/17.1/x86-64\n"
    )

    make_conf = target / "etc" / "portage" / "make.conf"
    line = 'EMERGE_DEFAULT_OPTS="${EMERGE_DEFAULT_OPTS} --getbinpkg"\n'
    if make_conf.exists():
        if "--getbinpkg" not in make_conf.read_text():
            with make_conf.open("a", encoding="utf-8") as handle:
                handle.write("\n" + line)
    else:
        make_conf.write_text(line)

    if cfg.luks or cfg.lvm:
        modules = []
        if cfg.luks:
            modules.append("crypt")
        if cfg.lvm:
            modules.append("lvm")
        write_target_file(
            target,
            "etc/dracut.conf.d/10-installer.conf",
            f'hostonly="no"\nadd_dracutmodules+=" {" ".join(modules)} "\n',
        )

    if DESKTOPS[cfg.desktop]["dm"]:
        write_target_file(
            target, "etc/conf.d/display-manager", f'DISPLAYMANAGER="{DESKTOPS[cfg.desktop]["dm"]}"\n'
        )


# ---------------------------------------------------------------------------
# Script de chroot
# ---------------------------------------------------------------------------

def build_chroot_script(cfg, root_password_hash=None, user_password_hash=None):
    desktop = DESKTOPS[cfg.desktop]
    lines = [
        "#!/bin/bash",
        "set -e",
        "source /etc/profile",
        'export PS1="(chroot) $PS1"',
        "",
        'echo ">>> Synchronisation de l\'arbre de Portage..."',
        "emerge-webrsync",
        "",
        'echo ">>> Récupération des clés OpenPGP des paquets binaires..."',
        "if ! command -v getuto >/dev/null 2>&1; then emerge --oneshot app-portage/getuto; fi",
        "getuto",
        "",
    ]

    if desktop["profile"] != DESKTOPS["minimal"]["profile"]:
        lines += [
            f'echo ">>> Sélection du profil {desktop["profile"]}..."',
            f"eselect profile set {shlex.quote(desktop['profile'])}",
            "emerge --update --deep --newuse @world",
            "",
        ]

    pre_kernel = []
    if cfg.luks:
        pre_kernel.append("sys-fs/cryptsetup")
    if cfg.lvm:
        pre_kernel.append("sys-fs/lvm2")
    if pre_kernel:
        lines += [
            "echo \">>> Outils de stockage (pour l'initramfs)...\"",
            "emerge --oneshot " + " ".join(pre_kernel),
            "",
        ]

    lines += [
        'echo ">>> Installation du noyau gentoo-kernel-bin..."',
        "emerge --oneshot sys-kernel/gentoo-kernel-bin",
        "",
    ]

    packages = list(desktop["packages"]) + ["app-admin/sudo", "sys-boot/grub"]
    if cfg.network_service == "networkmanager":
        packages += ["net-misc/networkmanager", "net-wireless/wpa_supplicant"]
    elif cfg.network_mode == "dhcp":
        packages.append("net-misc/dhcpcd")

    lines += [
        'echo ">>> Installation des paquets sélectionnés..."',
        "emerge " + " ".join(packages),
        "",
        'echo ">>> Configuration du fuseau horaire et des locales..."',
        "emerge --config sys-libs/timezone-data",
        "locale-gen",
        f"eselect locale set {shlex.quote(cfg.locale)}",
        "env-update && source /etc/profile",
        "",
        'echo ">>> Création de l\'utilisateur..."',
        f"useradd -m -G wheel,audio,video,usb,cdrom,portage,plugdev -s /bin/bash {shlex.quote(cfg.username)}",
    ]

    if user_password_hash:
        lines.append(f"echo {shlex.quote(cfg.username + ':' + user_password_hash)} | chpasswd -e")
    if root_password_hash:
        lines.append(f"usermod -p {shlex.quote(root_password_hash)} root")
    else:
        lines += ['echo "[Avertissement] Mot de passe root interactif :"', "passwd"]

    lines += [
        "",
        'echo ">>> Configuration de sudo..."',
        "cat > /etc/sudoers.d/10-wheel <<'SUDOERS'",
        "%wheel ALL=(ALL:ALL) ALL",
        "SUDOERS",
        "chmod 0440 /etc/sudoers.d/10-wheel",
        "",
        'echo ">>> Activation des services..."',
    ]

    if cfg.network_service == "networkmanager":
        lines.append("rc-update add NetworkManager default")
    else:
        lines.append(f"ln -sf net.lo /etc/init.d/net.{cfg.iface}")
        lines.append(f"rc-update add net.{cfg.iface} default")
    if desktop["dm"]:
        lines.append("rc-update add display-manager default")

    lines += ["", 'echo ">>> Installation du chargeur d\'amorçage..."']
    if cfg.boot_mode == "uefi":
        lines.append("grub-install --target=x86_64-efi --efi-directory=/efi --bootloader-id=Gentoo")
    else:
        lines.append(f"grub-install --target=i386-pc {shlex.quote(cfg.disk)}")
    lines.append("grub-mkconfig -o /boot/grub/grub.cfg")

    lines += ["", "sync", 'echo ">>> Installation terminée avec succès."']
    return "\n".join(lines) + "\n"


def write_grub_cmdline(cfg, target, luks_uuid):
    if not (cfg.luks or cfg.lvm):
        return
    params = []
    if cfg.luks and luks_uuid:
        params.append(f"rd.luks.uuid={luks_uuid}")
    if cfg.lvm:
        params.append("rd.lvm.vg=vg0")
    content = "# Généré par gentooinstall\n" f'GRUB_CMDLINE_LINUX="{" ".join(params)}"\n'
    write_target_file(target, "etc/default/grub", content, mode=0o644)


# ---------------------------------------------------------------------------
# Résumé et exécution
# ---------------------------------------------------------------------------

def print_summary(cfg):
    print("\n=== Récapitulatif de l'installation ===")
    print(f"  Disque           : {cfg.disk}")
    print(f"  Mode de démarrage: {cfg.boot_mode}")
    print(f"  Chargeur         : GRUB")
    print(f"  Système fichiers : {cfg.filesystem}")
    print(f"  Swap             : {cfg.swap}")
    print(f"  /home séparé     : {'oui' if cfg.separate_home else 'non'}")
    print(f"  LUKS             : {'oui' if cfg.luks else 'non'}")
    print(f"  LVM              : {'oui' if cfg.lvm else 'non'}")
    print(f"  Nom d'hôte       : {cfg.hostname}")
    print(f"  Clavier/fuseau   : {cfg.keymap} / {cfg.timezone}")
    print(f"  Locale           : {cfg.locale}")
    print(f"  Réseau           : {cfg.network_service} / {cfg.network_mode} ({cfg.iface})")
    print(f"  Utilisateur      : {cfg.username}")
    print(f"  Bureau           : {DESKTOPS[cfg.desktop]['label']}")
    print(f"  Stage3           : {cfg.stage3_url}")
    print("=======================================")


def verify_sha256(url, target_dir):
    """Vérifie le SHA256 du stage3 si un fichier de somme exploitable est présent.

    Retourne True si la vérification a été effectuée, False sinon (avertissement
    et poursuite de l'installation).
    """
    archive_name = url.rsplit("/", 1)[-1]
    sig_name = f"{archive_name}.sha256"
    sig_path = target_dir / sig_name
    run(["wget", "-O", str(sig_path), f"{url}.sha256"], check=False)

    # wget crée un fichier vide quand l'URL est introuvable : lancer
    # « sha256sum -c » sur un fichier vide échoue et interrompt l'installation.
    if not sig_path.exists() or sig_path.stat().st_size == 0:
        sig_path.unlink(missing_ok=True)
        print("[Avertissement] Fichier de hachage absent ou vide, poursuite sans vérification.")
        return False

    run(["sha256sum", "-c", sig_name], cwd=target_dir)
    return True


def download_and_extract(cfg, target_dir):
    archive_name = cfg.stage3_url.rsplit("/", 1)[-1]
    archive_path = target_dir / archive_name
    run(["wget", "-O", str(archive_path), cfg.stage3_url])

    print("\n--- Vérification de l'intégrité (SHA256) ---")
    verify_sha256(cfg.stage3_url, target_dir)

    print(f"\n--- Extraction de {archive_name} ---")
    run(["tar", "--xattrs-include=*.*", "--numeric-owner", "-xf", archive_name], cwd=target_dir)


def mount_pseudo_filesystems(target_dir):
    run(["cp", "--dereference", "/etc/resolv.conf", str(target_dir / "etc")])
    run(["mount", "--types", "proc", "/proc", str(target_dir / "proc")])
    run(["mount", "--rbind", "/sys", str(target_dir / "sys")])
    run(["mount", "--make-rslave", str(target_dir / "sys")])
    run(["mount", "--rbind", "/dev", str(target_dir / "dev")])
    run(["mount", "--make-rslave", str(target_dir / "dev")])


def main(argv=None):
    args = parse_args(argv)
    args.interactive = not args.yes and not args.dry_run

    if not args.dry_run:
        ensure_root()

    cfg = build_config(args)

    if args.dry_run:
        print_summary(cfg)
        print("\n[Simulation] Aucune commande réelle n'a été exécutée.")
        return

    print("\n=== Assistant d'Installation Gentoo (style setup-alpine) ===\n")

    if not is_block_device(cfg.disk):
        print(f"{cfg.disk} n'est pas un périphérique bloc valide.")
        raise SystemExit(1)
    if cfg.disk not in {f"/dev/{name}" for name in get_disks()}:
        print(f"[Avertissement] {cfg.disk} ne figure pas dans la liste des disques détectés.")
    if hosts_current_system(cfg.disk) and not args.force:
        print(f"[DANGER] {cfg.disk} semble héberger le système en cours d'exécution.")
        print("Refus d'effacer ce disque. Relancez avec --force si c'est réellement voulu.")
        raise SystemExit(1)

    target_dir = Path(cfg.target_dir)
    ensure_clean_target(target_dir)

    print_summary(cfg)
    if args.interactive:
        if input("Confirmer et démarrer l'installation ? (oui/non) : ").strip().lower() != "oui":
            print("Annulation.")
            return

    print("\n--- Partitionnement du disque ---")
    plan = build_partition_plan(cfg)
    create_partitions(cfg, plan)

    print("\n--- Création des systèmes de fichiers et montage ---")
    state = setup_storage(cfg, plan, target_dir)

    if cfg.swap == "file":
        print("\n--- Création du fichier de swap ---")
        create_swap_file(cfg, target_dir)

    print("\n--- Téléchargement du stage3 ---")
    download_and_extract(cfg, target_dir)

    print("\n--- Configuration initiale ---")
    mount_pseudo_filesystems(target_dir)
    write_fstab(target_dir, build_fstab_entries(cfg, state))
    write_base_config(cfg, target_dir)
    write_network_config(cfg, target_dir)
    write_grub_cmdline(cfg, target_dir, state["luks_uuid"])

    print("\n--- Génération du script de chroot ---")
    root_password_hash = hash_password(cfg.root_password)
    user_password_hash = hash_password(cfg.user_password)
    chroot_script = build_chroot_script(cfg, root_password_hash, user_password_hash)
    script_path = target_dir / "install_chroot.sh"
    script_path.write_text(chroot_script)
    run(["chmod", "+x", str(script_path)])

    print("\n--- Entrée dans le chroot pour finaliser l'installation ---")
    run(["chroot", str(target_dir), "/bin/bash", "/install_chroot.sh"])

    script_path.unlink(missing_ok=True)
    print("\n🎉 Installation terminée ! Vous pouvez redémarrer sur votre nouveau système Gentoo.")
    print(f"N'oubliez pas de faire 'umount -R {target_dir}' avant de redémarrer.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\n[Erreur fatale] {exc}")
        raise SystemExit(1)
