"""Native-board invariants for ordinary-signal orphan copper cleanup."""
import importlib.util
import sys
import unittest
from pathlib import Path
for dependency in ("pcbnew","shapely","numpy","PIL"):
    if importlib.util.find_spec(dependency) is None:
        raise unittest.SkipTest("Dedicated hardware environment required: "+dependency)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import pcbnew
from audit_orphan_copper import audit

class OrphanCopperTests(unittest.TestCase):
    def board(self):
        b=pcbnew.BOARD();b.SetCopperLayerCount(6)
        return b
    def net(self,b,name):
        n=pcbnew.NETINFO_ITEM(b,name);b.Add(n);return n
    def track(self,b,n,a,z,layer=pcbnew.F_Cu):
        t=pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]),pcbnew.FromMM(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(z[0]),pcbnew.FromMM(z[1])))
        t.SetWidth(pcbnew.FromMM(.15));t.SetLayer(layer);t.SetNet(n);b.Add(t)
        return t
    def pad(self,b,n,xy):
        fp=pcbnew.FOOTPRINT(b);fp.SetReference("U1");b.Add(fp)
        p=pcbnew.PAD(fp);p.SetNumber("1");p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(pcbnew.LSET(pcbnew.F_Cu))
        p.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(.6),pcbnew.FromMM(.6)))
        p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(xy[0]),pcbnew.FromMM(xy[1])))
        p.SetNet(n);fp.Add(p)
        return p
    def test_only_padless_component_is_removed_and_pad_graph_is_unchanged(self):
        b=self.board();n=self.net(b,"CONTROL");self.pad(b,n,(10,10))
        self.track(b,n,(10,10),(11,10));orphan=self.track(b,n,(15,15),(16,15))
        found,before=audit(b)
        self.assertEqual(len(found),1)
        self.assertEqual([x["uuid"] for x in found[0]["objects"]],[orphan.m_Uuid.AsString()])
        b.Remove(orphan)
        after_found,after=audit(b)
        self.assertEqual(after_found,[]);self.assertEqual(before,after)
    def test_protected_or_locked_copper_is_never_cleanup_target(self):
        b=self.board()
        for i,name in enumerate(("+1V8","GND","USB2_SSTX_N","MG_TXC","SD_CLK")):
            self.track(b,self.net(b,name),(i,1),(i+.5,1))
        t=self.track(b,self.net(b,"CONTROL"),(10,1),(11,1));t.SetLocked(True)
        self.assertEqual(audit(b)[0],[])
    def test_plated_transition_preserves_pad_connected_back_copper(self):
        b=self.board();n=self.net(b,"CONTROL");self.pad(b,n,(10,10))
        self.track(b,n,(10,10),(11,10))
        v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(11),pcbnew.FromMM(10)))
        v.SetWidth(pcbnew.FromMM(.6));v.SetDrill(pcbnew.FromMM(.3));v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(pcbnew.F_Cu,pcbnew.B_Cu);v.SetNet(n);b.Add(v)
        self.track(b,n,(11,10),(12,10),pcbnew.B_Cu)
        self.assertEqual(audit(b)[0],[])

if __name__=="__main__":unittest.main()
