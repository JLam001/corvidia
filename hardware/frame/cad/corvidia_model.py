"""Corvidia parametric B-rep model. FreeCAD 1.1; dimensions mm, angles degrees.
FeaturePython objects retain shapes in FCStd; load through Open_Corvidia.FCMacro
so this module is importable when editing the parameter spreadsheet.
"""
import math, json
from pathlib import Path
import FreeCAD as App
import Part
V=App.Vector
ROOT=Path(__file__).resolve().parents[1]
PARAMS=json.loads((ROOT/'cad/parameters.json').read_text())

def box(x,y,z,dx,dy,dz): return Part.makeBox(dx,dy,dz,V(x,y,z))
def cyl(x,y,z,r,h,axis=None): return Part.makeCylinder(r,h,V(x,y,z),axis or V(0,0,1))
def fuse(parts):
    s=parts[0]
    for q in parts[1:]: s=s.fuse(q)
    return s.removeSplitter()
def cut(s,tools):
    for q in tools: s=s.cut(q)
    return s.removeSplitter()
def hole(x,y,z,d,h): return cyl(x,y,z,d/2,h)
def hexhole(x,y,z,af,h):
    r=af/math.sqrt(3)
    pts=[V(x+r*math.cos(i*math.pi/3),y+r*math.sin(i*math.pi/3),z) for i in range(6)]
    return Part.Face(Part.makePolygon(pts+[pts[0]])).extrude(V(0,0,h))
def link(a,b,w,z,h):
    a,b=V(a[0],a[1],z),V(b[0],b[1],z)
    d=b-a; n=V(-d.y,d.x,0); n.normalize(); n.multiply(w/2)
    poly=Part.makePolygon([a+n,b+n,b-n,a-n,a+n])
    return fuse([Part.Face(poly).extrude(V(0,0,h)),cyl(a.x,a.y,z,w/2,h),cyl(b.x,b.y,z,w/2,h)])
def rectangle_wire(x,y,z):
    p=[V(-x/2,-y/2,z),V(x/2,-y/2,z),V(x/2,y/2,z),V(-x/2,y/2,z)]
    return Part.makePolygon(p+[p[0]])
def pattern(px,py,cx=0,cy=0): return [(cx+i*px/2,cy+j*py/2) for i in (-1,1) for j in (-1,1)]
def boards(p):
    return [('ESC',p.EscPitch,p.EscPitch,0,0,p.EscX,p.EscY,p.EscZ),
            ('Feather',p.FeatherPitchX,p.FeatherPitchY,0,p.FeatherCentreY,p.FeatherEnvelopeX,p.FeatherEnvelopeY,p.FeatherEnvelopeZ),
            ('IMU',p.ImuPitchX,p.ImuPitchY,0,p.ImuCentreY,p.ImuX,p.ImuY,p.ImuHeight)]
def jetson_holes(p): return pattern(p.JetsonPitchX,p.JetsonPitchY,p.JetsonHoleOffsetX,p.JetsonHoleOffsetY)
def zcarrier(p): return p.ShelfZ+p.DeckThickness+p.JetsonGap
def tilt(s,p,angle=None):
    s=s.copy(); s.rotate(V(p.CameraPivotX,0,p.CameraPivotZ),V(0,1,0),-(p.CameraTilt if angle is None else angle)); return s

def chassis(p):
    t=p.DeckThickness
    s=box(-p.DeckX/2,-p.DeckY/2,0,p.DeckX,p.DeckY,t)
    additions=[]; cuts=[]
    for x,y in pattern(104,86):
        additions.append(cyl(x,y,t,4.5,p.ShelfZ-t))
        cuts.extend([hole(x,y,-1,p.M3Clearance,p.ShelfZ+2),hexhole(x,y,0,p.M3NutAF,p.M3NutDepth)])
    for x,y in pattern(2*p.BenchX,2*p.BenchY):
        additions.append(cyl(x,y,-4,7,4)); cuts.append(hole(x,y,-5,p.M4Clearance,t+6))
    for sx in (-1,1):
        for sy in (-1,1):
            for xx in (35,47):
                cuts.extend([hole(sx*xx,sy*38,-1,p.M3Clearance,t+2),hexhole(sx*xx,sy*38,t-p.M3NutDepth,p.M3NutAF,p.M3NutDepth+1)])
    for name,px,py,cx,cy,dx,dy,dz in boards(p):
        for x,y in pattern(px,py,cx,cy):
            # ESC supplied grommets sit on shorter 3 mm seats; others 6 mm.
            height=(3 if name=='ESC' else p.BoardZ-t)
            additions.append(cyl(x,y,t,2.8,height))
            cuts.append(hole(x,y,-1,p.M2Clearance,p.BoardZ+2))
    # Two retained tie routes for external power, and USB / signal bundles.
    for y in (-8,8): cuts.append(box(-56,y-1.2,-1,10,2.4,t+2))
    # Ribbon rise and microSD access apertures; outside ESC footprint.
    cuts += [box(30,-8,-1,14,16,t+2), box(-12,25,-1,24,13,t+2)]
    return cut(fuse([s]+additions),cuts)

