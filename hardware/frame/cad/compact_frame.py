"""Corvidia Compact: one printed part, open motors, fixed forward camera."""
import sys,json,math
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R/'cad'))
import FreeCAD as App,Part
import corvidia_model as M
from types import SimpleNamespace
V=App.Vector
DEFAULTS={k:v[0] for k,v in M.PARAMS.items()}
DEFAULTS.update(ShelfZ=0,DeckThickness=6,CameraPivotX=55,CameraPivotZ=26,CameraTilt=0,TrayRimThickness=2.4)
EXTRA={'MotorPadRadius':16,'ArmWidth':20,'ArmRootWidth':28,'TabThickness':4,'CameraPlateThickness':3,'CameraFootDepth':14,'CameraBraceDepth':24,'CameraBraceThickness':3,'FlatDeckX':116,'FlatDeckY':116,'EscCentreX':0,'EscCentreY':-75,'FeatherCentreY':73,'ImuCentreX':-45,'ImuCentreY':-73}
DEFAULTS.update(EXTRA)
def params(sheet=None):
 return SimpleNamespace(**{k:(float(getattr(sheet,k)) if sheet else v) for k,v in DEFAULTS.items()})
def board_locations(p):return [('ESC',p.EscCentreX,p.EscCentreY,p.EscX,p.EscY,p.EscPitch,p.EscPitch,3,p.EscZ),('Feather',0,p.FeatherCentreY,p.FeatherEnvelopeX,p.FeatherEnvelopeY,p.FeatherPitchX,p.FeatherPitchY,6,p.FeatherEnvelopeZ),('IMU',p.ImuCentreX,p.ImuCentreY,p.ImuX,p.ImuY,p.ImuPitchX,p.ImuPitchY,6,p.ImuHeight)]
def camera_plate(p):
 # Two outside triangular cheeks brace the ring without covering its window,
 # PCB, screw axes or lens-adjustment area.
 x=p.CameraPivotX;zc=p.CameraPivotZ;t=p.CameraPlateThickness
 ring=M.box(x,-22,zc-22,t,44,44)
 cuts=[M.box(x-2,-15.5,zc-15.5,t+2,31,31)]
 for y,z in M.pattern(p.CameraPitch,p.CameraPitch,0,zc):cuts.append(M.cyl(x-1,y,z,p.M2Clearance/2,t+2,V(1,0,0)))
 ring=M.cut(ring,cuts)
 foot=M.box(x-7,-23,0,p.CameraFootDepth,46,zc-22+1)
 braces=[]
 for y in (-23,23-p.CameraBraceThickness):
  pts=[V(x,y,0),V(x+p.CameraBraceDepth,y,0),V(x+p.CameraBraceDepth,y,4),V(x,y,zc+18)]
  braces.append(Part.Face(Part.makePolygon(pts+[pts[0]])).extrude(V(0,p.CameraBraceThickness,0)))
 return M.fuse([ring,foot]+braces)
def jetson_tray(p):
 # Unbroken bonding surface for the user's own tray. Top is level with arm
 # roots; no board pads, rims, bench bosses, holes or ribbon slot in this deck.
 return M.box(-p.FlatDeckX/2,-p.FlatDeckY/2,0,p.FlatDeckX,p.FlatDeckY,p.DeckThickness)
def tapered_arm(a,b,p):
 # Tangent-sided hull of unequal end circles: a broad rounded root blends
 # continuously into the arm. Keep the 6 mm mounting/deck height unchanged.
 a=V(*a,0);b=V(*b,0);d=b-a;length=d.Length;d.normalize()
 r0=p.ArmRootWidth/2;r1=p.ArmWidth/2
 k=(r0-r1)/length
 if abs(k)>=1:raise ValueError('Arm taper radii exceed centre distance')
 normal=V(-d.y,d.x,0);f=math.sqrt(1-k*k)
 n0=d*k+normal*f;n1=d*k-normal*f
 pts=[a+n0*r0,b+n0*r1,b+n1*r1,a+n1*r0]
 prism=Part.Face(Part.makePolygon(pts+[pts[0]])).extrude(V(0,0,p.AdapterThickness))
 return M.fuse([prism,M.cyl(a.x,a.y,0,r0,p.AdapterThickness),M.cyl(b.x,b.y,0,r1,p.AdapterThickness)])
