import sys
sys.path.insert(0,'tools')
import pcbnew,route_completion as r
from PIL import Image,ImageDraw
from shapely.geometry import box
b=pcbnew.LoadBoard('runs/rgmii-power-filtered-17.kicad_pcb');g=r.Grid(b);g.load(b);im=Image.new('RGB',(1000,800),'white');d=ImageDraw.Draw(im);region=box(62.5,36,68.5,43)
def pixel(x,y):return((x-62.5)*140+40,(y-36)*100+40)
colors={'GND':'#aaaaaa','+1V2_PHY':'#b050b0','MG_TXD0':'red','MG_TXD1':'#0066ff','MG_TXD2':'#008855','MG_TXD3':'#aa7700','MG_INT#':'#000000'}
for net,items in g.items.items():
 for layers,shape,_,_ in items:
  if 'F' not in layers or not shape.intersects(region):continue
  q=shape.intersection(region)
  for p in (list(q.geoms) if hasattr(q,'geoms') else [q]):
   if p.geom_type=='Polygon':d.polygon([pixel(x,y) for x,y in p.exterior.coords],fill=colors.get(net,'#dd7777'))
for f in b.GetFootprints():
 for p in f.Pads():
  x,y=p.GetPosition().x/1e6,p.GetPosition().y/1e6
  if p.IsOnLayer(pcbnew.F_Cu) and 62.5<x<68.5 and 36<y<43:d.text(pixel(x,y),f.GetReference()+'.'+p.GetNumber(),fill='black')
for n,(net,col) in enumerate(colors.items()):d.text((700,10+14*n),net,fill=col)
for y in range(36,44):d.text(pixel(62.5,y),str(y),fill='black')
for x in range(63,69):d.text(pixel(x,36),str(x),fill='black')
im.save('runs/phy-fanout.png')
