#!/usr/bin/env python3
"""A backup never packs, follows or writes a host path through a link
planted in a container.

create_backup copies wp-content (and a hub's onionheaven/ and onionhome/)
out of the WordPress container, where code running as www-data can plant
links. The Settings page lets the site ask for a backup under a password of
its choosing and download it, so a link staged on the host as
`uploads/x -> ~/.ssh/id_ed25519` would hand the site that key: `zip -r`
(no -y) stores the file a link points to. The derivative pruning would walk
a linked uploads/ and delete host files, and the archive marker would be
written through a link. These tests plant links, hard links and special
files in what each staging step receives, on the Mac path (docker exec tar
into unpack_plain_tar, docker cp) and the Linux one (docker cp, then the
scrub-staging helper), and check that nothing outside the staging folder is
packed, followed, pruned or written.

No docker: the container side is a stub script or bash function.
"""

import io
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onionpress import backup, backup_crypto, derivatives  # noqa: E402
from onionpress.cli import OnionPressCLI  # noqa: E402
import test_backup_metadata_validation as mv  # noqa: E402

SECRET = b"host file a site must never get back in its backup\n"
PW = mv.PW


def _outside(root):
    """A folder outside every staging tree, standing in for the user's home:
    a secret, an ssh key, and an uploads-like folder whose image sizes the
    derivative pruning would delete if it walked in. Returns its path."""
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".ssh"))
    os.makedirs(os.path.join(home, "Pictures"))
    for rel in ("secret.txt", os.path.join(".ssh", "id_ed25519")):
        with open(os.path.join(home, rel), "wb") as f:
            f.write(SECRET)
    for name in ("photo.jpg", "photo-150x150.jpg"):
        with open(os.path.join(home, "Pictures", name), "wb") as f:
            f.write(SECRET)
    return home


def _snapshot(path):
    """Every name under *path* with its type and contents (lstat)."""
    out = {}
    for dirpath, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    out[os.path.relpath(p, path)] = f.read()
            else:
                out[os.path.relpath(p, path)] = stat.S_IFMT(st.st_mode)
    return out


def _non_plain(root):
    """Paths under *root* that are neither regular files nor folders."""
    bad = []
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            mode = os.lstat(os.path.join(dirpath, name)).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                bad.append(os.path.relpath(os.path.join(dirpath, name), root))
    return bad


def _tar(entries):
    """GNU-format tar bytes of *entries*: (kind, name, arg) with kind file
    (arg: bytes), dir, sym/hard (arg: link target), fifo or chr."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for kind, name, arg in entries:
            ti = tarfile.TarInfo(name)
            ti.mtime = 1_600_000_000
            if kind == "file":
                ti.size = len(arg)
                ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(arg))
                continue
            ti.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                       "hard": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE,
                       "chr": tarfile.CHRTYPE}[kind]
            ti.mode = 0o755 if kind == "dir" else 0o644
            if kind in ("sym", "hard"):
                ti.linkname = arg
            tf.addfile(ti)
    return buf.getvalue()


def _inner_names_and_bytes(zip_path, password):
    """A format-2 backup's inner zip: {name: bytes}."""
    fd, inner = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        backup_crypto.decrypt_archive(zip_path, password, inner)
        with zipfile.ZipFile(inner) as zf:
            return {i.filename: zf.read(i) for i in zf.infolist()}
    finally:
        os.unlink(inner)


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = _outside(self.tmp)
        self.home_before = _snapshot(self.home)
        self.logs = []

    def assert_home_untouched(self):
        self.assertEqual(_snapshot(self.home), self.home_before)

    def assert_logged_left_out(self, kind):
        self.assertTrue(any("out of the backup" in m and kind in m
                            for m in self.logs), self.logs)