def curved_guard(p,mx,my):
    # Revolved hourglass section: minimum ID at mid-height, flared round lips.
    lo=p.PropBottom-p.GuardMargin; hi=p.PropTop+p.GuardMargin
    r=p.GuardID/2; t=p.GuardWall; f=p.GuardFlare; cap=t/2
    z0=lo+cap; z1=hi-cap; mid=(lo+hi)/2
    assert z1>z0 and p.PropBottom>=p.GuardMargin, 'Invalid prop/guard heights'
    pts=[(r+f,z0),(r,mid),(r+f,z1),(r+f+cap,hi),
         (r+f+t,z1),(r+t,mid),(r+f+t,z0),(r+f+cap,lo)]
    v=[V(rr,0,zz) for rr,zz in pts]
    edges=[Part.Arc(v[0],v[1],v[2]).toShape(),Part.Arc(v[2],v[3],v[4]).toShape(),
           Part.Arc(v[4],v[5],v[6]).toShape(),Part.Arc(v[6],v[7],v[0]).toShape()]
    band=Part.Face(Part.Wire(edges)).revolve(V(0,0,0),V(0,0,1),360)
    band.translate(V(mx,my,0))
    stays=[]
    # Curved radial ribs; flat tangential faces give predictable PETG toolpaths.
    half=p.GuardStayWidth/2
    for angle in (0,90,180,270):
        centre=[(53,-1),(51,lo*.25),(51,lo*.72),(r+f,lo+1.5)]
        curves=[]
        for sign in (-1,1):
            curve=Part.BezierCurve();curve.setPoles([V(rr+sign*1.2,-half,zz) for rr,zz in centre]);curves.append(curve)
        inner,outer=curves
        edges=[inner.toShape(),Part.makeLine(inner.EndPoint,outer.EndPoint),outer.toShape().reversed(),Part.makeLine(outer.StartPoint,inner.StartPoint)]
        stay=Part.Face(Part.Wire(edges)).extrude(V(0,p.GuardStayWidth,0))
        stay.rotate(V(0,0,0),V(0,0,1),angle);stay.translate(V(mx,my,0));stays.append(stay)
    return fuse([band]+stays)

def arm(p,sx,sy):
    mx,my=p.MotorX,p.MotorY; z=-p.ArmThickness; h=p.ArmThickness
    ring=curved_guard(p,mx,my)
    root=box(28,32,z,32,22,h)
    parts=[ring,root,link((44,43),(mx,my),20,z,h),cyl(mx,my,z,22,h)]
    for dx,dy in [(1,0),(-1,0),(0,1),(0,-1)]:
        parts.append(link((mx+dx*16,my+dy*16),(mx+dx*(p.GuardID/2+2.4-4.2),my+dy*(p.GuardID/2+2.4-4.2)),8,z,h))
    holes=[]
    for x in (35,47): holes.append(hole(x,38,z-1,p.M3Clearance,h+2))
    for x,y in [(mx+17,my),(mx-17,my),(mx,my+17),(mx,my-17)]:
        holes.extend([hole(x,y,z-1,p.M3Clearance,h+2),hexhole(x,y,z,p.M3NutAF,p.M3NutDepth)])
    for x,y in pattern(p.MotorPitch,p.MotorPitch,mx,my): holes.append(hole(x,y,z-1,6.6,h+2))
    holes.append(hole(mx,my,z-1,p.MotorRelief,h+2))
    holes.append(hole(52,43,z-1,6.6,h+2))  # shelf screw tip and nut insertion passage
    # Recessed wire channel ends short of the adapter; retain with two zip ties.
    holes.append(link((58,55),(mx-19,my-17),7,-3.2,3.3))
    for x,y in [(66,63),(79,75)]:
        for sign in (-1,1): holes.append(hole(x+sign*5,y-sign*5,z-1,3.0,h+2))
    s=cut(fuse(parts),holes)
    if sx==-1: s=s.mirror(V(0,0,0),V(1,0,0))
    if sy==-1: s=s.mirror(V(0,0,0),V(0,1,0))
    return s

