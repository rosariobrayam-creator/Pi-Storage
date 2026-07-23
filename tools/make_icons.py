"""Generate the Pi-Storage mark: two SVGs and the PNG app icons.

The mark is a camera aperture with a raspberry leaf on top. Geometry is defined
once here, in a 48-unit viewBox, and everything is emitted from it -- the header
logo, the favicon, and the iOS/Android icons -- so the shapes can never drift
apart. iOS ignores SVG for home screen icons, which is why PNGs exist at all.

Run on a dev machine and commit the output; the Pi never needs this:
    python tools/make_icons.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"

PAPER = (244, 239, 230)
INK = (43, 35, 32)
BERRY = (196, 43, 79)
PAPER_HEX, INK_HEX, BERRY_HEX = "#F4EFE6", "#2B2320", "#C42B4F"

# The colour wheel the header logo animates through (see app.css blade-hue).
# Icons are a frozen frame of it: six blades + the hexagon, one hue each.
WHEEL_HEX = ["#E0435C", "#E8833A", "#E5B93C", "#7DBB6C",
             "#4FB8A8", "#5B8DD9", "#9B6BD9"]
WHEEL = [(224, 67, 92), (232, 131, 58), (229, 185, 60), (125, 187, 108),
         (79, 184, 168), (91, 141, 217), (155, 107, 217)]

SS = 4  # supersample factor for the PNGs
VIEW = 48.0  # viewBox units

CX, CY = 24.0, 29.0  # aperture centre
RING_R = 12.5  # outer ring radius
INNER_R = 5.0  # aperture opening radius
DOT_R = 2.6  # centre dot
BLADE_SWEEP = 58.0  # degrees a blade leans; what makes it read as an aperture
STROKE = 2.0
RING_STROKE = 2.2


def _pt(radius, deg):
    a = math.radians(deg)
    return (CX + radius * math.cos(a), CY + radius * math.sin(a))


def _bezier(p0, c1, c2, p3, steps=24):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u**3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * p3[1],
        ))
    return out


# Six blade vertices around the opening, starting at the top.
ANGLES = [-90 + i * 60 for i in range(6)]
VERTS = [_pt(INNER_R, a) for a in ANGLES]

# Icon separators between the coloured pie pieces: much thinner than the
# brand line work, so the colour dominates and the ink just divides.
THIN = 1.1


def _sector(i, steps=12):
    """The pie piece between blade i and blade i+1: out along blade i, around
    the ring arc, back in along blade i+1, closed by the hex edge."""
    a0 = ANGLES[i] + BLADE_SWEEP
    a1 = ANGLES[i] + 60 + BLADE_SWEEP
    pts = [VERTS[i], _pt(RING_R, a0)]
    for s in range(1, steps + 1):
        pts.append(_pt(RING_R, a0 + (a1 - a0) * s / steps))
    pts.append(VERTS[(i + 1) % 6])
    return pts

# The opening: a hexagon. Each blade then leans out to the ring, all the same
# direction, which is what gives an aperture its characteristic swirl. Critically
# these do NOT pass through the centre -- lines through the centre read as a
# wagon wheel, not a camera.
HEX = [VERTS[i] for i in range(6)] + [VERTS[0]]
BLADES = [(VERTS[i], _pt(RING_R, ANGLES[i] + BLADE_SWEEP)) for i in range(6)]

# Raspberry leaf pair, sitting on top of the ring.
LEAF_L = (_bezier((24, 11.5), (22.5, 8.2), (19.6, 6.8), (16.8, 7.2))
          + _bezier((16.8, 7.2), (17.0, 10.0), (18.6, 12.4), (21.3, 13.6)))
LEAF_R = (_bezier((24, 11.5), (25.5, 8.2), (28.4, 6.8), (31.2, 7.2))
          + _bezier((31.2, 7.2), (31.0, 10.0), (29.4, 12.4), (26.7, 13.6)))


# ------------------------------------------------------------------ PNG


def draw_mark(size: int, content_fraction: float) -> Image.Image:
    """Full-bleed cream square with the mark centred at content_fraction scale."""
    px = size * SS
    img = Image.new("RGB", (px, px), PAPER)
    d = ImageDraw.Draw(img)

    scale = px * content_fraction / VIEW
    offset = (px - VIEW * scale) / 2

    def P(pt):
        return (offset + pt[0] * scale, offset + pt[1] * scale)

    def stroke(points, width_units, colour=INK):
        w = max(1, round(width_units * scale))
        pts = [P(p) for p in points]
        d.line(pts, fill=colour, width=w, joint="curve")
        r = w / 2  # PIL has no round caps, so cap the ends by hand
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=colour)

    # Colour first: each pie piece between two blades gets a wheel hue.
    for i in range(6):
        d.polygon([P(p) for p in _sector(i)], fill=WHEEL[i])

    # Ink on top: full-weight brand lines, thin separators between pieces.
    stroke(LEAF_L, STROKE)
    stroke(LEAF_R, STROKE)

    cx, cy = P((CX, CY))
    rr = RING_R * scale
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=INK,
              width=max(1, round(RING_STROKE * scale)))

    stroke(HEX, THIN)
    for a, b in BLADES:
        stroke([a, b], THIN)

    dr = DOT_R * scale
    d.ellipse([cx - dr, cy - dr, cx + dr, cy + dr], fill=BERRY)

    return img.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------------ SVG


def _poly(points):
    return " ".join(("M" if i == 0 else "L") + f"{x:.2f} {y:.2f}"
                    for i, (x, y) in enumerate(points))


def _shapes(stroke_colour: str, dot_colour: str, animated: bool = False) -> str:
    """The mark's SVG body.

    animated=True (header logo only) fills the aperture with the same coloured
    pie pieces as the app icon and wraps them in <g class="lens">, each piece
    classed seg-N, so app.css can spin the lens and cycle every fill through
    the colour wheel. The favicon-outline variant stays static ink.
    """
    if animated:
        parts = [
            f'<g fill="none" stroke="{stroke_colour}" stroke-width="{STROKE}"'
            ' stroke-linecap="round" stroke-linejoin="round">',
            f'  <path d="{_poly(LEAF_L)}"/>',
            f'  <path d="{_poly(LEAF_R)}"/>',
            f'  <circle cx="{CX}" cy="{CY}" r="{RING_R}" stroke-width="{RING_STROKE}"/>',
            "</g>",
            '<g class="lens">',
        ]
        for i in range(6):
            parts.append(
                f'  <path class="seg seg-{i}" d="{_poly(_sector(i))} Z"'
                f' fill="{WHEEL_HEX[i]}"/>'
            )
        parts.append(
            f'  <g fill="none" stroke="{stroke_colour}" stroke-width="{THIN}"'
            ' stroke-linecap="round" stroke-linejoin="round">'
        )
        parts.append(f'    <path d="{_poly(HEX)}"/>')
        for a, b in BLADES:
            parts.append(f'    <path d="M{a[0]:.2f} {a[1]:.2f}L{b[0]:.2f} {b[1]:.2f}"/>')
        parts.append("  </g>")
        parts.append("</g>")
        parts.append(f'<circle cx="{CX}" cy="{CY}" r="{DOT_R}" fill="{dot_colour}"/>')
        return "\n".join(parts)

    parts = [
        f'<g fill="none" stroke="{stroke_colour}" stroke-width="{STROKE}"'
        ' stroke-linecap="round" stroke-linejoin="round">',
        f'  <path d="{_poly(LEAF_L)}"/>',
        f'  <path d="{_poly(LEAF_R)}"/>',
        f'  <circle cx="{CX}" cy="{CY}" r="{RING_R}" stroke-width="{RING_STROKE}"/>',
        f'  <path d="{_poly(HEX)}"/>',
    ]
    for a, b in BLADES:
        parts.append(f'  <path d="M{a[0]:.2f} {a[1]:.2f}L{b[0]:.2f} {b[1]:.2f}"/>')
    parts.append("</g>")
    parts.append(f'<circle cx="{CX}" cy="{CY}" r="{DOT_R}" fill="{dot_colour}"/>')
    return "\n".join(parts)


def _shapes_filled() -> str:
    """Icon variant: coloured pie pieces under thin ink separators."""
    parts = []
    for i in range(6):
        parts.append(f'<path d="{_poly(_sector(i))} Z" fill="{WHEEL_HEX[i]}"/>')
    parts.append(
        f'<g fill="none" stroke="{INK_HEX}"'
        ' stroke-linecap="round" stroke-linejoin="round">'
    )
    parts.append(f'  <path stroke-width="{STROKE}" d="{_poly(LEAF_L)}"/>')
    parts.append(f'  <path stroke-width="{STROKE}" d="{_poly(LEAF_R)}"/>')
    parts.append(
        f'  <circle stroke-width="{RING_STROKE}" cx="{CX}" cy="{CY}" r="{RING_R}"/>'
    )
    parts.append(f'  <path stroke-width="{THIN}" d="{_poly(HEX)}"/>')
    for a, b in BLADES:
        parts.append(
            f'  <path stroke-width="{THIN}"'
            f' d="M{a[0]:.2f} {a[1]:.2f}L{b[0]:.2f} {b[1]:.2f}"/>'
        )
    parts.append("</g>")
    parts.append(f'<circle cx="{CX}" cy="{CY}" r="{DOT_R}" fill="{BERRY_HEX}"/>')
    return "\n".join(parts)


def write_svgs() -> None:
    # Header logo: inherits text colour so it works on cream and on charcoal.
    (TEMPLATES / "_logo.svg").write_text(
        "<!-- Generated by tools/make_icons.py -- edit the geometry there, not here.\n"
        "     Pi-Storage mark: a camera aperture with a raspberry leaf. Line work in\n"
        "     currentColor so it follows the surrounding text; the pie pieces carry\n"
        "     the same wheel hues as the app icon. No external refs. -->\n"
        '<svg class="logo" viewBox="0 0 48 48" role="img" aria-label="Pi-Storage"\n'
        '     xmlns="http://www.w3.org/2000/svg">\n'
        + _shapes("currentColor", f"var(--berry, {BERRY_HEX})", animated=True)
        + "\n</svg>\n",
        encoding="utf-8",
    )
    # Favicon: fixed colours, own background, since it sits on browser chrome.
    (STATIC / "icon.svg").write_text(
        "<!-- Generated by tools/make_icons.py -->\n"
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">\n'
        f'<rect width="48" height="48" rx="10" fill="{PAPER_HEX}"/>\n'
        + _shapes_filled()
        + "\n</svg>\n",
        encoding="utf-8",
    )
    print(f"wrote {TEMPLATES / '_logo.svg'}")
    print(f"wrote {STATIC / 'icon.svg'}")


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    write_svgs()
    targets = [
        # iOS crops to a squircle, so the mark sits smaller on the touch icon.
        ("icon-180.png", 180, 0.66),
        ("icon-192.png", 192, 0.72),
        ("icon-512.png", 512, 0.72),
        # Android masks to a circle; stay inside the central 80% safe zone.
        ("icon-512-maskable.png", 512, 0.56),
    ]
    for name, size, frac in targets:
        draw_mark(size, frac).save(STATIC / name, "PNG", optimize=True)
        print(f"wrote {STATIC / name}  ({size}x{size})")


if __name__ == "__main__":
    main()
