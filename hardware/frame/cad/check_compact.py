"""Verify the compact project and render its actual extrusion paths."""
from pathlib import Path
import json,zipfile,re,math
import xml.etree.ElementTree as ET
from PIL import Image,ImageDraw
R=Path(__file__).resolve().parents[1];O=R/'output/Compact_P2S_PLA'
FEATURES={'Outer wall','Inner wall','Sparse infill','Internal solid infill','Top surface','Bottom surface','Bridge','Overhang wall','Gap infill','Support','Support interface','Brim'}
def paths(code):
 x=y=0.;z=0.;feature='Custom';segments=[];layer=0
 for line in code.splitlines():
  if line.startswith('; FEATURE: '):feature=line[11:].strip();continue
  if line.startswith('; CHANGE_LAYER'):layer+=1
  if line.startswith('; Z_HEIGHT: '):z=float(line[12:]);continue
  if not re.match(r'^G[0123] ',line):continue
  w={k:float(v) for k,v in re.findall(r'([XYEIJ])(-?\d*\.?\d+)',line.split(';')[0])}
  xx=w.get('X',x);yy=w.get('Y',y)
  if layer and feature in FEATURES and w.get('E',0)>0 and (xx!=x or yy!=y):
   pts=[(x,y),(xx,yy)]
   if line.startswith(('G2 ','G3 ')) and ('I'in w or 'J'in w):
    cx=x+w.get('I',0);cy=y+w.get('J',0);radius=math.hypot(x-cx,y-cy);a=math.atan2(y-cy,x-cx);b=math.atan2(yy-cy,xx-cx)
    delta=(b-a)%(2*math.pi)
    if line.startswith('G2 '):delta=-((a-b)%(2*math.pi))
    pts=[(cx+radius*math.cos(a+delta*i/32),cy+radius*math.sin(a+delta*i/32)) for i in range(33)]
   segments.append((z,feature,pts))
  x,y=xx,yy
 return segments

def draw_tile(title,segs,z_target=None):
 im=Image.new('RGB',(600,650),'white');d=ImageDraw.Draw(im)
 d.text((18,8),title,fill='black');d.text((18,26),'Orange: part / Blue: support / Grey: brim',fill='black')
 def xy(p):return (35+p[0]*2,610-p[1]*2)
 d.rectangle((35,98,547,610),outline='#90A0B0',width=2)
 for z,f,pts in segs:
  if z_target is not None and abs(z-z_target)>.025:continue
  color='#347CAB' if f.startswith('Support') else '#A0A0A0' if f=='Brim' else '#E77721'
  d.line([xy(p) for p in pts],fill=color,width=1)
 return im

def check_motor_holes(segs):
 # Check the actual extrusion, not only the source mesh: every low layer must
 # have a bore perimeter, with no printing through the bore's open core.
 holes=[]
 for sx in (-1,1):
  for sy in (-1,1):
   cx=128+sx*104;cy=128+sy*96
   holes.append((f'{sx},{sy} relief',cx,cy,4.5))
   for dx in (-5.75,5.75):
    for dy in (-5.75,5.75):holes.append((f'{sx},{sy} screw {dx},{dy}',cx+dx,cy+dy,1.7))
 low=[q for q in segs if .1<q[0]<=6.001]
 layers={round(q[0],3) for q in low}
 results=[]
 for name,cx,cy,r in holes:
  closest=1e9;perimeter_layers=set()
  for z,f,pts in low:
   xs=[a[0] for a in pts];ys=[a[1] for a in pts]
   if max(xs)<cx-r-2 or min(xs)>cx+r+2 or max(ys)<cy-r-2 or min(ys)>cy+r+2:continue
   for a,b in zip(pts,pts[1:]):
    dx=b[0]-a[0];dy=b[1]-a[1];ll=dx*dx+dy*dy
    t=max(0,min(1,((cx-a[0])*dx+(cy-a[1])*dy)/ll)) if ll else 0
    distance=math.hypot(a[0]+t*dx-cx,a[1]+t*dy-cy)
    closest=min(closest,distance)
    if f=='Outer wall' and r-.1<distance<r+.8:perimeter_layers.add(round(z,3))
  assert closest>r-.25,(name,'filled bore',closest)
  assert perimeter_layers==layers,(name,'missing bore perimeter',layers-perimeter_layers)
  results.append(dict(hole=name,diameter_mm=2*r,min_extrusion_centreline_radius_mm=round(closest,3),open_layers=len(perimeter_layers)))
 return results

with zipfile.ZipFile(O/'Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf') as z:
 assert z.testzip() is None
 settings=json.loads(z.read('Metadata/project_settings.config'))
 assert settings['printer_model']=='Bambu Lab P2S'
 assert settings['printer_settings_id']=='Bambu Lab P2S 0.4 nozzle'
 assert settings['curr_bed_type']=='Textured PEI Plate'
 assert settings['filament_type'][0]=='PLA'
 assert settings['sparse_infill_density']=='100%'
 assert abs(float(settings['layer_height'])-.24)<.001
 config=ET.fromstring(z.read('Metadata/model_settings.config'))
 assert len(config.findall('object'))==1 and len(config.findall('plate'))==1
 assert len(config.findall('plate/model_instance'))==1
 segs=paths(z.read('Metadata/plate_1.gcode').decode())
 motor_holes=check_motor_holes(segs)
 pts=[pt for _,_,poly in segs for pt in poly]
 bounds=[min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)]
 assert min(bounds[:2])>.4 and max(bounds[2:])<255.6,bounds
 assert all(0<seg[0]<=256 for seg in segs)
 draw_tile('Compact frame: first layer / 256 x 256 mm bed',segs,.2).save(O/'first_layer.png')
 images=[draw_tile(f'Compact frame: Z={h} mm',segs,h) for h in [.2,6.2,12.68,45.8]]
 overview=Image.new('RGB',(1200,1300),'white')
 for k,im in enumerate(images):overview.paste(im,((k%2)*600,(k//2)*650))
 overview.save(O/'toolpaths.png')
 (O/'preview.png').write_bytes(z.read('Metadata/plate_1.png'))
geom=json.loads((O/'geometry_checks.json').read_text());assert geom['passes']
result=json.loads((O/'result.json').read_text());assert result['return_code']==0 and len(result['sliced_plates'])==1
plate=result['sliced_plates'][0];assert not plate['warning_message']
report=dict(passes=True,objects=1,plates=1,model_mm=geom['bbox_mm'],extrusion_bounds_xy_mm=bounds,extrusion_footprint_mm=[bounds[2]-bounds[0],bounds[3]-bounds[1]],bed_mm=[256,256],estimated_minutes=plate['total_predication']/60,estimated_grams=sum(f['total_used_g'] for f in plate['filaments']),remaining_checks='Physical fit, motor screw length, wiring, strength, heat and fixture restraint remain unverified.')
report['motor_hole_toolpath_checks']=motor_holes
(O/'print_checks.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
