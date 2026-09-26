"""Run with FreeCAD Python after downloading the sources listed in SOURCES.md."""
from pathlib import Path
import FreeCAD, Part, json, hashlib
R=Path(__file__).resolve().parents[1]/'references'
s=Part.Shape();s.read(str(R/'P3766-P3768SKU4-P3767ENVELOPE.stp'))
base=s.Solids[1266]; b=base.BoundBox
assert abs(base.Volume-6446.43)<1 and abs(b.ZMin+5.03851)<0.01, 'Vendor CAD changed: inspect base selection'
pcb=s.Solids[274]
assert abs(pcb.BoundBox.XLength-100)<0.01 and abs(pcb.BoundBox.YLength-79)<0.01
Part.makeCompound([q for i,q in enumerate(s.Solids) if i not in set(range(1266,1271)) | {1377,1378} | set(range(1380,1393))]).exportBrep(str(R/'orin_without_base.brep'))
rows=[]
for i,so in enumerate(s.Solids):
 b=so.BoundBox
 rows.append({'i':i,'v':round(so.Volume,2),'bbox':[round(v,3) for v in [b.XMin,b.YMin,b.ZMin,b.XMax,b.YMax,b.ZMax]]})
(R/'orin_solids.json').write_text(json.dumps(rows,indent=2))
files=[p for p in R.iterdir() if p.suffix in ('.stp','.brd','.pdf','.brep')]
(R/'sha256.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},indent=2))