class TestUnpackPlainTar(_Base):
    """unpack_plain_tar: what create_backup unpacks the container's
    `tar -cf -` with on the host."""

    def _unpack(self, data):
        dest = tempfile.mkdtemp(dir=self.tmp, prefix="dest-")
        n = backup.unpack_plain_tar(io.BytesIO(data), dest, self.logs.append,
                                    "wp-content")
        return dest, n

    def test_plain_files_and_folders_unpack(self):
        long_name = "./uploads/" + "d" * 120 + "/" + "f" * 120 + ".txt"
        dest, n = self._unpack(_tar([
            ("dir", "./", None),
            ("dir", "./themes/", None),
            ("file", "./themes/a.css", b"css"),
            ("file", "./uploads/2024/01/photo.jpg", b"jpg"),    # no folder entries
            ("file", "./uploads/back\\slash.txt", b"bs"),
            ("file", "./uploads/café.txt", b"cafe"),
            ("file", long_name, b"long"),
            ("file", "./themes/a.css", b"css again"),           # later wins, as tar
        ]))
        self.assertEqual(n, 0)
        self.assertEqual(_snapshot(dest), {
            "themes": stat.S_IFDIR, "themes/a.css": b"css again",
            "uploads": stat.S_IFDIR, "uploads/2024": stat.S_IFDIR,
            "uploads/2024/01": stat.S_IFDIR,
            "uploads/2024/01/photo.jpg": b"jpg",
            "uploads/back\\slash.txt": b"bs",
            "uploads/café.txt": b"cafe",
            "uploads/" + "d" * 120: stat.S_IFDIR,
            long_name[2:]: b"long",
        })
        self.assertEqual(os.stat(os.path.join(dest, "themes", "a.css")).st_mtime,
                         1_600_000_000)

    def test_modes_keep_owner_access_and_drop_special_bits(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
            for name, mode in (("setuid", 0o4777), ("none", 0o000), ("exec", 0o755)):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = 1, mode
                tf.addfile(ti, io.BytesIO(b"x"))
        dest, _ = self._unpack(buf.getvalue())
        modes = {n: stat.S_IMODE(os.stat(os.path.join(dest, n)).st_mode)
                 for n in ("setuid", "none", "exec")}
        self.assertFalse(modes["setuid"] & (stat.S_ISUID | 0o022))
        self.assertEqual(modes["none"] & 0o600, 0o600)
        self.assertTrue(modes["exec"] & stat.S_IXUSR)

    def test_links_and_special_entries_are_left_out(self):
        home = self.home
        cases = {
            "absolute link to a folder, then a file through it":
                [("sym", "a", home), ("file", "a/pwn", b"X")],
            "relative link out, then a file through it":
                [("sym", "a", "../home"), ("file", "a/pwn", b"X")],
            "link to a file, then the same name as a file":
                [("sym", "a", home + "/secret.txt"), ("file", "a", b"X")],
            "a link to the ssh key": [("sym", "key.txt", home + "/.ssh/id_ed25519")],
            "a link to uploads": [("sym", "uploads", home + "/Pictures")],
            "a dangling link": [("sym", "uploads", "/nonexistent/x")],
            "'..' name": [("file", "../home/pwn", b"X")],
            "absolute name": [("file", home + "/pwn", b"X")],
            "hard link to an absolute path": [("hard", "h", home + "/secret.txt")],
            "hard link out with '..'": [("hard", "h", "../home/secret.txt")],
            "hard link through a link":
                [("sym", "a", home), ("hard", "h", "a/secret.txt")],
            "nested link, then a file through it":
                [("dir", "d", None), ("sym", "d/s", home), ("file", "d/s/pwn", b"X")],
            "FIFO": [("fifo", "p", None)],
            "character device": [("chr", "c", None)],
        }
        for label, entries in cases.items():
            with self.subTest(label):
                self.logs.clear()
                dest, n = self._unpack(_tar(entries))
                self.assertGreaterEqual(n, 1)
                self.assertEqual(_non_plain(dest), [])
                self.assert_home_untouched()
                self.assertTrue(any("out of the backup" in m for m in self.logs))

    def test_a_folder_where_a_file_was_is_left_out(self):
        dest, n = self._unpack(_tar([("file", "a", b"1"), ("file", "a/b", b"2"),
                                     ("dir", "c", None), ("file", "c", b"3")]))
        self.assertEqual(n, 2)
        self.assertEqual(_snapshot(dest), {"a": b"1", "c": stat.S_IFDIR})

    def test_a_real_tar_stream_of_a_planted_tree(self):
        """What `tar -cf -` makes of a wp-content with links in it."""
        tree = os.path.join(self.tmp, "wp-content")
        os.makedirs(os.path.join(tree, "uploads"))
        with open(os.path.join(tree, "uploads", "real.jpg"), "wb") as f:
            f.write(b"real")
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"),
                   os.path.join(tree, "uploads", "key.jpg"))
        os.symlink(os.path.join(self.home, "Pictures"), os.path.join(tree, "linked"))
        os.mkfifo(os.path.join(tree, "uploads", "fifo"))
        stream = subprocess.run(["tar", "-cf", "-", "-C", tree, "."],
                                env=dict(os.environ, COPYFILE_DISABLE="1"),
                                capture_output=True, check=True).stdout
        dest, n = self._unpack(stream)
        self.assertEqual(n, 3)
        self.assertEqual(_snapshot(dest), {"uploads": stat.S_IFDIR,
                                           "uploads/real.jpg": b"real"})
        self.assert_logged_left_out("a symbolic link")

    def test_logged_names_are_printable(self):
        self._unpack(_tar([("sym", "evil\nBackup: complete\x1b[2J", "/etc")]))
        line = [m for m in self.logs if "out of the backup" in m][0]
        self.assertTrue(line.isprintable(), repr(line))


