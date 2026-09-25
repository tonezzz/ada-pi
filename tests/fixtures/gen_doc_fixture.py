#!/usr/bin/env python3
"""Generate tests/fixtures/doc-receipt.png — a synthetic receipt photo for
the doc-upload live scenario (upload -> /api/documents/intake -> Ada asks
what to do with it). Pure PIL; rerun to regenerate.

    python3 tests/fixtures/gen_doc_fixture.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path(__file__).with_name("doc-receipt.png")
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

W, H = 1000, 1400


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def main() -> None:
    im = Image.new("RGB", (W, H), (245, 243, 238))
    d = ImageDraw.Draw(im)
    mono = font("DejaVuSansMono.ttf", 30)
    mono_sm = font("DejaVuSansMono.ttf", 26)
    bold = font("DejaVuSans-Bold.ttf", 38)

    y = 40
    for line, f in [
        ("CHABA MART", bold),
        ("123/45 Sukhumvit Rd, Bangkok", mono_sm),
        ("Tel 02-555-0100", mono_sm),
        ("TAX INVOICE (ABB)", mono_sm),
    ]:
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, y), line, fill=(20, 20, 20), font=f)
        y += f.size + 14

    y += 10
    d.line((40, y, W - 40, y), fill=(20, 20, 20), width=2)
    y += 16

    items = [
        ("Coffee beans 250g", "145.00"),
        ("Green tea x2", "90.00"),
        ("Notebook A5", "59.00"),
        ("USB-C cable 1m", "189.00"),
    ]
    for name, price in items:
        d.text((48, y), name, fill=(20, 20, 20), font=mono)
        pw = d.textlength(price, font=mono)
        d.text((W - 48 - pw, y), price, fill=(20, 20, 20), font=mono)
        y += mono.size + 18

    y += 8
    d.line((40, y, W - 40, y), fill=(20, 20, 20), width=2)
    y += 16
    for name, price, f in [
        ("TOTAL", "483.00", bold),
        ("CASH", "500.00", mono),
        ("CHANGE", "17.00", mono),
    ]:
        d.text((48, y), name, fill=(20, 20, 20), font=f)
        pw = d.textlength(price, font=f)
        d.text((W - 48 - pw, y), price, fill=(20, 20, 20), font=f)
        y += f.size + 18

    y += 20
    for line in [
        "Date: 25/09/2026  14:32",
        "Receipt No: R-2026-09917",
        "Cashier: NOK",
        "",
        "*** THANK YOU ***",
    ]:
        w = d.textlength(line, font=mono_sm)
        d.text(((W - w) / 2, y), line, fill=(20, 20, 20), font=mono_sm)
        y += mono_sm.size + 12

    # Slight desk-photo feel: a touch of blur.
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(OUT)
    print(f"wrote {OUT} ({im.size[0]}x{im.size[1]})")


if __name__ == "__main__":
    main()