def shape(p):
 tray=jetson_tray(p)
 pieces=[tray]
 for sx in (-1,1):
  for sy in (-1,1):
   x,y=sx*p.MotorX,sy*p.MotorY
   arm=M.fuse([tapered_arm((sx*50,sy*43),(x,y),p),M.cyl(x,y,0,p.MotorPadRadius,p.AdapterThickness)])
   cuts=[M.hole(x,y,-1,p.MotorRelief,p.AdapterThickness+2)]
   cuts += [M.hole(xx,yy,-1,p.M3Clearance,p.AdapterThickness+2) for xx,yy in M.pattern(p.MotorPitch,p.MotorPitch,x,y)]
   # Paired through tie slots, beyond the tray and clear of the motor screw pattern.
   for sign in (-1,1):cuts.append(M.hole(sx*77+sign*3,sy*70-sign*3,-1,2.4,p.AdapterThickness+2))
   pieces.append(M.cut(arm,cuts))
 for name,cx,cy,dx,dy,px,py,h,dz in board_locations(p):
  tab=M.fuse([M.box(cx-dx/2-3,cy-dy/2-3,0,dx+6,dy+6,p.TabThickness),M.link((0,45 if cy>0 else -45),(cx,cy),20,0,p.TabThickness)])
  posts=[M.cyl(x,y,p.TabThickness,2.8,h) for x,y in M.pattern(px,py,cx,cy)]
  cuts=[M.hole(x,y,-1,p.M2Clearance,p.TabThickness+h+2) for x,y in M.pattern(px,py,cx,cy)]
  pieces.append(M.cut(M.fuse([tab]+posts),cuts))
 pieces.append(camera_plate(p))
 s=M.fuse(pieces)
 s=M.cut(s,[M.hole(x,y,-1,p.M2Clearance,14) for _,cx,cy,_,_,px,py,_,_ in board_locations(p) for x,y in M.pattern(px,py,cx,cy)])
 return s
class CompactFeature:
 def __init__(self,obj,sheet):
  if 'Parameters' not in obj.PropertiesList:obj.addProperty('App::PropertyLink','Parameters')
  obj.Parameters=sheet;obj.Proxy=self
 def execute(self,obj):obj.Shape=shape(params(obj.Parameters))
 def dumps(self):return None
 def loads(self,state):return None

