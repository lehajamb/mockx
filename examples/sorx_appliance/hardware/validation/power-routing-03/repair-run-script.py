import sys
sys.path.insert(0,'/hardware/tools')
import route_power_branches as r
r.ALLOCATIONS.update({'PCIE_RST#_3V3':(.15,.001,'Static open-drain reset, R65 pullup; not a PCIe differential pair'),'PCIE_RST#_1V8':(.15,.001,'Static reset logic input; not a PCIe differential pair')})
raise SystemExit(r.main())
