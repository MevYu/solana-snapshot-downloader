import unittest

from downloader.models import Candidate, http_url, snapshot_from_url


class ModelTests(unittest.TestCase):
    def test_rejects_unsafe_or_malformed_urls_and_filenames(self):
        for value in ("file:///tmp/x", "http://user:password@node/x", "http://node:99999/x",
                      "http://node:8900http://node:8899/x", "http://node/\nfile", "http://node:0/"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                http_url(value)
        for filename in ("../foo", "snapshot-1-A.tar.zst", "snapshot-1-2-" + "A" * 44 + ".tar.zst",
                         "incremental-snapshot-2-1-" + "A" * 44 + ".tar.zst"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                snapshot_from_url("http://node/" + filename)

    def test_candidate_rejects_wrong_base(self):
        full = snapshot_from_url("http://node/snapshot-100-" + "A" * 44 + ".tar.zst")
        inc = snapshot_from_url("http://node/incremental-snapshot-99-110-" + "B" * 44 + ".tar.zst")
        with self.assertRaises(ValueError):
            Candidate("http://node/", full, inc)


if __name__ == "__main__":
    unittest.main()