def adapter(p):
    h=p.AdapterThickness
    s=cyl(0,0,0,21,h)
    cuts=[hole(0,0,-1,p.MotorRelief,h+2),box(11,-3,-1,12,6,h+2)]
    for x,y in pattern(p.MotorPitch,p.MotorPitch): cuts.append(hole(x,y,-1,p.M3Clearance,h+2))
    for x,y in [(17,0),(-17,0),(0,17),(0,-17)]: cuts.append(hole(x,y,-1,p.M3Clearance,h+2))
    # Wire exit is between mounting points, not through the adapter fixing hole.
    cuts[1]=box(10,10,-1,14,7,h+2)
    return cut(s,cuts)

def shelf(p):
    z=p.ShelfZ; t=p.DeckThickness; floor=z+t
    # Full supporting plate, with a cable aperture and the existing frame joints.
    add=[box(-58,-52,z,116,104,t)]; cuts=[]
    for x,y in jetson_holes(p):
        # Plain bearing pads at the proven clear PCB mounting lands: no screws.
        add.append(cyl(x,y,floor,3.3,p.JetsonGap))
    top=zcarrier(p)+p.JetsonPCBThickness
    edge=p.JetsonY/2+p.TrayClearance; wall=p.TrayRimThickness
    for sign in (-1,1):
        # Low inboard stems clear the canopy's lower sill. The rim sits in its window.
        yy=edge-1.4 if sign>0 else -edge
        add.append(box(-30,yy,floor,60,1.4,6.3))
        yy=edge+wall/2
        add.append(link((-29.3,sign*yy),(29.3,sign*yy),wall,floor+6,top-floor-6))
    front=p.JetsonX/2+p.TrayClearance
    for yy in (-24,24):
        add.append(link((front+wall/2,yy-3),(front+wall/2,yy+3),wall,floor,top-floor))
    # A short rear stop fits between the connector row and the existing canopy tab.
    add.append(link((-front-wall/2,-38.2),(-front-wall/2,-37.2),wall,floor,top-floor))
    for x,y in pattern(104,86): cuts.append(hole(x,y,z-1,p.M3Clearance,t+2))
    for x,y in [(sx*44,sy*47) for sx in (-1,1) for sy in (-1,1)]+[(52,-25),(52,25)]:
        cuts.extend([hole(x,y,z-1,p.M3Clearance,t+2),hexhole(x,y,z,p.M3NutAF,p.M3NutDepth)])
    cuts.append(box(41.5,-9,z-1,4,18,t+2))
    return cut(fuse(add),cuts)

def canopy(p):
    z=p.ShelfZ+p.DeckThickness; top=p.CanopyTop
    outer=Part.makeLoft([rectangle_wire(120,108,z),rectangle_wire(100,96,top)],True)
    inner=Part.makeLoft([rectangle_wire(115.2,103.2,z-0.1),rectangle_wire(95.2,91.2,top+0.1)],True)
    s=outer.cut(inner)
    # Large access windows leave pillars and a continuous upper frame.
    s=cut(s,[box(-70,-40,z+6,30,80,top-z-13),box(45,-36,z-0.1,30,72,top-z-7),
             box(-42,-65,z+6,84,25,top-z-13),box(-42,40,z+6,84,25,top-z-13)])
    tabs=[]; cuts=[]
    for x,y in [(sx*44,sy*47) for sx in (-1,1) for sy in (-1,1)]:
        tabs.append(box(x-4,y-7,z,8,14,3)); cuts.append(hole(x,y,z-1,p.M3Clearance,5))
    return cut(fuse([s]+tabs),cuts)

