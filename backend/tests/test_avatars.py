import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from nio import UploadResponse
from PIL import Image

from app.avatars import (
    AvatarManager,
    BEIJING,
    default_steve_head_png,
    render_head_png,
    seconds_until_next_sunday_midnight,
)
from app.database import Database


class FakeMatrixUploadClient:
    def __init__(self) -> None:
        self.uploads = []

    async def upload(self, data_provider, **kwargs):
        data = data_provider.read()
        self.uploads.append((data, kwargs))
        return UploadResponse(f"mxc://example.org/avatar{len(self.uploads)}"), None


class AvatarTest(unittest.IsolatedAsyncioTestCase):
    def test_default_steve_avatar_is_a_32_pixel_png(self) -> None:
        png = default_steve_head_png()
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual((32, 32), image.size)
            self.assertEqual("RGBA", image.mode)

    def test_skin_face_and_outer_layer_are_rendered(self) -> None:
        skin = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(8, 16):
            for y in range(8, 16):
                skin.putpixel((x, y), (200, 100, 50, 255))
        skin.putpixel((40, 8), (0, 255, 0, 255))
        source = io.BytesIO()
        skin.save(source, format="PNG")

        rendered = render_head_png(source.getvalue())
        with Image.open(io.BytesIO(rendered)) as head:
            self.assertEqual((32, 32), head.size)
            self.assertEqual((0, 255, 0, 255), head.getpixel((0, 0)))
            self.assertEqual((200, 100, 50, 255), head.getpixel((8, 8)))

    def test_next_refresh_is_sunday_midnight_beijing(self) -> None:
        saturday = datetime(2026, 9, 19, 23, 0, tzinfo=BEIJING)
        self.assertEqual(3600, seconds_until_next_sunday_midnight(saturday))
        sunday_midnight = datetime(2026, 9, 20, 0, 0, tzinfo=BEIJING)
        self.assertEqual(
            7 * 24 * 60 * 60,
            seconds_until_next_sunday_midnight(sunday_midnight),
        )

    async def test_successful_refresh_is_stored_locally_and_in_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "bridge.db")
            client = FakeMatrixUploadClient()
            manager = AvatarManager(
                database, client, Path(directory) / "avatars", 8
            )
            manager.avatar_directory.mkdir()
            manager._session = object()

            async def fake_fetch(player_id):
                return (
                    "0123456789abcdef0123456789abcdef",
                    "b" * 64,
                    _test_skin_png(),
                )

            manager._fetch_skin = fake_fetch
            await manager._refresh_player("Steve")

            record = database.get_player_avatar("steve")
            self.assertFalse(record.refresh_failed)
            self.assertEqual("mxc://example.org/avatar1", record.mxc_uri)
            self.assertTrue(Path(record.local_path).is_file())
            database.close()


def _test_skin_png() -> bytes:
    image = Image.new("RGBA", (64, 64), (180, 120, 80, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
