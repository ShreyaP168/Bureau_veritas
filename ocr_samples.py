import pymupdf,pytesseract
from PIL import Image
from pathlib import Path
pytesseract.pytesseract.tesseract_cmd=r'C:\Program Files\Tesseract-OCR\tesseract.exe'
for p in Path('samples').glob('*.pdf'):
 d=pymupdf.open(p); out=[]
 for i,page in enumerate(d):
  pix=page.get_pixmap(dpi=180)
  t=pytesseract.image_to_string(Image.frombytes('RGB',[pix.width,pix.height],pix.samples),config='--psm 3',timeout=90)
  out.append({'number':i+1,'text':t,'method':'tesseract'})
  print(p.name,i+1,flush=True)
 import json
 Path('outputs',p.stem+'.pages.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