def camera_fixed(p):
    z=p.ShelfZ+p.DeckThickness; cx=p.CameraPivotX; cz=p.CameraPivotZ
    parts=[]; cuts=[]
    # Independent cheeks joined by rear bridge, open around lens and PCB.
    for sign in (-1,1):
        y=sign*27.8-2
        parts += [box(46,sign*25-7,z,cx+18-46,14,3.5),box(cx-7,y,z+3,25,4,cz+12-z-3)]
        cuts.append(cyl(cx,y-1,cz,p.M3Clearance/2,6,V(0,1,0)))
        # Continuous arcuate slot for 0..20 degrees at radius 12.
        pts=[(cx+12*math.cos(math.radians(a)),cz+12*math.sin(math.radians(a))) for a in range(0,int(p.CameraTiltMax)+1)]
        slot=[]
        for x,zz in pts: slot.append(cyl(x,y-1,zz,p.M3Clearance/2,6,V(0,1,0)))
        cuts.append(fuse(slot))
        cuts.append(hole(52,sign*25,z-1,p.M3Clearance,6))
    parts.append(box(46,-32,z,4,64,3.5))
    return cut(fuse(parts),cuts)

def camera_carrier(p):
    cx=p.CameraPivotX; cz=p.CameraPivotZ; gap=p.MateClearance
    s=box(cx-1,-22,cz-22,4,44,44).cut(box(cx-2,-15.5,cz-15.5,6,31,31))
    parts=[s]; cuts=[]
    for y,zz in pattern(p.CameraPitch,p.CameraPitch,0,cz): cuts.append(cyl(cx-2,y,zz,p.M2Clearance/2,6,V(1,0,0)))
    for sign in (-1,1):
        # outside of each ear stops 0.3 mm before the fixed cheek.
        y=21.5 if sign>0 else -25.8+gap
        parts.append(box(cx-2,y,cz-5,17,4.3-gap,10))
        for x in (cx,cx+12):
            nut=hexhole(0,0,0,p.M3NutAF,p.M3NutDepth)
            nut.rotate(V(0,0,0),V(1,0,0),-90 if sign>0 else 90)
            nut.translate(V(x,21.5 if sign>0 else -21.5,cz))
            cuts.append(nut)
            cuts.append(cyl(x,-32,cz,p.M3Clearance/2,64,V(0,1,0)))
    return tilt(cut(fuse(parts),cuts),p)

def camera_ref(p):
    cx=p.CameraPivotX+3; cz=p.CameraPivotZ
    pcb=box(cx,-p.CameraSize/2,cz-p.CameraSize/2,p.CameraPCBThickness,p.CameraSize,p.CameraSize)
    pcb=cut(pcb,[cyl(cx-1,y,zz,p.M2Clearance/2,p.CameraPCBThickness+2,V(1,0,0)) for y,zz in pattern(p.CameraPitch,p.CameraPitch,0,cz)])
    # Approximate full-depth keepout. The 31 mm lens alone is insufficient.
    lens=cyl(cx+p.CameraPCBThickness,0,cz,p.CameraLensDiameter/2,p.CameraDepth,V(1,0,0))
    return tilt(fuse([pcb,lens]),p)

def camera_cone(p,angle=None):
    cx=p.CameraPivotX+3+p.CameraPCBThickness+p.CameraDepth; cz=p.CameraPivotZ
    length=100
    s=Part.makeCone(0.05,math.tan(math.radians(p.CameraFOV/2))*length,length,V(cx,0,cz),V(1,0,0))
    return tilt(s,p,angle)

def board_ref(p,name):
    for n,px,py,cx,cy,dx,dy,dz in boards(p):
        if n==name:
            s=box(cx-dx/2,cy-dy/2,p.BoardZ,dx,dy,dz)
            return cut(s,[hole(x,y,p.BoardZ-1,p.M2Clearance,dz+2) for x,y in pattern(px,py,cx,cy)])

