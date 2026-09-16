"""Render archived fixture crops as a readable supplementary layout figure."""

import io
import json
from pathlib import Path

import reportlab
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

root = Path("artifacts/revision-20260915/boundary-cues-preflight")
rows = json.loads((root / "matrix.json").read_text())
fonts = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("FixtureSans", str(fonts / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("FixtureBold", str(fonts / "VeraBd.ttf")))
out = Path("reproduced/figures/natural-fixtures.pdf")
c = canvas.Canvas(str(out), pagesize=(470, 460))
c.setFont("FixtureBold", 12)
c.drawString(20, 440, "Visible boundaries in the natural-layout controls")
for index, layout in enumerate(("table", "cards", "form")):
    row = next(r for r in rows if r["layout"] == layout and r["task_id"] == "copy-known-value")
    image = Image.open(root / "fixture-previews" / f"{layout}-visible-None-copy-known-value.png")
    x, y = row["position"]["x"], row["position"]["y"]
    crop = image.crop((x, y, x + 630, y + 180))
    stream = io.BytesIO()
    crop.save(stream, format="PNG")
    top = 412 - index * 137
    c.setFont("FixtureBold", 9)
    c.drawString(
        20,
        top,
        {"table": "A  Separate table columns", "cards": "B  Neighboring cards", "form": "C  Labeled form fields"}[
            layout
        ],
    )
    c.drawImage(ImageReader(stream), 20, top - 128, width=430, height=430 * 180 / 630)
c.save()
print(out)