class TestScrubStagedTree(_Base):
    """scrub_staged_tree: run on every tree docker cp'd out of a container,
    and on the whole staging by pack_backup."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "staging")
        os.makedirs(os.path.join(self.root, "uploads", "2024"))
        with open(os.path.join(self.root, "uploads", "2024", "a.jpg"), "wb") as f:
            f.write(b"a")

    def test_links_and_special_files_are_removed(self):
        up = os.path.join(self.root, "uploads")
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(up, "key.txt"))
        os.symlink(os.path.join(self.home, "Pictures"), os.path.join(up, "linked"))
        os.symlink("../../home/secret.txt", os.path.join(up, "rel.txt"))
        os.symlink("/nonexistent/onionpress-test", os.path.join(up, "dangling"))
        os.mkfifo(os.path.join(up, "fifo"))
        sock_dir = tempfile.mkdtemp(dir="/tmp")   # AF_UNIX paths are short
        self.addCleanup(shutil.rmtree, sock_dir, True)
        sock_root = os.path.join(sock_dir, "s")
        os.makedirs(sock_root)
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(sock_root, "sock"))
        s.close()
        self.assertEqual(backup.scrub_staged_tree(self.root, self.logs.append, "wp-content"), 5)
        self.assertEqual(backup.scrub_staged_tree(sock_root, self.logs.append), 1)
        self.assertEqual(_snapshot(self.root), {
            "uploads": stat.S_IFDIR, "uploads/2024": stat.S_IFDIR,
            "uploads/2024/a.jpg": b"a"})
        self.assertEqual(os.listdir(sock_root), [])
        self.assert_home_untouched()
        self.assert_logged_left_out("a symbolic link")
        self.assert_logged_left_out("a device, FIFO or socket")

    def test_a_hard_link_to_an_outside_file_is_removed(self):
        os.link(os.path.join(self.home, "secret.txt"),
                os.path.join(self.root, "uploads", "hard.txt"))
        # Two names inside the tree are a file the tree holds: kept.
        os.link(os.path.join(self.root, "uploads", "2024", "a.jpg"),
                os.path.join(self.root, "uploads", "a-again.jpg"))
        self.assertEqual(backup.scrub_staged_tree(self.root, self.logs.append), 1)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "uploads", "hard.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "uploads", "a-again.jpg")))
        self.assert_home_untouched()
        self.assert_logged_left_out("a hard link to a file outside it")

    def test_unreadable_files_and_folders_are_made_readable(self):
        hidden = os.path.join(self.root, "uploads", "hidden")
        os.makedirs(hidden)
        os.symlink(os.path.join(self.home, "secret.txt"), os.path.join(hidden, "link"))
        locked = os.path.join(self.root, "uploads", "2024", "a.jpg")
        os.chmod(locked, 0)
        os.chmod(hidden, 0)
        self.addCleanup(os.chmod, hidden, 0o700)
        self.assertEqual(backup.scrub_staged_tree(self.root, self.logs.append), 1)
        self.assertEqual(os.listdir(hidden), [])
        self.assertTrue(os.access(locked, os.R_OK))

    def test_the_root_must_be_a_real_folder(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.home, link)
        with self.assertRaises(ValueError):
            backup.scrub_staged_tree(link, self.logs.append)
        self.assert_home_untouched()

    def test_many_entries_are_counted_not_listed(self):
        for i in range(30):
            os.symlink("/etc", os.path.join(self.root, f"l{i}"))
        self.assertEqual(backup.scrub_staged_tree(self.root, self.logs.append), 30)
        self.assertEqual(len(self.logs), backup._STAGE_LOG_LIMIT + 1)
        self.assertIn("10 more", self.logs[-1])

    def test_the_cli_helper(self):
        """`onionpress.cli scrub-staging`, what the Linux create_backup runs."""
        os.symlink(os.path.join(self.home, "secret.txt"),
                   os.path.join(self.root, "uploads", "x"))
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(OnionPressCLI.cmd_scrub_staging(self.root), 0)
            self.assertEqual(OnionPressCLI.cmd_scrub_staging(
                os.path.join(self.tmp, "missing")), 0)
            link = os.path.join(self.tmp, "rootlink")
            os.symlink(self.home, link)
            self.assertEqual(OnionPressCLI.cmd_scrub_staging(link), 1)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "uploads", "x")))
        self.assertIn("out of the backup", err.getvalue())
        self.assert_home_untouched()


class TestDerivativesNeverFollowALink(_Base):

    def test_prune_skips_a_linked_uploads_folder(self):
        link = os.path.join(self.tmp, "uploads")
        os.symlink(os.path.join(self.home, "Pictures"), link)
        self.assertEqual(derivatives.prune_image_derivatives(link), (0, 0))
        self.assert_home_untouched()

    def test_the_marker_is_never_written_through_a_link(self):
        wp = os.path.join(self.tmp, "wp1")
        os.makedirs(os.path.join(wp, "uploads"))
        os.symlink(os.path.join(self.home, "secret.txt"),
                   os.path.join(wp, derivatives.ARCHIVE_MARKER_RELPATH))
        with self.assertRaises(OSError):
            derivatives.write_archive_marker(wp)
        wp = os.path.join(self.tmp, "wp2")
        os.makedirs(wp)
        os.symlink(os.path.join(self.home, "Pictures"), os.path.join(wp, "uploads"))
        with self.assertRaises(OSError):
            derivatives.write_archive_marker(wp)
        self.assert_home_untouched()


class TestPackBackupLeavesLinksOut(_Base):
    """pack_backup is the step both platforms share: whatever reaches it,
    zip never reads a host file through a link."""

    def setUp(self):
        super().setUp()
        mv._cheap_kdf(self)
        self.staging = mv._make_staging(self.tmp, mv._metadata())
        up = os.path.join(self.staging, "wp-content", "uploads")
        os.makedirs(up)
        with open(os.path.join(up, "real.jpg"), "wb") as f:
            f.write(b"real")
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(up, "key.jpg"))
        os.symlink(os.path.join(self.home, "Pictures"), os.path.join(up, "linked"))
        os.link(os.path.join(self.home, "secret.txt"), os.path.join(up, "hard.txt"))
        os.mkfifo(os.path.join(up, "fifo"))

    def test_links_never_reach_the_archive(self):
        out = os.path.join(self.tmp, "b.zip")
        backup.pack_backup(self.staging, out, PW, self.logs.append)
        members = _inner_names_and_bytes(out, PW)
        self.assertFalse([n for n, data in members.items() if SECRET in data])
        names = {n.rstrip("/") for n in members}
        self.assertIn("wp-content/uploads/real.jpg", names)
        for gone in ("key.jpg", "linked", "hard.txt", "fifo"):
            self.assertNotIn(f"wp-content/uploads/{gone}", names)
        self.assert_home_untouched()
        self.assert_logged_left_out("a symbolic link")
        # And the backup restores (a link stored as a link would be refused).
        dest = os.path.join(self.tmp, "restored")
        os.makedirs(dest)
        backup.extract_backup(out, PW, self.logs.append, staging=dest)
        self.assertEqual(_non_plain(dest), [])

    def test_zip_is_never_told_to_store_links(self):
        with open(os.path.join(PROJECT_ROOT, "src", "onionpress", "backup.py")) as f:
            src = f.read()
        pack = src[src.index("\ndef pack_backup("):src.index("\n_WRONG_PASSWORD_MSG")]
        self.assertNotIn("'-y'", pack)
        self.assertNotIn("--symlinks", pack)
        self.assertLess(pack.index("scrub_staged_tree(staging"), pack.index("['zip', '-r'"))


class TestCreateBackupWithPlantedLinks(_Base):
    """create_backup (Mac, and `onionpress.cli backup`) against a container
    stub whose wp-content, onionheaven/ and onionhome/ hold planted links."""

    def setUp(self):
        super().setUp()
        mv._cheap_kdf(self)
        home = self.home
        # wp-content as the container's tar would see it.
        wp = os.path.join(self.tmp, "container-wp-content")
        os.makedirs(os.path.join(wp, "themes"))
        os.makedirs(os.path.join(wp, "plugins"))
        with open(os.path.join(wp, "themes", "flavor.css"), "w") as f:
            f.write("theme data")
        os.symlink(os.path.join(home, ".ssh", "id_ed25519"),
                   os.path.join(wp, "plugins", "key.php"))
        os.symlink(os.path.join(home, "Pictures"), os.path.join(wp, "uploads"))
        # A hub's data as docker cp hands it over (links copied as links):
        # the OnionHeaven volume from the onionheaven container, the
        # OnionHome name registry from onionpress-wordpress.
        hub = os.path.join(self.tmp, "container-hub")
        for sub in ("onionheaven", "onionhome"):
            os.makedirs(os.path.join(hub, sub))
            with open(os.path.join(hub, sub, "registry.db"), "w") as f:
                f.write(sub)
            os.symlink(os.path.join(home, "secret.txt"), os.path.join(hub, sub, "stolen"))
        self.pem = os.path.join(self.tmp, "key.pem")
        with open(self.pem, "wb") as f:
            f.write(mv.key_manager.build_openssh_key(mv.PRIV, mv.PUB))
        self.calls = os.path.join(self.tmp, "calls")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(f"""#!/bin/bash
