"""Generate PWA icons for DoukHub."""
from PIL import Image, ImageDraw, ImageFont
import os

out_dir = os.path.dirname(os.path.abspath(__file__))
bg = (0, 97, 164, 255)   # #0061A4
fg = (255, 255, 255, 255)  # white

for size in [192, 512]:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 12
    radius = size // 5
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=bg,
    )
    try:
        font = ImageFont.truetype("arial.ttf", int(size * 0.55))
    except (IOError, OSError):
        font = ImageFont.load_default()
    text = "D"
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), text, fill=fg, font=font)
    path = os.path.join(out_dir, f"icon-{size}.png")
    img.save(path)
    print(f"saved {path}")

# apple-touch-icon (180x180, no transparency)
img192 = Image.open(os.path.join(out_dir, "icon-192.png"))
img180 = img192.resize((180, 180), Image.LANCZOS)
# Fill transparent corners with white
bg_img = Image.new("RGBA", (180, 180), (255, 255, 255, 255))
bg_img.paste(img180, (0, 0), img180)
bg_img.convert("RGB").save(os.path.join(out_dir, "apple-touch-icon.png"))
print("saved apple-touch-icon.png")