def coupon(p,name):
    if name=='Motor': return adapter(p)
    if name=='Camera':
        s=camera_carrier(p); s.rotate(V(p.CameraPivotX,0,p.CameraPivotZ),V(0,1,0),p.CameraTilt)
        s.rotate(V(0,0,0),V(0,1,0),-90); b=s.BoundBox; s.translate(V(-b.XMin,-b.YMin,-b.ZMin)); return s
    if name=='Jetson':
        # Reduced-material tray coupon reproduces every landing pad and edge stop.
        s=shelf(p)
        s=s.cut(box(-16,-37,p.ShelfZ-1,32,74,p.DeckThickness+2))
        s.translate(V(0,0,-p.ShelfZ));return s.removeSplitter()
    for n,px,py,cx,cy,dx,dy,dz in boards(p):
        if n==name:
            s=box(-dx/2-3,-dy/2-3,0,dx+6,dy+6,3)
            return cut(s,[hole(x,y,-1,p.M2Clearance,5) for x,y in pattern(px,py)])
    if name=='Clearance':
        s=box(0,0,0,65,24,5)
        for i,d in enumerate([2.2,2.4,2.7,2.9,3.2,3.4,4.3,4.5]): s=s.cut(hole(5+i*7.5,6,-1,d,7))
        for i,af in enumerate([5.6,5.8,6.0]): s=s.cut(hexhole(10+i*20,17,2.4,af,3.6)).cut(hole(10+i*20,17,-1,3.4,7))
        return s.removeSplitter()

def make_shape(kind,p):
    if kind=='Chassis': return chassis(p)
    if kind=='Shelf': return shelf(p)
    if kind=='Canopy': return canopy(p)
    if kind=='CameraBracket': return camera_fixed(p)
    if kind=='CameraCarrier': return camera_carrier(p)
    if kind=='CameraEnvelope': return camera_ref(p)
    if kind=='CameraView': return camera_cone(p)
    if kind.startswith('Coupon_'): return coupon(p,kind.split('_')[1])
    if kind in ('ESC','Feather','IMU'): return board_ref(p,kind)
    if kind=='FanKeepout':
        # Cooler native bounds transformed by Rz(-90) and centring.
        return box(0.01,-26.9,zcarrier(p)+p.JetsonCoolerTop,39,57.8,p.FanHeadroom)
    if kind=='PowerRoute': return box(-90,-5,8,72,10,8)
    if kind=='RibbonRoute': return fuse([box(42,-8,12,3,16,35),box(42,-8,38,26,16,4)])
    if kind=='RearConnectorAccess': return box(-90,-39,zcarrier(p),47,78,23)
    if kind.startswith(('Arm_','Adapter_','Prop_','Motor_','Wire_')):
        name,sxs,sys=kind.split('_'); sx=int(sxs);sy=int(sys);mx=sx*p.MotorX;my=sy*p.MotorY
        if name=='Arm': return arm(p,sx,sy)
        if name=='Adapter':
            s=adapter(p);s.translate(V(mx,my,0));return s
        if name=='Prop': return cyl(mx,my,p.PropBottom,p.PropDiameter/2,p.PropTop-p.PropBottom).cut(cyl(mx,my,p.PropBottom-1,p.PropHubDiameter/2,p.PropTop-p.PropBottom+2))
        if name=='Motor': return fuse([cyl(mx,my,p.AdapterThickness,p.MotorDiameter/2,p.MotorBodyHeight),cyl(mx,my,p.AdapterThickness+p.MotorBodyHeight,2.5,p.MotorLength-p.MotorBodyHeight)])
        if name=='Wire': return link((sx*59,sy*56),(sx*(p.MotorX-20),sy*(p.MotorY-18)),3,-2.9,2.8)
    raise ValueError(kind)

class FrameFeature:
    def __init__(self,obj,kind,sheet):
        obj.addProperty('App::PropertyString','Kind','Design'); obj.Kind=kind
        obj.addProperty('App::PropertyLink','Parameters','Design');obj.Parameters=sheet
        obj.addProperty('App::PropertyString','ReleaseStatus','Design');obj.ReleaseStatus='PROVISIONAL - physical fit and installed height checks required'
        obj.Proxy=self
    def execute(self,obj):
        class P: pass
        p=P()
        for k in PARAMS:
            q=getattr(obj.Parameters,k);setattr(p,k,float(q.Value if hasattr(q,'Value') else q))
        shape=make_shape(obj.Kind,p)
        # Explicitly restore top-level location after assigning the shape.
        placement=App.Placement(shape.Placement)
        obj.Shape=shape
        obj.Placement=placement
    def dumps(self): return None
    def loads(self,state): return None

