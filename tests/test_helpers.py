import shutil
import tempfile
import unittest
from pathlib import Path

from gentooinstall import (
    Config,
    build_chroot_script,
    build_partition_paths,
    build_partition_plan,
    choose_boot_mode,
    enable_locales,
    ensure_clean_target,
    hash_password,
    hosts_current_system,
    mkfs_cmd,
    normalize_config,
    parse_args,
    partition_suffix_for_device,
    size_to_mib,
    validate_stage3_url,
    write_fstab,
)


class GentooInstallTests(unittest.TestCase):
    def test_nvme_partition_paths(self):
        self.assertEqual(partition_suffix_for_device("/dev/nvme0n1"), "p")
        self.assertEqual(build_partition_paths("/dev/nvme0n1"), ("/dev/nvme0n1p1", "/dev/nvme0n1p2"))

    def test_sata_partition_paths(self):
        self.assertEqual(partition_suffix_for_device("/dev/sda"), "")
        self.assertEqual(build_partition_paths("/dev/sda"), ("/dev/sda1", "/dev/sda2"))

    def test_choose_boot_mode_prefers_uefi_when_supported(self):
        self.assertEqual(choose_boot_mode(True), "uefi")
        self.assertEqual(choose_boot_mode(False), "bios")

    def test_size_to_mib(self):
        self.assertEqual(size_to_mib("4G"), 4096)
        self.assertEqual(size_to_mib("512M"), 512)
        self.assertEqual(size_to_mib("1g"), 1024)
        with self.assertRaises(ValueError):
            size_to_mib("pas une taille")

    def test_valid_stage3_url(self):
        url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-openrc/stage3-amd64-openrc-20240630T170447Z.tar.xz"
        self.assertEqual(validate_stage3_url(url), url)

    def test_invalid_stage3_url(self):
        with self.assertRaises(ValueError):
            validate_stage3_url("https://example.com/release.tar.gz")

    def test_validate_stage3_url_rejects_shell_metacharacters(self):
        with self.assertRaises(ValueError):
            validate_stage3_url("https://example.com/stage3-amd64-openrc.tar.xz; rm -rf /")

    def test_write_fstab_with_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir)
            write_fstab(
                target,
                [
                    ("UUID=11111111-2222-3333-4444-555555555555", "/", "ext4", "noatime", 0, 1),
                    ("UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "/efi", "vfat", "defaults", 0, 2),
                ],
            )
            text = (target / "etc" / "fstab").read_text()
            self.assertIn("UUID=11111111-2222-3333-4444-555555555555", text)
            self.assertIn("/efi", text)

    def test_mkfs_cmd(self):
        self.assertEqual(mkfs_cmd("ext4", "/dev/sda1"), ["mkfs.ext4", "-F", "/dev/sda1"])
        self.assertEqual(mkfs_cmd("btrfs", "/dev/sda1"), ["mkfs.btrfs", "-f", "/dev/sda1"])
        self.assertEqual(mkfs_cmd("xfs", "/dev/sda1"), ["mkfs.xfs", "-f", "/dev/sda1"])
        with self.assertRaises(ValueError):
            mkfs_cmd("zfs", "/dev/sda1")

    def test_partition_plan_uefi_simple(self):
        cfg = Config(disk="/dev/nvme0n1", boot_mode="uefi")
        roles = [part["role"] for part in build_partition_plan(cfg)]
        self.assertEqual(roles, ["esp", "root"])

    def test_partition_plan_bios_luks_adds_boot(self):
        cfg = Config(disk="/dev/sda", boot_mode="bios", luks=True)
        roles = [part["role"] for part in build_partition_plan(cfg)]
        self.assertIn("boot", roles)
        self.assertEqual(roles[-1], "root")

    def test_partition_plan_lvm_single_data(self):
        cfg = Config(disk="/dev/sda", boot_mode="bios", lvm=True, separate_home=True, swap="partition")
        roles = [part["role"] for part in build_partition_plan(cfg)]
        self.assertEqual(roles, ["data"])

    def test_normalize_luks_without_lvm(self):
        cfg = Config(luks=True, lvm=False, swap="partition", separate_home=True)
        normalize_config(cfg)
        self.assertFalse(cfg.separate_home)
        self.assertEqual(cfg.swap, "file")

    def test_esp_mount_depends_on_bootloader(self):
        self.assertEqual(Config(boot_mode="uefi", bootloader="grub").esp_mount, "/efi")
        self.assertEqual(Config(boot_mode="uefi", bootloader="systemd-boot").esp_mount, "/boot")

    def test_ensure_clean_target_creates_missing_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "gentoo"
            ensure_clean_target(target)
            self.assertTrue(target.is_dir())

    def test_ensure_clean_target_rejects_non_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir)
            (target / "residu").write_text("x")
            with self.assertRaises(SystemExit):
                ensure_clean_target(target)

    def test_hosts_current_system_detects_mounted_disk(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mounts = Path(tmp_dir) / "mounts"
            mounts.write_text("/dev/nvme0n1p2 / ext4 rw 0 0\n/dev/sda1 /boot ext4 rw 0 0\n")
            self.assertTrue(hosts_current_system("/dev/nvme0n1", str(mounts)))
            self.assertFalse(hosts_current_system("/dev/sdb", str(mounts)))

    def test_enable_locales_uncomments_requested(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir)
            (target / "etc").mkdir()
            (target / "etc" / "locale.gen").write_text(
                "#fr_FR.UTF-8 UTF-8\n#de_DE.UTF-8 UTF-8\n"
            )
            enable_locales(target, ["fr_FR.UTF-8"])
            text = (target / "etc" / "locale.gen").read_text()
            self.assertIn("fr_FR.UTF-8 UTF-8", text)
            self.assertNotIn("#fr_FR.UTF-8", text)
            self.assertIn("#de_DE.UTF-8", text)

    def test_build_chroot_script_with_hashes(self):
        cfg = Config(disk="/dev/sda", boot_mode="bios", desktop="minimal", username="bob")
        script = build_chroot_script(cfg, "$6$root", "$6$user", None)
        self.assertIn("usermod -p '$6$root' root", script)
        self.assertIn("chpasswd -e", script)
        self.assertIn("grub-install --target=i386-pc /dev/sda", script)
        self.assertIn("net.lo /etc/init.d/net.", script)

    def test_build_chroot_script_systemd_boot_cmdline(self):
        cfg = Config(disk="/dev/nvme0n1", boot_mode="uefi", bootloader="systemd-boot", luks=True)
        script = build_chroot_script(cfg, "$6$r", "$6$u", "LUKS-UUID")
        self.assertIn("rd.luks.uuid=LUKS-UUID", script)
        self.assertIn("bootctl --esp-path=/boot install", script)

    def test_parse_args_supports_non_interactive_options(self):
        args = parse_args(
            [
                "--disk", "/dev/sda",
                "--boot-mode", "bios",
                "--stage3-url", "https://example.com/stage3-amd64-openrc.tar.xz",
                "--desktop", "xfce",
                "--luks", "--no-lvm",
                "--dry-run", "--yes",
            ]
        )
        self.assertEqual(args.disk, "/dev/sda")
        self.assertEqual(args.desktop, "xfce")
        self.assertTrue(args.luks)
        self.assertFalse(args.lvm)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.yes)

    @unittest.skipUnless(shutil.which("openssl"), "openssl non disponible")
    def test_hash_password_produces_sha512_crypt(self):
        hashed = hash_password("un-mot-de-passe")
        self.assertTrue(hashed.startswith("$6$"))


if __name__ == "__main__":
    unittest.main()