def build():
 import MeshPart
 out=R/'output/Compact_P2S_PLA';out.mkdir(exist_ok=True)
 doc=App.newDocument('Corvidia_Compact');sheet=doc.addObject('Spreadsheet::Sheet','Parameters')
 for col,label in [('A1','Parameter'),('B1','Value'),('C1','Source / meaning')]:sheet.set(col,label)
 for row,(k,v) in enumerate(DEFAULTS.items(),2):
  sheet.set(f'A{row}',k);sheet.set(f'B{row}',str(v));sheet.setAlias(f'B{row}',k);sheet.set(f'C{row}',M.PARAMS[k][1]+'; '+M.PARAMS[k][2] if k in M.PARAMS else 'Compact design allowance')
 for key,desc in {'ShelfZ':'Flat deck underside; design allowance','DeckThickness':'6 mm flat deck, level with arm roots; design allowance','JetsonGap':'REFERENCE ONLY: assumed user tray lift above deck, actual tray unmeasured','CameraPivotX':'Fixed camera X datum; design allowance','CameraPivotZ':'Fixed camera optical centre height; design allowance','FeatherCentreY':'Compact side tab centre; design allowance','ImuCentreY':'Compact side tab centre; design allowance'}.items():
  row=list(DEFAULTS).index(key)+2;sheet.set(f'C{row}',desc)
 part=doc.addObject('Part::FeaturePython','CompactFrame');CompactFeature(part,sheet);doc.recompute();p=params()
 refs=[];group=doc.addObject('App::DocumentObjectGroup','ComponentReferences')
 def ref(n,s):
  o=doc.addObject('Part::Feature',n);o.Shape=s;group.addObject(o);refs.append(o);return o
 orin=Part.Shape();orin.read(str(R/'references/orin_without_base.brep'));orin.translate(V(-46,-22.5,0));orin.rotate(V(),V(0,0,1),-90);orin.translate(V(0,0,M.zcarrier(p)));ref('OrinNano',orin)
 for name,cx,cy,dx,dy,px,py,h,dz in board_locations(p):
  # ESC sits on supplied grommets at the original nominal Z=10; other boards on their pads.
  z=10
  s=M.box(cx-dx/2,cy-dy/2,z,dx,dy,dz)
  s=M.cut(s,[M.hole(x,y,z-1,p.M2Clearance,dz+2) for x,y in M.pattern(px,py,cx,cy)])
  ref(name,s)
 ref('Camera',M.camera_ref(p))
 sweeps=[]
 for sx in (-1,1):
  for sy in (-1,1):
   ref(f'Motor{sx}{sy}'.replace('-','N'),M.make_shape(f'Motor_{sx}_{sy}',p));sweeps.append(M.make_shape(f'Prop_{sx}_{sy}',p))
 s=part.Shape;mesh=MeshPart.meshFromShape(Shape=s,LinearDeflection=.08,AngularDeflection=.15,Relative=False)
 mesh.write(str(out/'Corvidia_Compact.stl'))
 def vol(a,b):return a.common(b).Volume if a.BoundBox.intersect(b.BoundBox) else 0
 report=dict(valid_solid=s.isValid(),solid_count=len(s.Solids),watertight=mesh.isSolid(),mesh_components=mesh.countComponents(),self_intersections=mesh.hasSelfIntersections(),bbox_mm=[mesh.BoundBox.XLength,mesh.BoundBox.YLength,mesh.BoundBox.ZLength],min_z=mesh.BoundBox.ZMin,component_intersections={o.Name:vol(s,o.Shape) for o in refs},prop_intersections=[vol(s,q) for q in sweeps],component_prop_intersections={o.Name:sum(vol(o.Shape,q) for q in sweeps) for o in refs},rear_connector_intersections={o.Name:vol(o.Shape,M.make_shape('RearConnectorAccess',p)) for o in refs if o.Name not in ('OrinNano',)},frame_rear_connector_intersection=vol(s,M.make_shape('RearConnectorAccess',p)),fan_clearance_intersection=vol(s,M.make_shape('FanKeepout',p)),camera_cone_intersection=vol(s,M.camera_cone(p)),bench_hole_centres=[],flat_bonding_area_mm=[110,116],user_tray_footprint_mm=[95,111],flat_top_z_mm=p.DeckThickness,flat_surface_obstruction_mm3=vol(s,M.box(-55,-58,p.DeckThickness,110,116,10)),flat_area_missing_material_mm3=M.box(-55,-58,0,110,116,p.DeckThickness).cut(s).Volume,user_tray_verified=False,jetson_reference_note='Nominal placement only; user tray footprint 95 x 111 mm; support height and complete envelope unmeasured')
 report['passes']=report['flat_surface_obstruction_mm3']<.001 and report['flat_area_missing_material_mm3']<.001 and s.isValid() and len(s.Solids)==1 and mesh.isSolid() and mesh.countComponents()==1 and not mesh.hasSelfIntersections() and max(report['bbox_mm'][:2])<=246 and all(v<.001 for v in report['component_intersections'].values()) and all(v<.001 for v in report['prop_intersections']) and all(v<.001 for v in report['component_prop_intersections'].values()) and report['fan_clearance_intersection']<.001 and report['camera_cone_intersection']<.001 and report['frame_rear_connector_intersection']<.001 and all(v<.001 for v in report['rear_connector_intersections'].values())
 (out/'geometry_checks.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2),flush=True)
 Part.export([part],str(out/'Corvidia_Compact.step'));doc.recompute();doc.saveAs(str(out/'Corvidia_Compact.FCStd'))
 return doc
if __name__=='__main__':
 # Serialize a stable importable module name, rather than '__main__'.
 import compact_frame
 compact_frame.build()
