"""Synthetic fixtures for browser checks, isolated from the user's data directory."""
import io
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from test_features import pdf
from archive import Library

root = Path(__file__).resolve().parents[1] / 'test-results' / 'features-fixtures'
root.mkdir(parents=True, exist_ok=True)
library = Library(root.parent / 'features-browser-data')
for index in range(10):
    image = Image.new('RGB', (320, 240), (80 + index * 4, 125, 60))
    ImageDraw.Draw(image).rectangle((20 + index, 40, 130, 210), fill=(30, 60, 25))
    output = io.BytesIO(); image.save(output, 'PNG')
    library.add_image(output.getvalue(), {'filename': f'forest-{index}.png', 'title': f'숲 사례 {index + 1}', 'tags': ['숲'], 'categories': ['환경'], 'note': '초록 배경의 합성 테스트 이미지입니다.'})
image.resize((640, 480)).save(root / 'forest-new.jpg')
blue = Image.new('RGB', (320, 240), (50, 80, 190)); blue.save(root / 'blue.png')
text = Image.new('RGB', (1100, 220), 'white')
ImageDraw.Draw(text).text((30, 50), 'ARCHIVE SEARCH TEST', font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 60), fill='black')
text.save(root / 'ocr.png')
(root / 'reference.pdf').write_bytes(pdf())
print(root)