_HW_CACHE={}
def hardware_shapes(p):
    key=tuple(vars(p).items())
    if key in _HW_CACHE:return _HW_CACHE[key]
    shapes={}
    def put(name,s):shapes[name.replace('-','N')]=s
    def screw(name,x,y,underhead,length,d,hh=3,hd=5.7,up=False):
        if up:s=fuse([cyl(x,y,underhead,hd/2,hh),cyl(x,y,underhead+hh,d/2,length)])
        else:s=fuse([cyl(x,y,underhead-length,d/2,length),cyl(x,y,underhead,hd/2,hh)])
        put(name,s)
    def nut(name,x,y,z,d=3,af=None,h=None):
        s=hexhole(x,y,z,(af or 5.5),(h or 2.4)).cut(hole(x,y,z-1,d,(h or 2.4)+2))
        put(name,s)
    for sx in (-1,1):
        for sy in (-1,1):
            mx,my=sx*p.MotorX,sy*p.MotorY
            for i,(x,y) in enumerate([(mx+17,my),(mx-17,my),(mx,my+17),(mx,my-17)]):
                screw(f'AdapterScrew_{sx}_{sy}_{i}',x,y,p.AdapterThickness,p.AdapterThickness+p.ArmThickness,3)
                nut(f'AdapterNut_{sx}_{sy}_{i}',x,y,-p.ArmThickness)
            for i,(x,y) in enumerate(pattern(p.MotorPitch,p.MotorPitch,mx,my)):
                screw(f'MotorScrew_{sx}_{sy}_{i}',x,y,-3,p.AdapterThickness+2,3,up=True)
            for i,x in enumerate((35,47)):
                screw(f'RootScrew_{sx}_{sy}_{i}',sx*x,sy*38,-p.ArmThickness-3,p.ArmThickness+p.DeckThickness,3,up=True)
                nut(f'RootNut_{sx}_{sy}_{i}',sx*x,sy*38,p.DeckThickness-p.M3NutDepth)
    for i,(x,y) in enumerate(pattern(104,86)):
        screw('ShelfScrew'+str(i),x,y,p.ShelfZ+p.DeckThickness,30,3);nut('ShelfNut'+str(i),x,y,0)
    for i,(x,y) in enumerate([(sx*44,sy*47) for sx in (-1,1) for sy in (-1,1)]):
        screw('CanopyScrew'+str(i),x,y,p.ShelfZ+p.DeckThickness+3,8,3);nut('CanopyNut'+str(i),x,y,p.ShelfZ)
    for i,y in enumerate((-25,25)):
        screw('CameraFootScrew'+str(i),52,y,p.ShelfZ+p.DeckThickness+3.5,8,3);nut('CameraFootNut'+str(i),52,y,p.ShelfZ)
    for name,px,py,cx,cy,dx,dy,dz in boards(p):
        for i,(x,y) in enumerate(pattern(px,py,cx,cy)):
            screw(name+'Screw'+str(i),x,y,p.BoardZ+1.6,14,2,2,3.8)
            nut(name+'Nut'+str(i),x,y,-1.6,2,4,1.6)
    for i,(y,z) in enumerate(pattern(p.CameraPitch,p.CameraPitch,0,p.CameraPivotZ)):
        x=p.CameraPivotX
        s=fuse([cyl(x-3,y,z,1,9,V(1,0,0)),cyl(x+6,y,z,1.9,2,V(1,0,0))])
        put('CameraPCBScrew'+str(i),tilt(s,p))
    for sign in (-1,1):
        for i,x in enumerate((p.CameraPivotX,p.CameraPivotX+12)):
            d=V(0,sign,0)
            s=fuse([cyl(x,sign*19.8,p.CameraPivotZ,1.5,10,d),cyl(x,sign*29.8,p.CameraPivotZ,2.85,3,d)])
            put(f'CameraClamp{sign}_{i}',tilt(s,p))
            n=hexhole(0,0,0,5.5,2.4).cut(hole(0,0,-1,3,5))
            n.rotate(V(0,0,0),V(1,0,0),-90 if sign>0 else 90);n.translate(V(x,sign*21.5,p.CameraPivotZ))
            put(f'CameraNut{sign}_{i}',tilt(n,p))
    _HW_CACHE.clear();_HW_CACHE[key]=shapes
    return shapes

class HardwareFeature(FrameFeature):
    def execute(self,obj):
        class P: pass
        p=P()
        for k in PARAMS:
            q=getattr(obj.Parameters,k);setattr(p,k,float(q.Value if hasattr(q,'Value') else q))
        s=hardware_shapes(p)[obj.Kind].copy();placement=App.Placement(s.Placement);obj.Shape=s;obj.Placement=placement
