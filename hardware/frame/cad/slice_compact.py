"""Slice the single-piece compact frame on one P2S plate."""
from pathlib import Path
import argparse,json,subprocess,sys,zipfile,struct,shutil
import xml.etree.ElementTree as ET
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R/'cad'))
def stamp_project(path,model_id,title):
 # CLI omits the model ID in slice metadata; use the vendor machine-model ID.
 with zipfile.ZipFile(path) as z:contents={n:z.read(n) for n in z.namelist()}
 for file,key,value in [('Metadata/slice_info.config','printer_model_id',model_id),('Metadata/model_settings.config','plater_name',title)]:
  root=ET.fromstring(contents[file])
  for plate in root.findall('plate'):
   for m in plate.findall('metadata'):
    if m.get('key')==key:m.set('value',value)
  contents[file]=ET.tostring(root,encoding='utf-8',xml_declaration=True)
 temp=path.with_suffix('.tmp')
 with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
  for name,data in contents.items():z.writestr(name,data)
 temp.replace(path)
parser=argparse.ArgumentParser()
parser.add_argument('--output-dir',type=Path,default=R/'output/Compact_P2S_PLA')
parser.add_argument('--app',type=Path,default=Path('/Applications/BambuStudio.app'))
args=parser.parse_args()
O=args.output_dir.resolve();O.mkdir(parents=True,exist_ok=True)
W=R/'tmp/compact_slice';W.mkdir(parents=True,exist_ok=True)
FINAL_NAME='Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf'
settings=json.loads((R/'cad/profiles/process.json').read_text())
settings.update(name='Corvidia Compact PLA 0.24 @P2S',layer_height='0.24',wall_loops='4',top_shell_layers='4',bottom_shell_layers='4',sparse_infill_density='100%',sparse_infill_pattern='zig-zag',brim_width='3',support_top_z_distance='0.24',support_bottom_z_distance='0.24',support_interface_top_layers='2')
# Solid fill retains solid material at the integrated motor pads.
(W/'process.json').write_text(json.dumps(settings,indent=2)+'\n')
# Explicitly centre the part; automatic arrangement reserves extra support margins
# and rejects this large sparse outline although its actual toolpaths fit.
data=(R/'output/Compact_P2S_PLA/Corvidia_Compact.stl').read_bytes();count=struct.unpack_from('<I',data,80)[0]
ns='http://schemas.microsoft.com/3dmanufacturing/core/2015/02';ET.register_namespace('',ns)
def element(tag,**kwargs):return ET.Element('{'+ns+'}'+tag,kwargs)
model=element('model',unit='millimeter');resources=element('resources');model.append(resources)
obj=element('object',id='1',type='model',name='Corvidia Compact');resources.append(obj)
mesh=element('mesh');obj.append(mesh);vertices=element('vertices');triangles=element('triangles');mesh.extend([vertices,triangles])
vertex_ids={}
for i in range(count):
 values=struct.unpack_from('<12fH',data,84+i*50)
 indices=[]
 for j in range(3):
  xyz=tuple(values[3+3*j:6+3*j])
  if xyz not in vertex_ids:
   vertex_ids[xyz]=len(vertex_ids)
   x,y,z=xyz;vertices.append(element('vertex',x=str(x),y=str(y),z=str(z)))
  indices.append(vertex_ids[xyz])
 triangles.append(element('triangle',v1=str(indices[0]),v2=str(indices[1]),v3=str(indices[2])))
build=element('build');model.append(build);build.append(element('item',objectid='1',transform='1 0 0 0 1 0 0 0 1 128 128 0'))
with zipfile.ZipFile(W/'Compact_layout.3mf','w',zipfile.ZIP_DEFLATED) as z:
 z.writestr('3D/3dmodel.model',ET.tostring(model,encoding='utf-8',xml_declaration=True))
 z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
 z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
exe=str(args.app/'Contents/MacOS/BambuStudio')
cmd=[exe,'--datadir',str(R/'tmp/bambu/userdata'),'--debug','2','--load-settings',str(R/'cad/profiles/machine.json')+';'+str(W/'process.json'),'--load-filaments',str(R/'cad/profiles/filament.json'),'--curr-bed-type','Textured PEI Plate','--orient','0','--arrange','0','--ensure-on-bed','--slice','0','--export-3mf',FINAL_NAME,'--outputdir',str(W),str(W/'Compact_layout.3mf')]
with (W/'slicer.log').open('w') as f:r=subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT)
assert r.returncode==0,r.returncode
shutil.copy2(W/FINAL_NAME,O/FINAL_NAME)
shutil.copy2(W/'result.json',O/'result.json')
result=json.loads((O/'result.json').read_text());assert result['return_code']==0 and len(result['sliced_plates'])==1
assert result['sliced_plates'][0]['warning_message']==''
stamp_project(O/FINAL_NAME,'N7','Corvidia Compact — one piece / one plate')
# Populate the instance association omitted by CLI when importing plain 3MF.
project=O/FINAL_NAME
with zipfile.ZipFile(project) as z:contents={n:z.read(n) for n in z.namelist()}
config=ET.fromstring(contents['Metadata/model_settings.config']);plate=config.find('plate');obj=config.find('object')
identity=json.loads(contents['Metadata/plate_1.json'])['bbox_objects'][0]['id']
instance=ET.SubElement(plate,'model_instance')
for key,value in [('object_id',obj.get('id')),('instance_id','0'),('identify_id',str(identity))]:ET.SubElement(instance,'metadata',key=key,value=value)
contents['Metadata/model_settings.config']=ET.tostring(config,encoding='utf-8',xml_declaration=True)
sliceinfo=ET.fromstring(contents['Metadata/slice_info.config']);ET.SubElement(sliceinfo.find('plate'),'object',identify_id=str(identity),name='Corvidia Compact',skipped='false')
contents['Metadata/slice_info.config']=ET.tostring(sliceinfo,encoding='utf-8',xml_declaration=True)
with zipfile.ZipFile(project,'w',zipfile.ZIP_DEFLATED) as z:
 for name,data in contents.items():z.writestr(name,data)
print(json.dumps(result,indent=2))
