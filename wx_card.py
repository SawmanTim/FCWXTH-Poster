"""
wx_card.py — render a branded "current conditions" graphic from the personal
Weather Underground station (KALPHILC8). Pure Pillow; no network.

render_conditions_card(data) -> PNG bytes.  `data` keys (all optional, shown as
"--" when missing):
    tempF, feelsF, feels_label, humidity, dewF, wind_dir, wind_mph, gust_mph,
    precip_rate, precip_today, as_of  (already-formatted local time string)
"""
from __future__ import annotations
import io, os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.abspath(__file__))

# Station identity — every card is attributed to it.
STATION_NAME = "Franklin County Weather with Tim Haithcock"
STATION_ID   = "KALPHILC8"
STATION_LOC  = "Phil Campbell, AL"

# Palette
BG_TOP   = (11, 61, 102)     # deep blue
BG_BOT   = (8, 32, 56)       # darker blue
CARD     = (255, 255, 255, 18)
WHITE    = (255, 255, 255)
MUTED    = (183, 205, 224)
ACCENT   = (255, 184, 28)    # amber (temperature)
ACCENT2  = (120, 200, 255)   # light blue (feels-like)

W, H = 1200, 800


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
    base = Image.new("RGB", (w, h), top)
    grad = Image.new("L", (1, h))
    for y in range(h):
        grad.putpixel((0, y), int(255 * y / h))
    alpha = grad.resize((w, h))
    base.paste(Image.new("RGB", (w, h), bot), (0, 0), alpha)
    return base


def _txt(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _fit(s: str, max_w: int, max_size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Largest font (down to 24) whose rendered width of s fits max_w."""
    for size in range(max_size, 23, -2):
        f = _font(size, bold=bold)
        if f.getlength(s) <= max_w:
            return f
    return _font(24, bold=bold)


def _val(v, suffix="", dash="--"):
    return f"{v}{suffix}" if v not in (None, "") else dash


def render_conditions_card(data: dict) -> bytes:
    img = _vgradient(W, H, BG_TOP, BG_BOT).convert("RGBA")

    f_title = _font(34, bold=True)
    f_label = _font(25, bold=True)
    f_small = _font(23)
    f_temp  = _font(150, bold=True)
    f_feels = _font(38, bold=True)
    f_tile_k= _font(21, bold=True)
    f_foot  = _font(22)

    DIV  = (90, 122, 156)          # subtle divider (solid, so it survives RGB save)
    TILE = (255, 255, 255, 30)     # translucent tile (drawn on overlay, composited)

    # Top-right hero tile (wind & gust), then a 3x2 grid of the remaining metrics —
    # mirrors the station's own Current Conditions panel.
    wind_val = (f"{_val(data.get('wind_dir'))} "
                f"{_val(data.get('wind_mph'))} / {_val(data.get('gust_mph'))} mph")
    grid = [
        ("DEWPOINT",     _val(data.get("dewF"), " °F")),
        ("HUMIDITY",     _val(data.get("humidity"), " %")),
        ("PRESSURE",     _val(data.get("pressure_in"), " in")),
        ("PRECIP RATE",  _val(data.get("precip_rate"), " in/hr")),
        ("PRECIP ACCUM", _val(data.get("precip_accum"), " in")),
        ("TODAY HIGH / LOW",
         f"{_val(data.get('high_today'), '°')} / {_val(data.get('low_today'), '°')}"),
    ]
    wx0, wy0, wx1, wy1 = 628, 178, 1140, 300            # wind & gust tile
    gx, gy, tw, th, gap = 60, 422, 344, 122, 24          # 3-col grid

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle([wx0, wy0, wx1, wy1], radius=18, fill=TILE)
    for i in range(len(grid)):
        col, row = i % 3, i // 3
        x, y = gx + col * (tw + gap), gy + row * (th + gap)
        od.rounded_rectangle([x, y, x + tw, y + th], radius=18, fill=TILE)
    img = Image.alpha_composite(img, overlay)

    d = ImageDraw.Draw(img)

    # Header (single title line; location on the subline; time goes in the footer)
    _txt(d, (60, 46), STATION_NAME, f_title, WHITE)
    _txt(d, (60, 96), "CURRENT CONDITIONS", f_label, ACCENT)
    _txt(d, (398, 98), f"·  {STATION_LOC}", f_small, MUTED)
    d.line([(60, 150), (W - 60, 150)], fill=DIV, width=2)

    # Big temperature (left) + feels-like
    _txt(d, (60, 178), _val(data.get("tempF"), "°"), f_temp, ACCENT)
    fl = data.get("feels_label", "Feels Like")
    _txt(d, (70, 350), f"{fl}  {_val(data.get('feelsF'), '°')}", f_feels, ACCENT2)

    # Wind & gust hero tile
    _txt(d, (wx0 + 24, wy0 + 18), "WIND & GUST", f_tile_k, MUTED)
    _txt(d, (wx0 + 24, wy0 + 52), wind_val, _fit(wind_val, (wx1 - wx0) - 48, 48), WHITE)

    # Metric grid text (after compositing so values are readable)
    for i, (k, v) in enumerate(grid):
        col, row = i % 3, i // 3
        x, y = gx + col * (tw + gap), gy + row * (th + gap)
        _txt(d, (x + 20, y + 20), k, f_tile_k, MUTED)
        _txt(d, (x + 20, y + 54), v, _fit(v, tw - 40, 46), WHITE)

    # Footer — timestamp + attribution to the station
    d.line([(60, H - 96), (W - 60, H - 96)], fill=DIV, width=2)
    _txt(d, (60, H - 78), data.get("as_of", ""), f_foot, WHITE)
    _txt(d, (60, H - 44),
         f'Observed at our station "{STATION_NAME}" · {STATION_ID} '
         f"· via Weather Underground", f_foot, MUTED)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()


if __name__ == "__main__":
    # Preview with sample readings observed on KALPHILC8.
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
