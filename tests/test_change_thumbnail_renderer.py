import tempfile
import time
import unittest
import uuid
import base64
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
                Path(temporary) / "v8" / f"{area_id}.webp",
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

            def capture(_target, path, _profile):
                image = Image.new("RGB", (768, 432))
                pixels = image.load()
                for y in range(432):
                    for x in range(768):
                        pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
                image.save(path, "PNG")

            with mock.patch.object(renderer, "_capture_png", side_effect=capture):
                result = renderer.render(area_id)
            self.assertTrue(result.is_file())
            with Image.open(result) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (768, 432))

    def test_capture_waits_for_explicit_map_tile_readiness(self):
        class Page:
            def __init__(self):
                self.states = iter((False, False, True))
                self.methods = []
                self.deadlines = []

            def call(self, method, _params=None, **_kwargs):
                self.methods.append(method)
                self.deadlines.append(_kwargs["deadline"])
                if method == "Runtime.evaluate":
                    return {"result": {"value": '{"ready":%s,"error":null}' % str(next(self.states)).lower()}}
                if method == "Page.captureScreenshot":
                    image = Image.new("RGB", (768, 432), "navy")
                    with tempfile.NamedTemporaryFile(suffix=".png") as capture:
                        image.save(capture.name, "PNG")
                        return {"data": base64.b64encode(Path(capture.name).read_bytes()).decode()}
                raise AssertionError(method)

        with tempfile.TemporaryDirectory() as temporary:
            page = Page()
            output = Path(temporary) / "capture.png"
            with mock.patch("backend.change_thumbnail_renderer.time.sleep"):
                ChangeThumbnailRenderer._wait_and_capture(page, output, time.monotonic() + 1)
            self.assertTrue(output.is_file())
            self.assertEqual(page.methods.count("Runtime.evaluate"), 3)
            self.assertEqual(page.methods[-1], "Page.captureScreenshot")
            self.assertGreater(page.deadlines[-1], page.deadlines[-2])

    def test_thumbnail_page_has_no_fixed_time_readiness_fallback(self):
        javascript = (Path(__file__).parents[1] / "frontend" / "change-thumbnail.js").read_text()
        self.assertIn("map.areTilesLoaded()", javascript)
        self.assertIn("map.isSourceLoaded(snapshotSource)", javascript)
        self.assertIn("map.isSourceLoaded(changeSource)", javascript)
        self.assertNotIn("setTimeout(markReady", javascript)

    def test_thumbnail_attribution_starts_collapsed(self):
        javascript = (Path(__file__).parents[1] / "frontend" / "change-thumbnail.js").read_text()
        stylesheet = (Path(__file__).parents[1] / "frontend" / "change-thumbnail.css").read_text()
        self.assertIn("attributionControl: false", javascript)
        self.assertIn("new maplibregl.AttributionControl({ compact: true })", javascript)
        self.assertIn(".maplibregl-ctrl-attrib.maplibregl-compact .maplibregl-ctrl-attrib-inner", stylesheet)
        self.assertIn("display: none !important", stylesheet)


if __name__ == "__main__":
    unittest.main()
