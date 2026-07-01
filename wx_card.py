"""
wx_card.py — render the FCWXTH "current conditions" broadcast graphic from the
personal Weather Underground station (KALPHILC8). Pure Pillow; no network.

Design per FCWXTH_Current_Conditions_Graphic_Design_Specification.md:
  16:9 canvas @ 1920x1080, dark charcoal bg, gold/blue/white/light-gray hierarchy,
  glowing gold temperature, rounded metric cards, left-aligned header/footer.

render_conditions_card(data) -> PNG bytes. `data` keys (all optional, shown as
"--" when missing): tempF, feelsF, feels_label, humidity, dewF, wind_dir,
wind_mph, gust_mph, precip_rate, precip_accum, high_today, low_today, as_of.
"""
from __future__ import annotations
import io, os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.abspath(__file__))

STATION_NAME = "Franklin County Weather with Tim Haithcock"
STATION_ID   = "KALPHILC8"
STATION_LOC  = "Phil Campbell, AL"

# --- Brand palette (spec hex values) ----------------------------------------
BG_TOP    = (0x11, 0x14, 0x17)   # #111417 primary
BG_BOT    = (0x10, 0x12, 0x14)   # #101214 alternate
GOLD      = (0xF4, 0xB3, 0x21)   # #F4B321 — title, temp, degree, bullet
BLUE      = (0x2F, 0x8C, 0xFF)   # #2F8CFF — CURRENT CONDITIONS, Feels Like
WHITE     = (0xFF, 0xFF, 0xFF)   # weather values, wind speed, footer text
LIGHT_GRAY= (0xC8, 0xCD, 0xD3)   # card headings, secondary info, city name
DIVIDER   = (0x34, 0x47, 0x5A)   # #34475A — divider lines, 70-80% opacity, 2px
CARD_BG   = (0x1D, 0x28, 0x35)   # #1D2835 — card background
CARD_BORDER = (0x34, 0x47, 0x5A)  # #34475A — card border, subtle 1px

W, H = 1920, 1080


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for p in (
        os.path.join(ROOT, "assets", "fonts", name),                 # bundled
        f"/usr/share/fonts/truetype/dejavu/{name}",                  # linux runner
        ("C:/Windows/Fonts/" + ("arialbd.ttf" if bold else "arial.ttf")),  # windows
    ):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _vgradient(w: int, h: int, top, bot) -> Image.Image:
    """Very subtle top-to-bottom gradient between two close dark tones."""
    base = Image.new("RGB", (w, h), top)
    grad = Image.new("L", (1, h))
    for y in range(h):
        grad.putpixel((0, y), int(255 * y / h))
    alpha = grad.resize((w, h))
    base.paste(Image.new("RGB", (w, h), bot), (0, 0), alpha)
    return base


