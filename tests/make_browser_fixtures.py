"""Create synthetic, disposable files for manual UI verification only."""
from pathlib import Path
from PIL import Image, ImageDraw


def main():
    folder = Path(__file__).resolve().parents[1] / "test-results" / "fixtures"
    folder.mkdir(parents=True, exist_ok=True)
    for name, color, shape in [("forest.png", "#d6e2ce", "forest"),
                               ("study.png", "#efe0cc", "study"),
                               ("shapes.webp", "#dae4e8", "shapes")]:
        image = Image.new("RGB", (640, 480), color)
        draw = ImageDraw.Draw(image)
        if shape == "forest":
            draw.ellipse((400, 40, 490, 130), fill="#f9f6de")
            for x, y in [(50, 130), (170, 80), (300, 170), (420, 150)]:
                draw.rectangle((x + 65, y + 140, x + 80, 410), fill="#6f7e53")
                draw.polygon([(x, y + 160), (x + 150, y + 160), (x + 75, y)], fill="#587b5d")
        elif shape == "study":
            draw.rounded_rectangle((100, 130, 540, 350), 15, fill="#f9f5eb")
            draw.line((320, 140, 320, 340), fill="#b9a47f", width=3)
            for y in range(170, 310, 30):
                draw.line((135, y, 290, y), fill="#d1c2a5", width=3)
                draw.line((350, y, 505, y), fill="#d1c2a5", width=3)
        else:
            draw.ellipse((85, 135, 305, 355), fill="#e3b18b")
            draw.rounded_rectangle((310, 100, 535, 330), 12, fill="#688c87")
        image.save(folder / name)
    (folder / "broken.png").write_text("not an image", encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    main()
