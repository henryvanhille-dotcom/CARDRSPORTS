import tempfile
import unittest
from pathlib import Path

import accounts
import database
from accounts import WorkspaceContext
from routes.vault_routes import _workspace_image_url


class UploadedFileOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database = database.DATABASE
        database.DATABASE = Path(self.temporary_directory.name) / "prospectr.db"
        accounts.initialize_accounts()

    def tearDown(self):
        database.DATABASE = self.original_database
        self.temporary_directory.cleanup()

    def test_claim_moves_a_guest_photo_but_never_takes_another_accounts_photo(self):
        guest_filename = "card-guest.jpg"
        protected_filename = "card-protected.jpg"
        accounts.record_uploaded_file(guest_filename, None)
        accounts.record_uploaded_file(protected_filename, "owner-a")

        accounts.claim_uploaded_files("owner-b", [guest_filename, protected_filename])

        self.assertEqual("owner-b", accounts.get_uploaded_file(guest_filename)["owner_id"])
        self.assertEqual("owner-a", accounts.get_uploaded_file(protected_filename)["owner_id"])

    def test_vault_rejects_another_accounts_private_upload(self):
        filename = "card-private.png"
        accounts.record_uploaded_file(filename, "owner-a")
        other_context = WorkspaceContext(owner_id="owner-b", user={"id": "owner-b"}, guest_mode=False)

        with self.assertRaisesRegex(ValueError, "your Cardr workspace"):
            _workspace_image_url("/uploads/" + filename, other_context)

    def test_local_guest_keeps_legacy_untracked_photo_compatibility(self):
        guest_context = WorkspaceContext(owner_id=None, user=None, guest_mode=True)
        self.assertEqual(
            "/uploads/card-legacy.webp",
            _workspace_image_url("/uploads/card-legacy.webp", guest_context),
        )


if __name__ == "__main__":
    unittest.main()
