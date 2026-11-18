import sys,json
sys.path.insert(0,'tools')
import pcbnew,route_completion as r,numpy as np
from PIL import Image,ImageDraw
from shapely.geometry import Point
r.W_TRK=.15;r.CLR=.15;r.EPS=.01
b=pcbnew.LoadBoard('validation/high-speed/rgmii-06/anchors-MG_TXD0.kicad_pcb');g=r.Grid(b);g.load(b);net='MG_TXD0';own=np.zeros_like(g.T['C'])
for layers,shape,hole,item in g.items[net]:
 if 'C' in layers:g.stamp(own,shape,r.CLR+r.W_TRK/2+r.EPS+(r.HOLE_EXTRA if hole else 0))
free=(g.T['C']-own)==0
x0,y0=g.cell(48,34);x1,y1=g.cell(72,56);rgb=np.where(free[y0:y1,x0:x1,None],np.array([255,255,255],dtype=np.uint8),np.array([220,65,65],dtype=np.uint8));im=Image.fromarray(rgb).resize(((x1-x0)*2,(y1-y0)*2));d=ImageDraw.Draw(im)
a=json.load(open('validation/high-speed/rgmii-06/anchors-MG_TXD0.json'))
for i,p in enumerate(a):
 x,y=g.cell(*p);x=(x-x0)*2;y=(y-y0)*2;d.ellipse((x-6,y-6,x+6,y+6),outline='blue',width=3);d.text((x+8,y),str(i),fill='blue')
im.save('runs/rgmii-grid.png')
for v in g.V.values():v.fill(60000)
for layer in ['F','B']:g.T[layer].fill(60000)
result=g.route(net,a[0],'C',a[1],'C');print('fresh result',result)