def _txt(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _fit(s: str, max_w: int, max_size: int, bold: bool = True, floor: int = 30) -> ImageFont.FreeTypeFont:
    """Largest font (down to `floor`) whose rendered width of s fits max_w."""
    for size in range(max_size, floor - 1, -2):
        f = _font(size, bold=bold)
        if f.getlength(s) <= max_w:
            return f
    return _font(floor, bold=bold)


def _val(v, suffix="", dash="--"):
    return f"{v}{suffix}" if v not in (None, "") else dash


def _glow_text(img: Image.Image, xy, s, font, fill, glow_color=GOLD,
               glow_opacity: float = 0.15, blur_radius: int = 14) -> None:
    """Draw text with a soft outer glow (spec: ~15% opacity) behind crisp text."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    a = int(255 * glow_opacity)
    ld.text(xy, s, font=font, fill=(*glow_color, a))
    layer = layer.filter(ImageFilter.GaussianBlur(blur_radius))
    img.alpha_composite(layer)
    ImageDraw.Draw(img).text(xy, s, font=font, fill=fill)


def _card(od: ImageDraw.ImageDraw, box, radius=20):
    """Metric card: filled background + 1px border, per spec."""
    od.rounded_rectangle(box, radius=radius, fill=(*CARD_BG, 255))
    od.rounded_rectangle(box, radius=radius, outline=(*CARD_BORDER, 255), width=1)


def render_conditions_card(data: dict) -> bytes:
    img = _vgradient(W, H, BG_TOP, BG_BOT).convert("RGBA")

    f_title  = _font(58, bold=True)
    f_label  = _font(34, bold=True)
    f_city   = _font(32)
    f_temp   = _font(260, bold=True)
    f_feels  = _font(46, bold=True)
    f_tile_k = _font(26, bold=True)
    f_foot   = _font(26)
    f_foot_b = _font(26, bold=True)

    PAD = 80   # outer margin, per spec "consistent margins"

    # ---- Cards (wind/gust hero + 2x3 metric grid), drawn on the base image so
    # borders/fills are solid, then metric text drawn after.
    wind_val = (f"{_val(data.get('wind_dir'))} "
                f"{_val(data.get('wind_mph'))} / {_val(data.get('gust_mph'))} mph")
    grid = [
        ("DEWPOINT",     _val(data.get("dewF"), "°F")),
        ("HUMIDITY",     _val(data.get("humidity"), "%")),
        ("PRESSURE",     _val(data.get("pressure_in"), " in")),
        ("PRECIP RATE",  _val(data.get("precip_rate"), " in/hr")),
        ("PRECIP ACCUM", _val(data.get("precip_accum"), " in")),
        ("TODAY HIGH / LOW",
         f"{_val(data.get('high_today'), '°')} / {_val(data.get('low_today'), '°')}"),
    ]

    # Vertical rhythm (calibrated against measured glyph bounding boxes):
    #   header 0-210 | main section 224-560 | 2x3 grid 596-882 | footer 916-1080
    wx0, wy0, wx1, wy1 = 1000, 248, W - PAD, 466   # wind & gust hero card (right)
    gx, gy = PAD, 596                               # 2x3 grid, left col start
    gcols, ggap = 3, 26
    gtw = (wx1 - gx - (gcols - 1) * ggap) // gcols
    gth = 130

    od = ImageDraw.Draw(img)
    _card(od, [wx0, wy0, wx1, wy1])
    for i in range(len(grid)):
        col, row = i % gcols, i // gcols
        x = gx + col * (gtw + ggap)
        y = gy + row * (gth + ggap)
        _card(od, [x, y, x + gtw, y + gth])

    # ---- Header (left aligned): title / CURRENT CONDITIONS • city / divider
    _txt(od, (PAD, 56), STATION_NAME, f_title, GOLD)
    _txt(od, (PAD, 134), "CURRENT CONDITIONS", f_label, BLUE)
    label_w = f_label.getlength("CURRENT CONDITIONS")
    bullet_x = PAD + label_w + 22
    _txt(od, (bullet_x, 134), "•", f_label, GOLD)
    _txt(od, (bullet_x + 26, 138), STATION_LOC, f_city, LIGHT_GRAY)
    od.line([(PAD, 210), (W - PAD, 210)], fill=(*DIVIDER, 200), width=2)

    # ---- Main section: left = big glowing temp + feels like; right = wind card
    _glow_text(img, (PAD - 6, 224), _val(data.get("tempF"), "°"), f_temp, GOLD)
    od = ImageDraw.Draw(img)  # re-grab draw handle after glow compositing
    fl = data.get("feels_label", "Feels Like")
    _txt(od, (PAD + 4, 500), f"{fl}", f_feels, BLUE)
    fl_w = f_feels.getlength(f"{fl}  ")
    _txt(od, (PAD + 4 + fl_w, 500), _val(data.get("feelsF"), "°"), f_feels, WHITE)

    _txt(od, (wx0 + 32, wy0 + 26), "WIND & GUST", f_tile_k, LIGHT_GRAY)
    _txt(od, (wx0 + 32, wy0 + 70), wind_val,
         _fit(wind_val, (wx1 - wx0) - 64, 60, floor=32), WHITE)

    # ---- Bottom grid text (card internal padding 24-32px per spec)
    for i, (k, v) in enumerate(grid):
        col, row = i % gcols, i // gcols
        x = gx + col * (gtw + ggap)
        y = gy + row * (gth + ggap)
        _txt(od, (x + 28, y + 24), k, f_tile_k, LIGHT_GRAY)
        _txt(od, (x + 28, y + 64), v, _fit(v, gtw - 56, 50, floor=30), WHITE)

    # ---- Footer (left aligned): timestamp / "Observed at our station NAME · ID · source"
    # Spec: White bucket explicitly lists "footer text" AND "station name in footer" —
    # so the whole footer is white; station name gets bold for emphasis.
    foot_y = 916
    od.line([(PAD, foot_y), (W - PAD, foot_y)], fill=(*DIVIDER, 200), width=2)
    _txt(od, (PAD, foot_y + 26), data.get("as_of", ""), f_foot, WHITE)
    line2_x = PAD
    _txt(od, (line2_x, foot_y + 62), 'Observed at our station "', f_foot, WHITE)
    line2_x += f_foot.getlength('Observed at our station "')
    _txt(od, (line2_x, foot_y + 62), STATION_NAME, f_foot_b, WHITE)
    line2_x += f_foot_b.getlength(STATION_NAME)
    _txt(od, (line2_x, foot_y + 62), f'" · {STATION_ID} · via Weather Underground',
         f_foot, WHITE)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


if __name__ == "__main__":
    sample = {
        "tempF": 93.9, "feelsF": 114.1, "feels_label": "Feels Like",
        "dewF": 80.3, "humidity": 65, "pressure_in": 30.26,
        "wind_dir": "NE", "wind_mph": 0.0, "gust_mph": 1.0,
        "precip_rate": 0.00, "precip_accum": 0.00,
        "high_today": 97, "low_today": 73,
        "as_of": "As of 3:00 PM CDT · Jun 30, 2026",
    }
    png = render_conditions_card(sample)
    path = os.path.join(os.environ.get("PREVIEW_OUT", ROOT), "card_preview.png")
    with open(path, "wb") as fh:
        fh.write(png)
    print("wrote", path, len(png), "bytes")
