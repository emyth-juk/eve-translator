import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Define paths relative to this script
script_dir = os.path.dirname(os.path.abspath(__file__))
# src/scripts -> src/assets
assets_dir = os.path.join(script_dir, '..', 'assets')
source = os.path.join(assets_dir, 'icon_source.png')
dest_ico = os.path.join(assets_dir, 'icon.ico')
dest_png = os.path.join(assets_dir, 'icon.png')
dest_banner = os.path.join(assets_dir, 'eve_translator_banner.png')

ICON_SIZE = 1024
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
BANNER_SIZE = (1600, 500)


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a clean UI font on Windows or common Linux build hosts."""
    candidates = [
        Path("C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def make_banner(icon: Image.Image) -> Image.Image:
    """Create the wide lockup used at the top of the GitHub README."""
    width, height = BANNER_SIZE
    banner = Image.new("RGB", BANNER_SIZE, (2, 10, 27))
    draw = ImageDraw.Draw(banner)

    # A restrained split-color rail echoes the source/target halves without
    # adding detail that competes with the selected mark.
    draw.rectangle((0, 0, width // 2, 8), fill=(33, 218, 240))
    draw.rectangle((width // 2, 0, width, 8), fill=(255, 183, 22))

    icon_size = 420
    icon_variant = icon.convert("RGB").resize(
        (icon_size, icon_size),
        Image.Resampling.LANCZOS,
    )
    banner.paste(icon_variant, (42, (height - icon_size) // 2))

    title_y = 130
    title_font = load_font(112, bold=True)
    subtitle_font = load_font(34)
    title_x = 510
    draw.text((title_x, title_y), "EVE", font=title_font, fill=(218, 248, 255))
    eve_width = draw.textbbox((0, 0), "EVE", font=title_font)[2]
    draw.text(
        (title_x + eve_width + 28, title_y),
        "Translator",
        font=title_font,
        fill=(255, 188, 35),
    )
    draw.text(
        (title_x + 3, 285),
        "REAL-TIME FLEET CHAT TRANSLATION",
        font=subtitle_font,
        fill=(102, 205, 225),
    )
    return banner

if not os.path.exists(source):
    print(f"Source icon not found at {source}")
    exit(1)

img = Image.open(source).convert("RGBA")

# Pad to square to avoid distortion when resizing down to small icon sizes.
width, height = img.size
side = max(width, height)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(img, ((side - width) // 2, (side - height) // 2))

# Save a power-of-two PNG for Qt, the tray, and the GitHub page.
app_icon = canvas.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
app_icon.save(dest_png, format="PNG", optimize=True)

# Save a multi-size ICO for Windows shell (covers common Explorer variants).
app_icon.save(dest_ico, format="ICO", sizes=ICO_SIZES)

# Save a wide lockup for the repository README.
make_banner(app_icon).save(dest_banner, format="PNG", optimize=True)

print(f"App icon saved to {dest_png} ({ICON_SIZE}x{ICON_SIZE})")
print(f"Windows icon saved to {dest_ico} with sizes: {ICO_SIZES}")
print(f"GitHub banner saved to {dest_banner} ({BANNER_SIZE[0]}x{BANNER_SIZE[1]})")
