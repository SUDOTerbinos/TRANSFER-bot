import json
import tempfile
import unittest
from pathlib import Path

from store import DestinationStore


class DestinationStoreTests(unittest.TestCase):
    def test_configuration_persists_and_can_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            store = DestinationStore(path)
            store.set(-1001, -1002, "Members lounge")

            reloaded = DestinationStore(path)
            self.assertEqual(reloaded.get(-1001).chat_id, -1002)
            self.assertEqual(reloaded.get(-1001).title, "Members lounge")
            self.assertTrue(reloaded.remove(-1001))
            self.assertIsNone(DestinationStore(path).get(-1001))

    def test_invalid_entries_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "destinations": {
                            "-1001": {"chat_id": "-1002", "title": "Valid"},
                            "bad": {"chat_id": "not-an-id"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = DestinationStore(path)
            self.assertEqual(store.get(-1001).title, "Valid")
            self.assertIsNone(store.get(-1002))


if __name__ == "__main__":
    unittest.main()