echo "$*" >> {self.calls!r}
case "$*" in
  *ks_hs_id.ed25519_expanded_private*) cat {self.pem!r} ;;
  *"wp config get DB_PASSWORD"*) echo pw ;;
  *"wp config get"*) echo wordpress ;;
  *mariadb-dump*) echo "CREATE TABLE wp_posts;" ;;
  *"test -f"*) exit 0 ;;
  *"tar -cf -"*) COPYFILE_DISABLE=1 tar -cf - -C {wp!r} . ;;
  "cp onionheaven:/var/lib/onionheaven/. "*)
      cp -R {os.path.join(hub, "onionheaven")!r} "$3" ;;
  "cp onionpress-wordpress:/var/lib/onionpress/onionhome/. "*)
      cp -R {os.path.join(hub, "onionhome")!r} "$3" ;;
  *) exit 1 ;;
esac
""")
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        p = mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        p.start()
        self.addCleanup(p.stop)
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)

    def test_planted_links_never_reach_the_backup(self):
        out = os.path.join(self.tmp, "backup.zip")
        backup.create_backup(mv.ADDR, "admin", PW, out, "2.5.2",
                             self.logs.append, data_dir=self.data_dir)
        members = _inner_names_and_bytes(out, PW)
        self.assertFalse([n for n, data in members.items() if SECRET in data])
        names = {n.rstrip("/") for n in members}
        self.assertIn("wp-content/themes/flavor.css", names)
        self.assertIn("onionheaven/registry.db", names)
        self.assertIn("onionhome/registry.db", names)
        for gone in ("wp-content/plugins/key.php", "onionheaven/stolen",
                     "onionhome/stolen"):
            self.assertNotIn(gone, names)
        self.assertFalse([n for n in names if n.startswith("wp-content/uploads/")
                          and not n.endswith(".onionpress-regenerate-derivatives")])
        # Nothing pruned or written through the linked uploads/.
        self.assert_home_untouched()
        self.assert_logged_left_out("a symbolic link")

    def test_the_container_tar_stores_hard_links_as_files(self):
        backup.create_backup(mv.ADDR, "admin", PW, os.path.join(self.tmp, "b.zip"),
                             "2.5.2", self.logs.append, data_dir=self.data_dir)
        with open(self.calls) as f:
            tar_calls = [line for line in f if "tar -cf -" in line]
        self.assertEqual(len(tar_calls), 1)
        self.assertIn("--hard-dereference", tar_calls[0])

    def test_no_host_tar_extracts_the_container_stream(self):
        with open(os.path.join(PROJECT_ROOT, "src", "onionpress", "backup.py")) as f:
            src = f.read()
        body = src[src.index("\ndef create_backup("):src.index("\n# Already-compressed")]
        self.assertNotRegex(body, r"\[\s*'tar',\s*'-x")
        self.assertIn("_stage_container_tar(", body)


class TestLinuxCreateBackup(_Base):
    """linux/onionpress create_backup, run for real with docker stubbed:
    docker cp copies links as links, so the scrub-staging helper must run
    before the derivative pruning and the marker, and pack-backup again."""

    def _create_backup_function(self):
        text = mv._read("linux/onionpress")
        start = text.index("\ncreate_backup() {")
        # The function embeds a python3 -c snippet with a bare "}" line.
        return text[start:text.index("\nrestore_backup() {", start)]

    def test_scrub_runs_before_anything_reads_the_copy(self):
        body = self._create_backup_function()
        cp = body.index("docker cp onionpress-wordpress:/var/www/html/wp-content/.")
        scrub = body.index('op_py scrub-staging --dir "$staging/wp-content"')
        self.assertLess(cp, scrub)
        self.assertLess(scrub, body.index("prune-image-derivatives"))
        self.assertLess(scrub, body.index(".onionpress-regenerate-derivatives"))

    def test_planted_links_never_reach_the_backup(self):
        mv._cheap_kdf(self)
        wp = os.path.join(self.tmp, "container-wp-content")
        os.makedirs(os.path.join(wp, "themes"))
        with open(os.path.join(wp, "themes", "flavor.css"), "w") as f:
            f.write("theme data")
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"),
                   os.path.join(wp, "themes", "key.css"))
        os.symlink(os.path.join(self.home, "Pictures"), os.path.join(wp, "uploads"))
        prelude = r'''
log() { echo "[log] $1" >> "$LOG_FILE"; }
docker() {
    echo "docker $*" >> "$CALLS"
    case "$1 $2" in
        "cp onionpress-wordpress:/var/www/html/wp-content/.") cp -R "$WP/." "$3" ;;
        "exec onionpress-tor") echo "$ADDR" ;;
        *) return 1 ;;
    esac
}
db_client() { echo "-- dump"; }
verify_wp_admin_password() { cat >/dev/null; }
setup_db_passwords() { :; }
python3() { "$PY" "$@"; }
_helper() {
    PYTHONPATH="$SRC" "$PY" -c 'import sys
from onionpress.cli import OnionPressCLI as C
cli = object.__new__(C)
cmd, a = sys.argv[1], sys.argv[2:]
if cmd == "scrub-staging":
    sys.exit(C.cmd_scrub_staging(a[1]))
if cmd == "prune-image-derivatives":
    cli.log = lambda m: print(m, file=sys.stderr)
    sys.exit(cli.cmd_prune_image_derivatives(a[1]))
if cmd == "pack-backup":
    sys.exit(cli.cmd_pack_backup(a[1], a[3], sys.stdin.read()))
sys.exit(2)' "$@"
}
op_py() { _helper "$@"; }
'''
        out = os.path.join(self.tmp, "backup.zip")
        home = os.path.join(self.tmp, "sandbox-home")
        tmpdir = os.path.join(self.tmp, "t")
        os.makedirs(tmpdir)
        env = {
            "PATH": os.environ["PATH"], "HOME": home, "TMPDIR": tmpdir,
            "DATA_DIR": os.path.join(home, ".onionpress"),
            "LOG_FILE": os.path.join(self.tmp, "log"),
            "CALLS": os.path.join(self.tmp, "calls"),
            "SRC": mv.SRC, "PY": sys.executable, "PW": PW, "WP": wp,
            "ADDR": mv.ADDR, "WORDPRESS_DB_PASSWORD": "x",
            "ONIONPRESS_VERSION": "2.5.2",
            "DOCKER_HOST": "unix:///var/empty/no-docker-for-tests.sock",
        }
        r = subprocess.run(
            ["bash", "-c", prelude + self._create_backup_function()
             + f'\ncreate_backup "$PW" {out!r}\necho "rc=$?"'],
            capture_output=True, text=True, timeout=120, env=env)
        with open(env["LOG_FILE"]) as f:
            log = f.read()
        self.assertIn("rc=0", r.stdout, r.stderr + log)
        members = _inner_names_and_bytes(out, PW)
        self.assertFalse([n for n, data in members.items() if SECRET in data])
        names = {n.rstrip("/") for n in members}
        self.assertIn("wp-content/themes/flavor.css", names)
        self.assertNotIn("wp-content/themes/key.css", names)
        self.assert_home_untouched()
        self.assertIn("out of the backup (a symbolic link)", log)


if __name__ == "__main__":
    unittest.main()
