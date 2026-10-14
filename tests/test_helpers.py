import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gentooinstall import (
    build_partition_paths,
    choose_boot_mode,
    parse_args,
    partition_suffix_for_device,
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

    def test_valid_stage3_url(self):
        url = "https://distfiles.gentoo.org/releases/amd64/autobuilds/current-stage3-amd64-openrc/stage3-amd64-openrc-20240630T170447Z.tar.xz"
        self.assertEqual(validate_stage3_url(url), url)

    def test_invalid_stage3_url(self):
        with self.assertRaises(ValueError):
            validate_stage3_url("https://example.com/release.tar.gz")

    def test_write_fstab_creates_etc_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target = root / "mnt" / "gentoo"
            target.mkdir(parents=True, exist_ok=True)

            with patch("gentooinstall.subprocess.check_output") as mock_check:
                mock_check.side_effect = ["11111111-2222-3333-4444-555555555555", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
                write_fstab("/dev/nvme0n1p2", target, "uefi", "/dev/nvme0n1p1")

            fstab = target / "etc" / "fstab"
            self.assertTrue(fstab.exists())
            self.assertIn("UUID=11111111-2222-3333-4444-555555555555", fstab.read_text())

    def test_parse_args_supports_dry_run_and_explicit_disk(self):
        args = parse_args(["--disk", "/dev/sda", "--boot-mode", "bios", "--stage3-url", "https://example.com/stage3-amd64-openrc.tar.xz", "--dry-run", "--yes"])
        self.assertEqual(args.disk, "/dev/sda")
        self.assertEqual(args.boot_mode, "bios")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.yes)


if __name__ == "__main__":
    unittest.main()
