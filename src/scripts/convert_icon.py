from PIL import Image
import os

# Define paths relative to this script
script_dir = os.path.dirname(os.path.abspath(__file__))
# src/scripts -> src/assets
assets_dir = os.path.join(script_dir, '..', 'assets')
source = os.path.join(assets_dir, 'icon_source.png')
dest_ico = os.path.join(assets_dir, 'icon.ico')
dest_png = os.path.join(assets_dir, 'icon.png')

if not os.path.exists(source):
    print(f"Source icon not found at {source}")
    exit(1)

img = Image.open(source).convert("RGBA")

# Pad to square to avoid distortion when resizing down to small icon sizes.
width, height = img.size
side = max(width, height)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(img, ((side - width) // 2, (side - height) // 2))

# Save the full-size PNG for app use.
canvas.save(dest_png, format="PNG")

# Save a multi-size ICO for Windows shell (covers common Explorer variants).
ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
canvas.save(dest_ico, format="ICO", sizes=ico_sizes)

print(f"Icon converted successfully to {dest_ico} with sizes: {ico_sizes}")
