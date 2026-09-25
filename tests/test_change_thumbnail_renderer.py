import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PIL import Image

from backend.change_thumbnail_renderer import ChangeThumbnailRenderer


class ChangeThumbnailRendererTests(unittest.TestCase):
    def test_cached_paths_are_versioned_and_uuid_validated(self):
        area_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            renderer = ChangeThumbnailRenderer(root=temporary, browser="/browser")
            self.addCleanup(renderer.close)
            self.assertEqual(
                renderer.cached_path(area_id),
                Path(temporary) / "v6" / f"{area_id}.webp",
            )
            with self.assertRaises(ValueError):
                renderer.cached_path("not-an-area")

    def test_render_converts_browser_capture_to_webp(self):
        area_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            renderer = ChangeThumbnailRenderer(
                root=temporary,
                browser="/browser",
                base_url="http://127.0.0.1:9999",
            )
            self.addCleanup(renderer.close)

            def capture(command, **_kwargs):
                screenshot = next(value for value in command if value.startswith("--screenshot="))
                path = Path(screenshot.split("=", 1)[1])
                image = Image.new("RGB", (768, 432))
                pixels = image.load()
                for y in range(432):
                    for x in range(768):
                        pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
                image.save(path, "PNG")
                return mock.Mock(returncode=0, stdout=b"", stderr=b"")

            with mock.patch("backend.change_thumbnail_renderer.subprocess.run", side_effect=capture):
                result = renderer.render(area_id)
            self.assertTrue(result.is_file())
            with Image.open(result) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (768, 432))


if __name__ == "__main__":
    unittest.main()
