import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from farhang.data import ROOT, load_items
from farhang.models import AnthropicModel, OpenAICompatibleModel, item_images

IMAGE_ITEM = load_items(ROOT / "data" / "derived" / "met_carpets.jsonl")[0]


class ImageAdapterTest(unittest.TestCase):
    def test_item_images(self):
        self.assertEqual(item_images({"prompt": "x"}), [])
        (media_type, data), = item_images(IMAGE_ITEM)
        self.assertEqual(media_type, "image/jpeg")
        self.assertTrue(data.startswith("/9j/"))  # base64 of a JPEG header

    def test_anthropic_sends_image_blocks_before_text(self):
        sent = {}

        def create(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="B")], stop_reason="end_turn")

        model = object.__new__(AnthropicModel)
        model.client = SimpleNamespace(messages=SimpleNamespace(create=create))
        model.model, model.effort, model.max_tokens = "m", None, 100
        self.assertEqual(model.complete("prompt", IMAGE_ITEM), "B")
        content = sent["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["image", "text"])
        self.assertNotIn("temperature", sent)

    def test_openai_sends_data_uri(self):
        captured = {}

        def urlopen(req, timeout):
            captured["body"] = json.loads(req.data)
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "B"}}]}).encode())

        model = OpenAICompatibleModel("m", "http://localhost:1/v1")
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(model.complete("prompt", IMAGE_ITEM), "B")
            content = captured["body"]["messages"][0]["content"]
            self.assertEqual(content[0], {"type": "text", "text": "prompt"})
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            model.complete("text only", {"prompt": "x"})
            self.assertEqual(captured["body"]["messages"][0]["content"], "text only")
