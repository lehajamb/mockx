#!/usr/bin/env python3
"""SORX carrier board rev A -- single source of truth for schematic, PCB and BOM.

Every part is declared once with its symbol, footprint, manufacturer part number and
pin-to-net map.  gen_sch.py, gen_pcb.py and gen_bom.py all import DESIGN from here, so the
schematic and the board cannot drift apart.
"""
import collections

VERDIN = """1=JTAG_1_TDI 2=ADC_1 3=JTAG_1_TRST# 4=ADC_2 5=JTAG_1_TDO 6=ADC_3 7=JTAG_1_VREF 8=ADC_4 9=JTAG_1_TCK 10=GND
11=GND 12=I2C_1_SDA 13=JTAG_1_TMS 14=I2C_1_SCL 15=PWM_1 16=PWM_2 17=GPIO_9_DSI 18=GND 19=PWM_3_DSI 20=CAN_1_TX
21=GPIO_10_DSI 22=CAN_1_RX 23=DSI_1_D3_N 24=CAN_2_TX 25=DSI_1_D3_P 26=CAN_2_RX 27=GND 28=GND 29=DSI_1_D2_N 30=I2S_1_BCLK
31=DSI_1_D2_P 32=I2S_1_SYNC 33=GND 34=I2S_1_D_OUT 35=DSI_1_CLK_N 36=I2S_1_D_IN 37=DSI_1_CLK_P 38=I2S_1_MCLK 39=GND 40=GND
41=DSI_1_D1_N 42=I2S_2_BCLK 43=DSI_1_D1_P 44=I2S_2_SYNC 45=GND 46=I2S_2_D_OUT 47=DSI_1_D0_N 48=I2S_2_D_IN 49=DSI_1_D0_P 50=GND
51=GND 52=QSPI_1_CLK 53=I2C_2_DSI_SDA 54=QSPI_1_CS# 55=I2C_2_DSI_SCL 56=QSPI_1_IO0 57=I2C_3_HDMI_SDA 58=QSPI_1_IO1 59=I2C_3_HDMI_SCL 60=QSPI_1_IO2
61=HDMI_1_HPD 62=QSPI_1_IO3 63=HDMI_1_CEC 64=QSPI_1_CS2# 65=GND 66=QSPI_1_DQS 67=HDMI_1_TXC_N 68=GND 69=HDMI_1_TXC_P 70=SD_1_D2
71=GND 72=SD_1_D3 73=HDMI_1_TXD0_N 74=SD_1_CMD 75=HDMI_1_TXD0_P 76=SD_1_PWR_EN 77=GND 78=SD_1_CLK 79=HDMI_1_TXD1_N 80=SD_1_D0
81=HDMI_1_TXD1_P 82=SD_1_D1 83=GND 84=SD_1_CD# 85=HDMI_1_TXD2_N 86=GND 87=HDMI_1_TXD2_P 88=MSP_1 89=GND 90=MSP_2
91=CSI_1_MCLK 92=MSP_3 93=I2C_4_CSI_SDA 94=MSP_4 95=I2C_4_CSI_SCL 96=MSP_5 97=GND 98=GND 99=CSI_1_D3_P 100=MSP_6
101=CSI_1_D3_N 102=MSP_7 103=GND 104=MSP_8 105=CSI_1_D2_P 106=MSP_9 107=CSI_1_D2_N 108=MSP_10 109=GND 110=GND
111=CSI_1_CLK_P 112=MSP_11 113=CSI_1_CLK_N 114=MSP_12 115=GND 116=MSP_13 117=CSI_1_D1_P 118=MSP_14 119=CSI_1_D1_N 120=MSP_15
121=GND 122=GND 123=CSI_1_D0_P 124=MSP_16 125=CSI_1_D0_N 126=MSP_17 127=GND 128=MSP_18 129=UART_1_RXD 130=MSP_19
131=UART_1_TXD 132=MSP_20 133=UART_1_CTS 134=GND 135=UART_1_RTS 136=MSP_21 137=UART_2_RXD 138=MSP_22 139=UART_2_TXD 140=MSP_23
141=UART_2_CTS 142=MSP_24 143=UART_2_RTS 144=MSP_25 145=GND 146=GND 147=UART_3_RXD 148=MSP_26 149=UART_3_TXD 150=MSP_27
151=UART_4_RXD 152=MSP_28 153=UART_4_TXD 154=MSP_29 155=USB_1_EN 156=MSP_30 157=USB_1_OC# 158=GND 159=USB_1_VBUS 160=MSP_31
161=USB_1_ID 162=MSP_32 163=USB_1_D_N 164=MSP_33 165=USB_1_D_P 166=MSP_34 167=GND 168=MSP_35 169=USB_2_SSTX_N 170=GND
171=USB_2_SSTX_P 172=MSP_36 173=GND 174=MSP_37 175=USB_2_SSRX_N 176=MSP_38 177=USB_2_SSRX_P 178=MSP_39 179=GND 180=MSP_40
181=USB_2_D_N 182=GND 183=USB_2_D_P 184=MSP_41 185=USB_2_EN 186=MSP_42 187=USB_2_OC# 188=MSP_43 189=ETH_2_RGMII_INT# 190=MSP_44
191=ETH_2_RGMII_MDIO 192=MSP_45 193=ETH_2_RGMII_MDC 194=GND 195=GND 196=SPI_1_CLK 197=ETH_2_RGMII_RXC 198=SPI_1_MISO 199=ETH_2_RGMII_RX_CTL 200=SPI_1_MOSI
201=ETH_2_RGMII_RXD_0 202=SPI_1_CS 203=ETH_2_RGMII_RXD_1 204=GND 205=ETH_2_RGMII_RXD_2 206=GPIO_1 207=ETH_2_RGMII_RXD_3 208=GPIO_2 209=GND 210=GPIO_3
211=ETH_2_RGMII_TX_CTL 212=GPIO_4 213=ETH_2_RGMII_TXC 214=PWR_1V8_MOCI 215=ETH_2_RGMII_TXD_3 216=GPIO_5_CSI 217=ETH_2_RGMII_TXD_2 218=GPIO_6_CSI 219=ETH_2_RGMII_TXD_1 220=GPIO_7_CSI
221=ETH_2_RGMII_TXD_0 222=GPIO_8_CSI 223=GND 224=GND 225=ETH_1_MDI0_P 226=PCIE_1_CLK_N 227=ETH_1_MDI0_N 228=PCIE_1_CLK_P 229=GND 230=GND
231=ETH_1_MDI1_N 232=PCIE_1_L0_RX_N 233=ETH_1_MDI1_P 234=PCIE_1_L0_RX_P 235=ETH_1_LED_1 236=GND 237=ETH_1_LED_2 238=PCIE_1_L0_TX_N 239=ETH_1_MDI2_P 240=PCIE_1_L0_TX_P
241=ETH_1_MDI2_N 242=GND 243=GND 244=PCIE_1_RESET# 245=ETH_1_MDI3_N 246=CTRL_RECOVERY_MICO# 247=ETH_1_MDI3_P 248=CTRL_PWR_BTN_MICO# 249=VCC_BACKUP 250=CTRL_FORCE_OFF_MOCI#
251=VCC 252=CTRL_WAKE1_MICO# 253=VCC 254=CTRL_PWR_EN_MOCI 255=VCC 256=CTRL_SLEEP_MOCI# 257=VCC 258=CTRL_RESET_MOCI# 259=VCC 260=CTRL_RESET_MICO#"""
VERDIN_PINS = {int(k): v for k, v in (t.split('=') for t in VERDIN.split())}

# Verdin signal -> carrier net (signals not listed are left unconnected)
SOM_NET = {
    'GND': 'GND', 'VCC': '+5V', 'VCC_BACKUP': 'VBAT_RTC', 'PWR_1V8_MOCI': '+1V8',
    'ETH_1_MDI0_P': 'MD_MDI0_P', 'ETH_1_MDI0_N': 'MD_MDI0_N', 'ETH_1_MDI1_P': 'MD_MDI1_P', 'ETH_1_MDI1_N': 'MD_MDI1_N',
    'ETH_1_MDI2_P': 'MD_MDI2_P', 'ETH_1_MDI2_N': 'MD_MDI2_N', 'ETH_1_MDI3_P': 'MD_MDI3_P', 'ETH_1_MDI3_N': 'MD_MDI3_N',
    'ETH_1_LED_1': 'MD_LED_ACT#', 'ETH_1_LED_2': 'MD_LED_LINK#',
    'ETH_2_RGMII_TXC': 'MG_TXC', 'ETH_2_RGMII_TX_CTL': 'MG_TX_CTL', 'ETH_2_RGMII_TXD_0': 'MG_TXD0', 'ETH_2_RGMII_TXD_1': 'MG_TXD1',
    'ETH_2_RGMII_TXD_2': 'MG_TXD2', 'ETH_2_RGMII_TXD_3': 'MG_TXD3', 'ETH_2_RGMII_RXC': 'MG_RXC', 'ETH_2_RGMII_RX_CTL': 'MG_RX_CTL',
    'ETH_2_RGMII_RXD_0': 'MG_RXD0', 'ETH_2_RGMII_RXD_1': 'MG_RXD1', 'ETH_2_RGMII_RXD_2': 'MG_RXD2', 'ETH_2_RGMII_RXD_3': 'MG_RXD3',
    'ETH_2_RGMII_MDC': 'MG_MDC', 'ETH_2_RGMII_MDIO': 'MG_MDIO', 'ETH_2_RGMII_INT#': 'MG_INT#',
    'PCIE_1_CLK_P': 'PCIE_CLK_P', 'PCIE_1_CLK_N': 'PCIE_CLK_N', 'PCIE_1_L0_TX_P': 'PCIE_TX_P', 'PCIE_1_L0_TX_N': 'PCIE_TX_N',
    'PCIE_1_L0_RX_P': 'PCIE_RX_P', 'PCIE_1_L0_RX_N': 'PCIE_RX_N', 'PCIE_1_RESET#': 'PCIE_RST#_1V8',
    'USB_2_SSTX_P': 'USB2_SSTX_P', 'USB_2_SSTX_N': 'USB2_SSTX_N', 'USB_2_SSRX_P': 'USB2_SSRX_P', 'USB_2_SSRX_N': 'USB2_SSRX_N',
    'USB_2_D_P': 'USB2_D_P', 'USB_2_D_N': 'USB2_D_N', 'USB_2_EN': 'USB2_EN', 'USB_2_OC#': 'USB2_OC#',
    'USB_1_D_P': 'USB1_D_P', 'USB_1_D_N': 'USB1_D_N', 'USB_1_EN': 'USB1_EN', 'USB_1_OC#': 'USB1_OC#',
    'USB_1_VBUS': 'USB1_VBUS_DET', 'USB_1_ID': 'USB1_ID',
    'SD_1_CLK': 'SD_CLK', 'SD_1_CMD': 'SD_CMD', 'SD_1_D0': 'SD_D0', 'SD_1_D1': 'SD_D1', 'SD_1_D2': 'SD_D2', 'SD_1_D3': 'SD_D3',
    'SD_1_CD#': 'SD_CD#', 'SD_1_PWR_EN': 'SD_PWR_EN',
    'UART_3_RXD': 'CON_RXD_1V8', 'UART_3_TXD': 'CON_TXD_1V8', 'UART_1_RXD': 'AUX_RXD', 'UART_1_TXD': 'AUX_TXD',
    'I2C_1_SDA': 'I2C1_SDA', 'I2C_1_SCL': 'I2C1_SCL',
    'SPI_1_CLK': 'FRAM_SCK', 'SPI_1_MISO': 'FRAM_MISO', 'SPI_1_MOSI': 'FRAM_MOSI', 'SPI_1_CS': 'FRAM_CS#',
    'PWM_1': 'FAN_PWM_1V8', 'ADC_1': 'IMON_ADC',
    'GPIO_1': 'PPS_GPIO', 'GPIO_2': 'WD_KICK', 'GPIO_3': 'WD_EN', 'GPIO_4': 'WD_SET0_N', 'GPIO_5_CSI': 'FAN_TACH',
    'GPIO_6_CSI': 'PPS_QOS', 'GPIO_7_CSI': 'IOEXP_INT#', 'GPIO_8_CSI': 'PD_ALERT#', 'GPIO_9_DSI': 'ALARM_OFF',
    'I2S_1_D_IN': 'PPS_FEC',
    'CTRL_RESET_MOCI#': 'RESET_MOCI#', 'CTRL_RESET_MICO#': 'RESET_MICO#', 'CTRL_PWR_EN_MOCI': 'PWR_EN',
    'CTRL_RECOVERY_MICO#': 'RECOVERY#', 'CTRL_WAKE1_MICO#': 'WAKE1#',
}

Part = collections.namedtuple('Part', 'ref sym value fp sheet pins nc mpn mfr desc dnp alt inbom')


class Design:
    def __init__(self):
        self.parts = collections.OrderedDict()
        self.cnt = collections.Counter()
        self.sheet = 'som'

    def ref(self, prefix):
        self.cnt[prefix] += 1
        return '%s%d' % (prefix, self.cnt[prefix])

    def part(self, prefix, sym, value, fp, pins, nc=(), mpn='', mfr='', desc='', dnp=False, alt='', inbom=True, ref=None):
        r = ref or self.ref(prefix)
        assert r not in self.parts, r
        self.parts[r] = Part(r, sym, value, fp, self.sheet, dict(pins), tuple(nc), mpn, mfr, desc, dnp, alt, inbom)
        return r

    # ---- passives -----------------------------------------------------------------------------
    def R(self, value, a, b, size='0402', dnp=False, tol='1%'):
        mpn = RES_MPN(value, size)
        return self.part('R', 'SORX:R', value, 'Resistor_SMD:R_%s' % FP_SIZE[size], {'1': a, '2': b}, mpn=mpn, mfr='Yageo',
                         desc='Resistor %s %s %s' % (value, tol, size), dnp=dnp, alt='Any %s %s thick-film %s, e.g. Vishay CRCW, Panasonic ERJ' % (size, tol, value))

    def C(self, value, a, b='GND', size=None, volt=None, dnp=False):
        key = (value, size, volt)
        size, volt, mpn, mfr, diel = CAP_SEL(value, size, volt)
        return self.part('C', 'SORX:C', value, 'Capacitor_SMD:C_%s' % FP_SIZE[size], {'1': a, '2': b}, mpn=mpn, mfr=mfr,
                         desc='MLCC %s %s %s %s' % (value, volt, diel, size), dnp=dnp, alt='Any %s %s %s %s MLCC (Murata GRM, TDK C, Kemet C)' % (value, volt, diel, size))

    def TP(self, net, name=None):
        return self.part('TP', 'SORX:TP', name or net, 'TestPoint:TestPoint_Pad_D1.0mm', {'1': net}, desc='Test pad', inbom=False)

    def FLAG(self, net):
        return None   # PWR_FLAG symbols are derived automatically by gen_sch.py from pin types


FP_SIZE = {'0402': '0402_1005Metric', '0603': '0603_1608Metric', '0805': '0805_2012Metric', '1206': '1206_3216Metric', '1210': '1210_3225Metric'}


def RES_MPN(value, size):
    v = value.replace('Ω', '').strip()
    code = {'0': '0R', '22': '22R', '27': '27R', '33': '33R', '49.9': '49R9', '100': '100R', '330': '330R', '470': '470R',
            '1k': '1K', '1.5k': '1K5', '1.8k': '1K8', '4.7k': '4K7', '4.99k': '4K99', '5.1k': '5K1', '8.2k': '8K2', '10k': '10K', '11k': '11K',
            '12.1k': '12K1', '24.9k': '24K9', '43.2k': '43K2', '46.4k': '46K4', '53.6k': '53K6', '91k': '91K', '100k': '100K', '909k': '909K'}[v]
    return 'RC%s%sR-07%sL' % (size, 'J' if v == '0' else 'F', code)


def CAP_SEL(value, size, volt):
    T = {  # value: (size, volt, mpn, mfr, dielectric)
        '47pF': ('0402', '50V', 'GRM1555C1H470JA01D', 'Murata', 'C0G'),
        '27pF': ('0402', '50V', 'GRM1555C1H270JA01D', 'Murata', 'C0G'),
        '18pF': ('0402', '50V', 'GRM1555C1H180JA01D', 'Murata', 'C0G'),
        '1nF': ('0402', '50V', 'GRM155R71H102KA01D', 'Murata', 'X7R'),
        '22nF': ('0402', '50V', 'GRM155R71H223KA12D', 'Murata', 'X7R'),
        '39nF': ('0402', '25V', 'GRM155R71E393KA88D', 'Murata', 'X7R'),
        '100nF': ('0402', '16V', 'GRM155R71C104KA88D', 'Murata', 'X7R'),
        '100nF/50V': ('0603', '50V', 'GRM188R71H104KA93D', 'Murata', 'X7R'),
        '1uF': ('0603', '25V', 'GRM188R61E105KA12D', 'Murata', 'X5R'),
        '4.7uF': ('0603', '10V', 'GRM188R61A475KE15D', 'Murata', 'X5R'),
        '4.7uF/35V': ('0805', '35V', 'GRM21BR6YA475KA73L', 'Murata', 'X5R'),
        '10uF': ('0805', '10V', 'GRM21BR61A106KE19L', 'Murata', 'X5R'),
        '10uF/35V': ('1210', '35V', 'GRM32ER7YA106KA12L', 'Murata', 'X7R'),
        '22uF': ('1206', '16V', 'GRM31CR61C226ME15L', 'Murata', 'X5R'),
    }
    s, v, mpn, mfr, d = T[value]
    return s, v, mpn, mfr, d


D = Design()
P = D.part

# ================================================================================================
D.sheet = 'som'
som_pins = {str(n): SOM_NET[s] for n, s in VERDIN_PINS.items() if s in SOM_NET}
som_pins.update({'261': 'GND', '262': 'GND'})
som_nc = [str(n) for n, s in VERDIN_PINS.items() if s not in SOM_NET]
P('J', 'SORX:Verdin_SODIMM260', 'Verdin SoM socket', 'sorx:SODIMM-260_DDR4_TE_2309409-2', som_pins, som_nc, mpn='2309409-2', mfr='TE Connectivity',
  desc='DDR4 SODIMM socket 260 pos 0.5 mm, 5.2 mm height (Verdin recommended)', alt='None fitted in rev A; Toradex qualifies TE 2309409-2 only')
P('M', 'SORX:SOM', 'Verdin iMX8M Plus Quad 4GB IT', '', {}, mpn='0063', mfr='Toradex', desc='SoM, NXP i.MX 8M Plus, 4 GB / 32 GB, plugs into J1',
  alt='Verdin iMX8M Plus Quad 4GB WB IT (0058); Verdin iMX95 (verify reserved pins)')
for v in ('22uF', '22uF', '4.7uF', '4.7uF', '100nF', '100nF'):
    D.C(v, '+5V')
D.C('4.7uF', '+1V8'); D.C('100nF', '+1V8')
D.R('1.8k', '+1V8', 'I2C1_SDA'); D.R('1.8k', '+1V8', 'I2C1_SCL')
D.R('10k', '+1V8', 'RESET_MOCI#'); D.R('100k', '+1V8', 'WAKE1#'); D.R('100k', 'PWR_EN', 'GND')
# RTC backup
P('BT', 'Device:Battery_Cell', 'CR2032', 'Battery:BatteryHolder_Keystone_3034_1x20mm', {'1': 'VBAT_CELL', '2': 'GND'}, mpn='3034', mfr='Keystone',
  desc='SMT retainer for 20 mm coin cell (CR2032)', alt='MPD BC2003 (different footprint)')
P('D', 'SORX:D', 'BAT54WS', 'Diode_SMD:D_SOD-323', {'A': 'VBAT_CELL', 'K': 'VBAT_D'}, mpn='BAT54WS-7-F', mfr='Diodes Inc.', desc='Schottky 30 V 200 mA SOD-323',
  alt='Nexperia BAT54J, onsemi BAT54HT1G')
D.R('1k', 'VBAT_D', 'VBAT_RTC'); D.C('100nF', 'VBAT_RTC')
# reset / recovery
P('SW', 'Switch:SW_Push', 'RESET', 'Button_Switch_THT:SW_Tactile_SKHH_Angled', {'1': 'RESET_SW', '2': 'GND'}, mpn='SKHHLQA010', mfr='Alps Alpine',
  desc='Tactile switch, right angle, through hole', alt='C&K PTS645VL58-2 LFS, TE 1825027 series (check footprint)')
D.R('100', 'RESET_MICO#', 'RESET_SW'); D.C('100nF', 'RESET_MICO#')
P('SW', 'Switch:SW_Push', 'RECOVERY', 'Button_Switch_THT:SW_Tactile_SKHH_Angled', {'1': 'RECOVERY_SW', '2': 'GND'}, mpn='SKHHLQA010', mfr='Alps Alpine',
  desc='Tactile switch, right angle, through hole', alt='C&K PTS645VL58-2 LFS')
D.R('470', 'RECOVERY#', 'RECOVERY_SW')
# FRAM journal head (populate option approved at checkpoint 1)
P('U', 'Memory_EEPROM:25LCxxx', 'CY15B104QN', 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',
  {'1': 'FRAM_CS#', '2': 'FRAM_MISO', '3': 'FRAM_WP#', '4': 'GND', '5': 'FRAM_MOSI', '6': 'FRAM_SCK', '7': 'FRAM_HOLD#', '8': '+1V8'},
  mpn='CY15B104QN-50SXI', mfr='Infineon', desc='4 Mbit SPI F-RAM, 1.8-3.6 V, 50 MHz, SOIC-8', alt='Fujitsu MB85RS4MTY (1.8 V, SOP-8); Everspin MR25H40 needs 3.3 V level shift')
D.R('10k', '+1V8', 'FRAM_CS#'); D.R('10k', '+1V8', 'FRAM_WP#'); D.R('10k', '+1V8', 'FRAM_HOLD#'); D.C('100nF', '+1V8')
# AUX header (bring-up)
P('J', 'Connector_Generic:Conn_01x06', 'AUX 1V8', 'Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical',
  {'1': '+1V8', '2': 'AUX_TXD', '3': 'AUX_RXD', '4': 'I2C1_SDA', '5': 'I2C1_SCL', '6': 'GND'}, mpn='61300611121', mfr='Wurth Elektronik',
  desc='Pin header 1x6 2.54 mm (bring-up, not fitted)', dnp=True, alt='Any 2.54 mm 1x6 header')
for n in ('+5V', '+1V8', 'RESET_MOCI#', 'PWR_EN', 'GND', 'GND'):
    D.TP(n)
D.FLAG('+1V8'); D.FLAG('VBAT_CELL'); D.FLAG('GND')

# ================================================================================================
D.sheet = 'power'
USBC = dict(sym='Connector:USB_C_Receptacle_USB2.0_16P', fp='Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal',
            mpn='USB4105-GF-A', mfr='GCT', desc='USB Type-C receptacle, 16 pin, USB 2.0, 5 A VBUS', alt='HRO TYPE-C-31-M-12 (footprint differs); Molex 216990-0001')
P('J', USBC['sym'], 'USB-C PWR', USBC['fp'], {'VBUS': 'VBUS_IN', 'GND': 'GND', 'CC1': 'PD_CC1', 'CC2': 'PD_CC2', 'SHIELD': 'GND'},
  nc=['A6', 'A7', 'B6', 'B7', 'A8', 'B8'], mpn=USBC['mpn'], mfr=USBC['mfr'], desc=USBC['desc'], alt=USBC['alt'])
P('D', 'SORX:TVS', 'SMAJ22A', 'Diode_SMD:D_SMA', {'A': 'GND', 'K': 'VBUS_IN'}, mpn='SMAJ22A', mfr='Littelfuse', desc='TVS 22 V standoff 400 W SMA, unidirectional',
  alt='Bourns SMAJ22A, Vishay SMAJ22A-E3/61')
D.C('4.7uF/35V', 'VBUS_IN'); D.C('100nF/50V', 'VBUS_IN')
P('U', 'Interface_USB:STUSB4500QTR', 'STUSB4500', 'Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm',
  {'1': 'PD_CC1', '2': 'PD_CC1', '4': 'PD_CC2', '5': 'PD_CC2', '6': 'GND', '7': 'I2C1_SCL', '8': 'I2C1_SDA', '10': 'GND', '11': 'PD_ATTACH#',
   '12': 'GND', '13': 'GND', '16': 'PD_EN_SNK#', '18': 'PD_VS', '19': 'PD_ALERT#', '21': 'PD_1V2', '22': 'GND', '23': 'PD_2V7', '24': 'VBUS_IN', '25': 'GND'},
  nc=['9', '14', '15', '17', '20'], mpn='STUSB4500QTR', mfr='STMicroelectronics', desc='USB PD sink controller, NVM-configured, QFN-24',
  alt='None drop-in; TI TPS25730 or Infineon CYPD3177 need a layout change')
D.R('1k', 'VBUS_IN', 'PD_VS'); D.C('1uF', 'PD_1V2'); D.C('1uF', 'PD_2V7'); D.C('100nF/50V', 'VBUS_IN')
D.R('10k', '+1V8', 'PD_ALERT#'); D.R('100k', '+1V8', 'PD_ATTACH#'); D.R('100k', 'VBUS_IN', 'PD_EN_SNK#')
# reverse-polarity FET + eFuse
P('Q', 'SORX:NMOS_SO8', 'DMT6009LSS', 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',
  {'S': 'VBUS_IN', 'G': 'EF_BGATE', 'D': 'VIN_PROT'}, mpn='DMT6009LSS-13', mfr='Diodes Inc.', desc='N-MOSFET 60 V, <15 mOhm, SO-8 (reverse-polarity blocking)',
  alt='Any 60 V SO-8 N-FET with standard pinout, e.g. Vishay SI4062DY, AOS AO4264E')
P('Q', 'Transistor_FET:2N7002', '2N7002', 'Package_TO_SOT_SMD:SOT-23', {'1': 'EF_DRV', '2': 'VBUS_IN', '3': 'EF_BGATE'}, mpn='2N7002-7-F', mfr='Diodes Inc.',
  desc='N-MOSFET 60 V SOT-23 (fast gate pull-down)', alt='onsemi 2N7002LT1G, Nexperia 2N7002')
P('U', 'Power_Management:TPS26631PWP', 'TPS26631', 'Package_SO:HTSSOP-20-1EP_4.4x6.5mm_P0.65mm_EP3.4x6.5mm_Mask2.96x2.96mm_ThermalVias',
  {'IN': 'VIN_PROT', 'B_GATE': 'EF_BGATE', 'DRV': 'EF_DRV', 'IN_SYS': 'VBUS_IN', 'UVLO': 'EF_UVLO', 'OVP': 'EF_OVP', 'GND': 'GND', 'dVdT': 'EF_DVDT',
   'ILIM': 'EF_ILIM', 'MODE': 'EF_MODE', '~{SHDN}': 'EF_SHDN#', 'IMON': 'EF_IMON', '~{FLT}': 'PWR_FLT#', 'PGTH': 'EF_PGTH', 'PGOOD': 'EF_PGOOD', 'OUT': 'VSYS'},
  mpn='TPS26631PWPR', mfr='Texas Instruments', desc='eFuse 4.5-60 V 6 A, adjustable ILIM/UVLO/OVP, reverse-polarity FET driver, HTSSOP-20',
  alt='TPS26630PWPR (same footprint, different fault response)')
D.C('100nF/50V', 'VBUS_IN'); D.C('10uF/35V', 'VIN_PROT')
D.R('909k', 'VBUS_IN', 'EF_UVLO'); D.R('46.4k', 'EF_UVLO', 'EF_OVP'); D.R('53.6k', 'EF_OVP', 'GND')
D.C('22nF', 'EF_DVDT'); D.R('8.2k', 'EF_ILIM', 'GND'); D.R('0', 'EF_MODE', 'GND', dnp=True)
D.R('24.9k', 'EF_IMON', 'GND'); D.R('1k', 'EF_IMON', 'IMON_ADC'); D.C('100nF', 'IMON_ADC')
D.R('100k', '+1V8', 'PWR_FLT#'); D.R('91k', 'VSYS', 'EF_PGTH'); D.R('11k', 'EF_PGTH', 'GND'); D.R('100k', '+5V', 'EF_PGOOD')
D.C('10uF/35V', 'VSYS'); D.C('10uF/35V', 'VSYS')
P('C', 'SORX:CP', '100uF/35V', 'Capacitor_SMD:CP_Elec_6.3x7.7', {'1': 'VSYS', '2': 'GND'}, mpn='EEE-FK1V101XP', mfr='Panasonic', desc='Al electrolytic 100 uF 35 V 6.3x7.7 mm',
  alt='Nichicon UCD1V101MCL1GS, Wurth 865080545012')


def buck(name, vout_net, en_net, rfbb, always_on):
    p = 'B' + name
    P('U', 'Regulator_Switching:LMR33630ADDA', 'LMR33630ADDA', 'Package_SO:Texas_HSOP-8-1EP_3.9x4.9mm_P1.27mm_ThermalVias',
      {'1': 'GND', '2': 'VSYS', '3': en_net, '4': p + '_PG', '5': p + '_FB', '6': p + '_VCC', '7': p + '_BOOT', '8': p + '_SW', '9': 'GND'},
      mpn='LMR33630ADDAR', mfr='Texas Instruments', desc='Buck 3.8-36 V in, 3 A, 400 kHz, HSOIC-8 (%s)' % vout_net,
      alt='LMR33630BDDAR (1.4 MHz, change L to 4.7 uH)')
    D.C('10uF/35V', 'VSYS'); D.C('100nF/50V', 'VSYS'); D.C('1uF', p + '_VCC'); D.C('100nF/50V', p + '_BOOT', p + '_SW')
    P('L', 'SORX:L', '10uH', 'Inductor_SMD:L_Coilcraft_XAL5050-XXX', {'1': p + '_SW', '2': vout_net}, mpn='XAL5050-103MEC', mfr='Coilcraft',
      desc='Shielded power inductor 10 uH, 4.9 A Isat, 5.3x5.5 mm', alt='Wurth 74439346100 (check pads), Bourns SRP5050FA-100M')
    D.R('100k', vout_net, p + '_FB'); D.R(rfbb, p + '_FB', 'GND')
    for _ in range(3):
        D.C('22uF', vout_net)
    D.R('100k', vout_net, p + '_PG')
    if always_on:
        D.R('100k', 'VSYS', en_net)


buck('5S', '+5V', 'B5S_EN', '24.9k', True)        # SoM rail, always on once VSYS is valid
buck('5U', '+5V_USB', 'PWR_EN', '24.9k', False)   # USB-A VBUS + fan, gated by SoM
buck('3V3', '+3V3', 'PWR_EN', '43.2k', False)     # carrier 3.3 V, gated by SoM
P('U', 'Regulator_Linear:AZ1117-1.2', 'AZ1117C-1.2', 'Package_TO_SOT_SMD:SOT-223-3_TabPin2', {'1': 'GND', '2': '+1V2_PHY', '3': '+3V3'},
  mpn='AZ1117CH-1.2TRG1', mfr='Diodes Inc.', desc='LDO 1.2 V 1 A SOT-223 (management PHY core)', alt='TI LM1117MPX-1.2 is not offered; use AMS1117-1.2 or LD1117S12TR')
D.C('10uF', '+3V3'); D.C('22uF', '+1V2_PHY'); D.C('100nF', '+1V2_PHY')
for n in ('VBUS_IN', 'VSYS', '+5V_USB', '+3V3', '+1V2_PHY', 'EF_PGOOD', 'EF_SHDN#', 'PD_ATTACH#', 'PD_EN_SNK#', 'GND'):
    D.TP(n)
for n in ('VBUS_IN', 'VIN_PROT', 'VSYS', '+5V', '+5V_USB', '+3V3', '+1V2_PHY'):
    D.FLAG(n)

# ================================================================================================
MAGJACK = dict(sym='Connector:RJ45_JK0654219', fp='Connector_RJ:RJ45_Pulse_JK0654219NL_Horizontal', mpn='JK0654219NL', mfr='Pulse Electronics',
               desc='RJ45 with 1000BASE-T magnetics, 4 separate centre taps, 2 LEDs, tab up', alt='None drop-in; Bel 0826-1G1T-23-F and Wurth 7499111446 need a footprint change')


def magjack(label, p, led1_k, led2_k):
    P('J', MAGJACK['sym'], label, MAGJACK['fp'],
      {'11': p + '_MDI0_P', '10': p + '_MDI0_N', '4': p + '_MDI1_P', '5': p + '_MDI1_N', '3': p + '_MDI2_P', '2': p + '_MDI2_N', '8': p + '_MDI3_P', '9': p + '_MDI3_N',
       '12': p + '_CT1', '6': p + '_CT2', '1': p + '_CT3', '7': p + '_CT4', '16': p + '_LED1_A', '15': led1_k, '14': p + '_LED2_A', '13': led2_k, 'SH': 'GND'},
      mpn=MAGJACK['mpn'], mfr=MAGJACK['mfr'], desc=MAGJACK['desc'], alt=MAGJACK['alt'])
    for i in range(1, 5):
        D.C('100nF', '%s_CT%d' % (p, i))
    D.R('330', '+3V3', p + '_LED1_A'); D.R('330', '+3V3', p + '_LED2_A')


D.sheet = 'eth_md'
magjack('MD-IN', 'MD', 'MD_LED_LINK#', 'MD_LED_ACT#')
D.C('1nF', 'MD_LED_LINK#'); D.C('1nF', 'MD_LED_ACT#')
# PPS input
P('J', 'Connector:Conn_Coaxial', 'PPS IN', 'Connector_Coaxial:SMA_Amphenol_132289_EdgeMount', {'1': 'PPS_RAW', '2': 'GND'}, mpn='132289', mfr='Amphenol RF',
  desc='SMA jack, edge mount, 50 ohm, for 1.57 mm PCB', alt='Cinch 142-0701-801, Molex 73251-1153 (footprint in library)')
D.R('49.9', 'PPS_RAW', 'GND', dnp=True); D.R('100', 'PPS_RAW', 'PPS_F'); D.R('100k', 'PPS_F', 'GND')
P('D', 'SORX:ESD', 'ESD9B5.0', 'Diode_SMD:D_SOD-923', {'A': 'GND', 'K': 'PPS_F'}, mpn='ESD9B5.0ST5G', mfr='onsemi', desc='ESD diode 5 V bidirectional SOD-923',
  alt='Nexperia PESD5V0S1BL (SOD-882, footprint differs)')
G = dict(fp='Package_TO_SOT_SMD:SOT-23-5', mfr='Texas Instruments')
P('U', '74xGxx:74LVC1G17', '74LVC1G17', G['fp'], {'2': 'PPS_F', '3': 'GND', '4': 'PPS_3V3', '5': '+3V3'}, mpn='SN74LVC1G17DBVR', mfr=G['mfr'],
  desc='Schmitt buffer, 5 V tolerant input, SOT-23-5 (PPS to I210 SDP0)', alt='Nexperia 74LVC1G17GV, Diodes 74LVC1G17W5-7')
P('U', '74xGxx:74LVC1G17', '74LVC1G17', G['fp'], {'2': 'PPS_F', '3': 'GND', '4': 'PPS_1V8', '5': '+1V8'}, mpn='SN74LVC1G17DBVR', mfr=G['mfr'],
  desc='Schmitt buffer at 1.8 V (PPS to SoM)', alt='Nexperia 74LVC1G17GV')
D.C('100nF', '+3V3'); D.C('100nF', '+1V8')
D.R('33', 'PPS_3V3', 'OE_PPS_IN'); D.R('33', 'PPS_1V8', 'PPS_GPIO'); D.R('33', 'PPS_1V8', 'PPS_QOS'); D.R('33', 'PPS_1V8', 'PPS_FEC')
D.TP('PPS_3V3')

D.sheet = 'eth_oe'
P('U', 'Interface_Ethernet:WGI210AT', 'I210-AT', 'Package_DFN_QFN:QFN-64-1EP_9x9mm_P0.5mm_EP3.4x3.4mm',
  {'1': 'OE_PWRGD', '2': 'OE_NCSI_PD', '7': 'OE_NCSI_PD', '8': 'OE_NCSI_PD', '9': 'OE_NCSI_PD', '43': 'OE_NCSI_PD',
   '10': '+3V3', '27': '+3V3', '41': '+3V3', '51': '+3V3', '64': '+3V3', '11': 'OE_0V9', '32': 'OE_0V9', '42': 'OE_0V9', '59': 'OE_0V9', '38': 'OE_0V9',
   '39': 'OE_1V5', '47': 'OE_1V5', '56': 'OE_1V5', '37': 'OE_CBOT', '40': 'OE_CTOP',
   '12': 'OE_NVM_SI', '13': 'OE_NVM_SK', '14': 'OE_NVM_SO', '15': 'OE_NVM_CS#', '16': 'OE_WAKE#', '17': 'PCIE_RST#_3V3', '18': 'OE_JTAG_PU', '29': 'OE_JTAG_PU',
   '19': 'OE_JTAG_TCK', '20': 'OE_PET_N', '21': 'OE_PET_P', '23': 'PCIE_TX_N', '24': 'PCIE_TX_P', '25': 'PCIE_CLK_N', '26': 'PCIE_CLK_P', '28': 'OE_DEVOFF#',
   '30': 'OE_LED1#', '31': 'OE_LED0#', '34': 'OE_SMB_CLK', '35': 'OE_SMB_ALRT#', '36': 'OE_SMB_DATA', '45': 'OE_XTAL2', '46': 'OE_XTAL1', '48': 'OE_RSET',
   '58': 'OE_MDI0_P', '57': 'OE_MDI0_N', '55': 'OE_MDI1_P', '54': 'OE_MDI1_N', '53': 'OE_MDI2_P', '52': 'OE_MDI2_N', '50': 'OE_MDI3_P', '49': 'OE_MDI3_N',
   '60': 'OE_SDP3', '61': 'OE_PPS_OUT', '62': 'OE_SDP2', '63': 'OE_PPS_IN', '65': 'GND'},
  nc=['3', '4', '5', '6', '22', '33', '44'], mpn='WGI210AT S LJXR', mfr='Intel', desc='GbE controller, PCIe, IEEE 1588, 4 SDP, QFN-64 (order entry port)',
  alt='WGI210IT (industrial temp, same footprint)')
for n in ('+3V3',) * 5:
    D.C('100nF', n)
D.C('10uF', '+3V3')
for n in ('OE_0V9',) * 4:
    D.C('100nF', n)
D.C('10uF', 'OE_0V9'); D.C('100nF', 'OE_1V5'); D.C('100nF', 'OE_1V5'); D.C('10uF', 'OE_1V5'); D.C('39nF', 'OE_CTOP', 'OE_CBOT')
D.R('10k', '+3V3', 'OE_PWRGD'); D.R('10k', 'OE_NCSI_PD', 'GND'); D.R('10k', '+3V3', 'OE_WAKE#'); D.R('10k', '+3V3', 'OE_JTAG_PU'); D.R('1k', 'OE_JTAG_TCK', 'GND')
D.R('10k', '+3V3', 'OE_DEVOFF#'); D.R('10k', '+3V3', 'OE_SMB_CLK'); D.R('10k', '+3V3', 'OE_SMB_DATA'); D.R('10k', '+3V3', 'OE_SMB_ALRT#')
D.R('4.99k', 'OE_RSET', 'GND'); D.R('10k', 'OE_SDP2', 'GND'); D.R('10k', 'OE_SDP3', 'GND'); D.R('10k', 'OE_PPS_IN', 'GND')
XTAL = dict(sym='Device:Crystal_GND24', fp='Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm', mpn='ABM8-25.000MHZ-B2-T', mfr='Abracon',
            desc='Crystal 25 MHz 18 pF +/-20 ppm 3.2x2.5 mm', alt='ECS-250-18-33-JGN-TR, TXC 7M-25.000MAAJ-T')
P('Y', XTAL['sym'], '25MHz', XTAL['fp'], {'1': 'OE_XTAL1', '3': 'OE_XTAL2', '2': 'GND', '4': 'GND'}, mpn=XTAL['mpn'], mfr=XTAL['mfr'], desc=XTAL['desc'], alt=XTAL['alt'])
D.C('27pF', 'OE_XTAL1'); D.C('27pF', 'OE_XTAL2')
P('U', 'Memory_Flash:W25Q32JVSS', 'W25Q16JV', 'Package_SO:SOIC-8_5.23x5.23mm_P1.27mm',
  {'1': 'OE_NVM_CS#', '2': 'OE_NVM_SO', '3': 'OE_NVM_WP#', '4': 'GND', '5': 'OE_NVM_SI', '6': 'OE_NVM_SK', '7': 'OE_NVM_HOLD#', '8': '+3V3'},
  mpn='W25Q16JVSSIQ', mfr='Winbond', desc='SPI NOR flash 16 Mbit, SOIC-8 208 mil (I210 NVM image and MAC address)', alt='Macronix MX25L1606EM2I-12G, W25Q32JVSSIQ')
D.R('10k', '+3V3', 'OE_NVM_WP#'); D.R('10k', '+3V3', 'OE_NVM_HOLD#'); D.R('10k', '+3V3', 'OE_NVM_CS#'); D.C('100nF', '+3V3')
D.C('100nF', 'OE_PET_P', 'PCIE_RX_P'); D.C('100nF', 'OE_PET_N', 'PCIE_RX_N')
P('U', '74xGxx:74LVC1G07', '74LVC1G07', G['fp'], {'2': 'PCIE_RST#_1V8', '3': 'GND', '4': 'PCIE_RST#_3V3', '5': '+1V8'}, nc=['1'], mpn='SN74LVC1G07DBVR', mfr=G['mfr'],
  desc='Open-drain buffer (PERST# 1.8 V to 3.3 V)', alt='Nexperia 74LVC1G07GV, Diodes 74LVC1G07W5-7')
D.R('10k', '+3V3', 'PCIE_RST#_3V3'); D.C('100nF', '+1V8')
magjack('OE', 'OE', 'OE_LED0#', 'OE_LED1#')
D.TP('OE_PPS_OUT'); D.TP('OE_0V9'); D.TP('OE_1V5')

D.sheet = 'eth_mgmt'
P('U', 'Interface_Ethernet:KSZ9031RNXCA', 'KSZ9131RNX', 'Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm_EP5.15x5.15mm',
  {'1': '+3V3', '12': '+3V3', '4': '+1V2_PHY', '9': '+1V2_PHY', '44': 'MG_1V2_PLL', '14': '+1V2_PHY', '18': '+1V2_PHY', '23': '+1V2_PHY', '26': '+1V2_PHY',
   '30': '+1V2_PHY', '39': '+1V2_PHY', '16': '+1V8', '34': '+1V8', '40': '+1V8', '29': 'GND', '49': 'GND',
   '2': 'MG_MDI0_P', '3': 'MG_MDI0_N', '5': 'MG_MDI1_P', '6': 'MG_MDI1_N', '7': 'MG_MDI2_P', '8': 'MG_MDI2_N', '10': 'MG_MDI3_P', '11': 'MG_MDI3_N',
   '15': 'MG_LED2', '17': 'MG_LED1', '19': 'MG_TXD0', '20': 'MG_TXD1', '21': 'MG_TXD2', '22': 'MG_TXD3', '24': 'MG_TXC', '25': 'MG_TX_CTL',
   '27': 'MG_RXD3', '28': 'MG_RXD2', '31': 'MG_RXD1', '32': 'MG_RXD0', '33': 'MG_RX_CTL', '35': 'MG_RXC', '36': 'MG_MDC', '37': 'MG_MDIO', '38': 'MG_INT#',
   '41': 'MG_LEDMODE', '42': 'RESET_MOCI#', '45': 'MG_XO', '46': 'MG_XI', '48': 'MG_ISET'},
  nc=['13', '43', '47'], mpn='KSZ9131RNXI-TR', mfr='Microchip', desc='GbE RGMII PHY, QFN-48 (management port; same PHY and address as Toradex reference)',
  alt='KSZ9131RNXC (commercial temp); KSZ9031RNXIA (previous generation, pin compatible)')
for n in ('+3V3', '+3V3', '+1V8', '+1V8', '+1V8'):
    D.C('100nF', n)
for _ in range(6):
    D.C('100nF', '+1V2_PHY')
D.C('10uF', '+1V2_PHY'); D.C('10uF', '+3V3')
P('FB', 'SORX:FB', '600R@100MHz', 'Inductor_SMD:L_0603_1608Metric', {'1': '+1V2_PHY', '2': 'MG_1V2_PLL'}, mpn='BLM18AG601SN1D', mfr='Murata', desc='Ferrite bead 600 ohm 0603',
  alt='TDK MMZ1608B601C, Wurth 742792651')
D.C('100nF', 'MG_1V2_PLL'); D.C('10uF', 'MG_1V2_PLL')
for n in ('MG_RXD0', 'MG_RXD1', 'MG_RXD2', 'MG_RXD3', 'MG_RXC', 'MG_LED1', 'MG_LED2', 'MG_LEDMODE'):
    D.R('10k', '+1V8', n)          # MODE=1111 (RGMII, all speeds), PHYAD=7, single-LED mode
D.R('10k', 'MG_RX_CTL', 'GND')     # CLK125_EN = 0
D.R('1.5k', '+1V8', 'MG_MDIO'); D.R('4.7k', '+1V8', 'MG_INT#'); D.R('12.1k', 'MG_ISET', 'GND')
P('Y', XTAL['sym'], '25MHz', XTAL['fp'], {'1': 'MG_XI', '3': 'MG_XO', '2': 'GND', '4': 'GND'}, mpn=XTAL['mpn'], mfr=XTAL['mfr'], desc=XTAL['desc'], alt=XTAL['alt'])
D.C('27pF', 'MG_XI'); D.C('27pF', 'MG_XO')
for i in (1, 2):
    P('U', '74xGxx:74LVC1G07', '74LVC1G07', G['fp'], {'2': 'MG_LED%d' % i, '3': 'GND', '4': 'MG_LED%d_K' % i, '5': '+1V8'}, nc=['1'], mpn='SN74LVC1G07DBVR', mfr=G['mfr'],
      desc='Open-drain buffer (PHY LED, 1.8 V domain to 3.3 V LED)', alt='Nexperia 74LVC1G07GV')
D.C('100nF', '+1V8')
magjack('MGMT', 'MG', 'MG_LED2_K', 'MG_LED1_K')

# ================================================================================================
D.sheet = 'usb_console'
USBA = dict(sym='Connector:USB3_A', fp='Connector_USB:USB3_A_Receptacle_Wuerth_692122030100', mpn='692122030100', mfr='Wurth Elektronik',
            desc='USB 3.0 Type-A receptacle, horizontal, THR', alt='Molex 48393-0003 (footprint in library)')
SWI = dict(sym='Power_Management:TPS2051CDBV', fp='Package_TO_SOT_SMD:SOT-23-5', mpn='TPS2065CDBVR', mfr='Texas Instruments',
           desc='USB power switch 1 A, active-high enable, SOT-23-5', alt='TPS2069CDBVR (1.5 A), Diodes AP2161W-7 (pinout differs: verify)')
LVT = dict(sym='Logic_LevelTranslator:SN74LV1T34DBV', fp='Package_TO_SOT_SMD:SOT-23-5', mpn='SN74LV1T34DBVR', mfr='Texas Instruments',
           desc='Single buffer, 1.8 V input to 3.3 V output', alt='Nexperia 74LV1T34GV')
ESD2 = dict(sym='Power_Protection:USBLC6-2SC6', fp='Package_TO_SOT_SMD:SOT-23-6', mpn='USBLC6-2SC6', mfr='STMicroelectronics', desc='USB 2.0 ESD protection SOT-23-6',
            alt='Nexperia PRTR5V0U2X (SOT-143, footprint differs), Littelfuse SP0503BAHT')


def usb_port(n, label, ss):
    v = 'USB%d_VBUS' % n
    pins = {'1': v, '2': 'USB%d_D_N' % n, '3': 'USB%d_D_P' % n, '4': 'GND', '7': 'GND', '10': 'GND'}
    nc = []
    if ss:
        pins.update({'5': 'USB2_SSRX_N', '6': 'USB2_SSRX_P', '8': 'USB2_SSTX_N', '9': 'USB2_SSTX_P'})
    else:
        nc = ['5', '6', '8', '9']
    P('J', USBA['sym'], label, USBA['fp'], pins, nc, mpn=USBA['mpn'], mfr=USBA['mfr'], desc=USBA['desc'], alt=USBA['alt'])
    P('U', LVT['sym'], '74LV1T34', LVT['fp'], {'2': 'USB%d_EN' % n, '3': 'GND', '4': 'USB%d_EN_3V3' % n, '5': '+3V3'}, mpn=LVT['mpn'], mfr=LVT['mfr'], desc=LVT['desc'], alt=LVT['alt'])
    D.R('100k', 'USB%d_EN' % n, 'GND'); D.C('100nF', '+3V3')
    P('U', SWI['sym'], 'TPS2065C', SWI['fp'], {'1': v, '2': 'GND', '3': 'USB%d_OC#' % n, '4': 'USB%d_EN_3V3' % n, '5': '+5V_USB'}, mpn=SWI['mpn'], mfr=SWI['mfr'], desc=SWI['desc'], alt=SWI['alt'])
    D.R('10k', '+1V8', 'USB%d_OC#' % n); D.C('10uF', '+5V_USB'); D.C('100nF', v)
    P('C', 'SORX:CP', '150uF/10V', 'Capacitor_SMD:CP_Elec_6.3x7.7', {'1': v, '2': 'GND'}, mpn='EEE-FK1A151P', mfr='Panasonic', desc='Al electrolytic 150 uF 10 V 6.3x7.7 mm (USB VBUS bulk)',
      alt='Nichicon UCD1A151MCL1GS')
    P('U', ESD2['sym'], 'USBLC6-2', ESD2['fp'], {'1': 'USB%d_D_N' % n, '6': 'USB%d_D_N' % n, '3': 'USB%d_D_P' % n, '4': 'USB%d_D_P' % n, '2': 'GND', '5': v},
      mpn=ESD2['mpn'], mfr=ESD2['mfr'], desc=ESD2['desc'], alt=ESD2['alt'])


usb_port(2, 'USB 1 (USB 3.0)', True)
P('U', 'Power_Protection:TPD4E05U06DQA', 'TPD4E05U06', 'Package_SON:USON-10_2.5x1.0mm_P0.5mm',
  {'1': 'USB2_SSTX_P', '2': 'USB2_SSTX_N', '4': 'USB2_SSRX_P', '5': 'USB2_SSRX_N', '3': 'GND', '8': 'GND'}, nc=['6', '7', '9', '10'], mpn='TPD4E05U06DQAR', mfr='Texas Instruments',
  desc='4-channel ESD for SuperSpeed lines, USON-10 flow-through', alt='Nexperia PUSB3FR4 (footprint differs), Diodes DT1240-04LP-7')
usb_port(1, 'USB 2 (USB 2.0 / RECOVERY)', False)
D.R('10k', 'USB1_VBUS', 'USB1_VBUS_DET'); D.R('1k', 'USB1_ID', 'GND'); D.R('100k', '+1V8', 'USB1_ID', dnp=True)
# debug console
P('J', USBC['sym'], 'USB-C DEBUG', USBC['fp'], {'VBUS': 'VBUS_DBG_RAW', 'GND': 'GND', 'CC1': 'DBG_CC1', 'CC2': 'DBG_CC2', 'D+': 'DBG_D_P', 'D-': 'DBG_D_N', 'SHIELD': 'GND'},
  nc=['A8', 'B8'], mpn=USBC['mpn'], mfr=USBC['mfr'], desc=USBC['desc'], alt=USBC['alt'])
D.R('5.1k', 'DBG_CC1', 'GND'); D.R('5.1k', 'DBG_CC2', 'GND')
P('FB', 'SORX:FB', '600R@100MHz', 'Inductor_SMD:L_0603_1608Metric', {'1': 'VBUS_DBG_RAW', '2': 'VBUS_DBG'}, mpn='BLM18AG601SN1D', mfr='Murata', desc='Ferrite bead 600 ohm 0603',
  alt='TDK MMZ1608B601C')
P('U', ESD2['sym'], 'USBLC6-2', ESD2['fp'], {'1': 'DBG_D_N', '6': 'DBG_D_N', '3': 'DBG_D_P', '4': 'DBG_D_P', '2': 'GND', '5': 'VBUS_DBG'},
  mpn=ESD2['mpn'], mfr=ESD2['mfr'], desc=ESD2['desc'], alt=ESD2['alt'])
D.R('27', 'DBG_D_P', 'FT_DP'); D.R('27', 'DBG_D_N', 'FT_DM'); D.C('47pF', 'FT_DP'); D.C('47pF', 'FT_DM')
P('U', 'Interface_USB:FT230XS', 'FT230XS', 'Package_SO:SSOP-16_3.9x4.9mm_P0.635mm',
  {'1': 'DBG_TXD', '3': '+3V3_DBG', '4': 'DBG_RXD', '5': 'GND', '13': 'GND', '6': 'DBG_CTS#', '8': 'FT_DP', '9': 'FT_DM', '10': '+3V3_DBG', '11': '+3V3_DBG', '12': 'VBUS_DBG'},
  nc=['2', '7', '14', '15', '16'], mpn='FT230XS-R', mfr='FTDI', desc='USB to UART bridge, SSOP-16 (bus powered from the debug host)', alt='Silicon Labs CP2102N (footprint differs)')
D.C('4.7uF', 'VBUS_DBG'); D.C('100nF', 'VBUS_DBG'); D.C('100nF', '+3V3_DBG'); D.C('100nF', '+3V3_DBG'); D.R('10k', 'DBG_CTS#', 'GND')
P('U', LVT['sym'], '74LV1T34', LVT['fp'], {'2': 'CON_TXD_1V8', '3': 'GND', '4': 'CON_TXD_3V3', '5': '+3V3'}, mpn=LVT['mpn'], mfr=LVT['mfr'], desc=LVT['desc'], alt=LVT['alt'])
D.C('100nF', '+3V3')
P('U', '74xGxx:74LVC1G125', '74LVC1G125', G['fp'], {'1': 'GND', '2': 'CON_TXD_3V3', '3': 'GND', '4': 'DBG_RXD', '5': '+3V3_DBG'}, mpn='SN74LVC1G125DBVR', mfr=G['mfr'],
  desc='Buffer with Ioff, powered from the debug island (isolates FT230X when either side is off)', alt='Nexperia 74LVC1G125GV')
P('U', 'Interface_UART:MAX3232', 'TRS3232E', 'Package_SO:TSSOP-16_4.4x5mm_P0.65mm',
  {'1': 'RS_C1P', '3': 'RS_C1N', '4': 'RS_C2P', '5': 'RS_C2N', '2': 'RS_VP', '6': 'RS_VN', '11': 'CON_TXD_3V3', '14': 'RS232_TXD', '13': 'RS232_RXD', '12': 'CON_RX_RS232',
   '10': 'GND', '8': 'GND', '15': 'GND', '16': '+3V3'}, nc=['7', '9'], mpn='TRS3232EIPWR', mfr='Texas Instruments', desc='RS-232 transceiver 3-5.5 V, 15 kV ESD, TSSOP-16',
  alt='Analog Devices MAX3232EEUE+, Renesas ICL3232EIVZ')
D.C('100nF', 'RS_C1P', 'RS_C1N'); D.C('100nF', 'RS_C2P', 'RS_C2N'); D.C('100nF', 'RS_VP'); D.C('100nF', 'RS_VN'); D.C('100nF', '+3V3')
P('U', '74xGxx:74LVC1G08', '74LVC1G08', G['fp'], {'1': 'CON_RX_RS232', '2': 'DBG_TXD', '3': 'GND', '4': 'CON_RXD_1V8', '5': '+1V8'}, mpn='SN74LVC1G08DBVR', mfr=G['mfr'],
  desc='AND gate at 1.8 V: merges RS-232 and USB console receive (both idle high)', alt='Nexperia 74LVC1G08GV')
D.R('100k', '+1V8', 'DBG_TXD'); D.C('100nF', '+1V8')
P('J', 'Connector:RJ45', 'CONSOLE', 'Connector_RJ:RJ45_Amphenol_54602-x08_Horizontal',
  {'1': 'CON_RTSCTS', '8': 'CON_RTSCTS', '2': 'CON_DTRDSR', '7': 'CON_DTRDSR', '3': 'RS232_TXD', '6': 'RS232_RXD', '4': 'GND', '5': 'GND'},
  mpn='54602-908LF', mfr='Amphenol ICC', desc='RJ45 jack 8P8C, unshielded, horizontal THT (Cisco console pinout)', alt='TE 5555164-1, Molex 95501-2881 (check footprint)')
D.FLAG('VBUS_DBG_RAW'); D.FLAG('VBUS_DBG'); D.FLAG('+3V3_DBG'); D.FLAG('USB1_VBUS'); D.FLAG('USB2_VBUS')

# ================================================================================================
D.sheet = 'mgmt_io'
P('J', 'Connector:Micro_SD_Card_Det_Hirose_DM3AT', 'microSD', 'Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5',
  {'1': 'SD_D2', '2': 'SD_D3', '3': 'SD_CMD', '4': '+3V3_SD', '5': 'SD_CLK_R', '6': 'GND', '7': 'SD_D0', '8': 'SD_D1', '9': 'SD_CD#', '10': 'GND', '11': 'GND'},
  mpn='DM3AT-SF-PEJM5', mfr='Hirose', desc='microSD socket, push-push, card detect', alt='Molex 104031-0811, Wurth 693072010801 (footprints in library)')
D.R('22', 'SD_CLK', 'SD_CLK_R')
P('Q', 'Transistor_FET:BSS138', 'BSS138', 'Package_TO_SOT_SMD:SOT-23', {'1': 'SD_PWR_EN', '2': 'GND', '3': 'SD_PWR_G'}, mpn='BSS138LT1G', mfr='onsemi', desc='N-MOSFET 50 V, logic level, SOT-23',
  alt='Diodes BSS138-7-F, Nexperia BSS138BK')
P('Q', 'Transistor_FET:AO3401A', 'AO3401A', 'Package_TO_SOT_SMD:SOT-23', {'1': 'SD_PWR_G', '2': '+3V3', '3': '+3V3_SD'}, mpn='AO3401A', mfr='Alpha & Omega', desc='P-MOSFET -30 V 4 A SOT-23 (SD card power switch)',
  alt='Diodes DMG2305UX-7, Vishay SI2301CDS')
D.R('100k', 'SD_PWR_EN', 'GND'); D.R('100k', '+3V3', 'SD_PWR_G'); D.C('10uF', '+3V3_SD'); D.C('100nF', '+3V3_SD')
# I2C housekeeping
P('U', 'Sensor_Temperature:TMP1075D', 'TMP1075', 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',
  {'1': 'I2C1_SDA', '2': 'I2C1_SCL', '3': 'TMP_ALERT#', '4': 'GND', '5': 'GND', '6': 'GND', '7': 'GND', '8': '+1V8'}, mpn='TMP1075DR', mfr='Texas Instruments',
  desc='Temperature sensor, I2C, 1.7-5.5 V, SOIC-8 (address 0x48)', alt='TI TMP75CIDR (1.4 V min); NXP LM75B needs 2.8 V')
D.C('100nF', '+1V8'); D.R('10k', '+1V8', 'TMP_ALERT#')
P('U', 'Interface_Expansion:TCA9534', 'TCA9534', 'Package_SO:TSSOP-16_4.4x5mm_P0.65mm',
  {'1': 'GND', '2': 'GND', '3': 'GND', '4': 'LED_SYS#', '5': 'LED_FEEDA#', '6': 'LED_FEEDB#', '7': 'LED_FIXA#', '9': 'LED_FIXB#', '10': 'PWR_FLT#', '11': 'TMP_ALERT#', '12': 'PD_ATTACH#',
   '8': 'GND', '13': 'IOEXP_INT#', '14': 'I2C1_SCL', '15': 'I2C1_SDA', '16': '+1V8'}, mpn='TCA9534PWR', mfr='Texas Instruments', desc='8-bit I2C I/O expander, 1.65-5.5 V, TSSOP-16 (address 0x20)',
  alt='NXP PCA9534PW (2.3 V min: not usable at 1.8 V); TI TCA9554PWR')
D.C('100nF', '+1V8'); D.R('10k', '+1V8', 'IOEXP_INT#')
LED = dict(fp='LED_SMD:LED_Kingbright_KPA-3010_3x2x1mm', mfr='Kingbright')


def led(label, color, a_net, k_net, r):
    mpn = {'green': 'KPA-3010SGC', 'red': 'KPA-3010EC', 'yellow': 'KPA-3010YC'}[color]
    ref = P('D', 'SORX:LED', label, LED['fp'], {'A': a_net, 'K': k_net}, mpn=mpn, mfr=LED['mfr'], desc='LED %s, right angle SMD 3.0x2.0x1.0 mm' % color,
            alt='Any right-angle 3010 LED, e.g. Wurth 155124 series (check pads)')
    D.R(r, '+3V3', a_net)
    return ref


led('PWR', 'green', 'LED_PWR_A', 'GND', '1k')
led('SYS', 'green', 'LED_SYS_A', 'LED_SYS#', '330')
led('FEED A', 'green', 'LED_FEEDA_A', 'LED_FEEDA#', '330')
led('FEED B', 'green', 'LED_FEEDB_A', 'LED_FEEDB#', '330')
led('FIX A', 'yellow', 'LED_FIXA_A', 'LED_FIXA#', '330')
led('FIX B', 'yellow', 'LED_FIXB_A', 'LED_FIXB#', '330')
led('ALARM', 'red', 'LED_ALARM_A', 'LED_ALARM_K', '470')
P('U', '74xGxx:74LVC1G07', '74LVC1G07', G['fp'], {'2': 'ALARM_OFF', '3': 'GND', '4': 'LED_ALARM_K', '5': '+1V8'}, nc=['1'], mpn='SN74LVC1G07DBVR', mfr=G['mfr'],
  desc='Open-drain buffer: ALARM LED is on until the SoM drives ALARM_OFF high', alt='Nexperia 74LVC1G07GV')
D.R('100k', 'ALARM_OFF', 'GND'); D.C('100nF', '+1V8')
# hardware watchdog
P('U', 'Power_Supervisor:MAX6369', 'MAX6369', 'Package_TO_SOT_SMD:SOT-23-8',
  {'1': 'WD_KICK_3V3', '2': 'GND', '4': 'WD_SET0', '5': '+3V3', '6': 'WD_EN_3V3', '7': 'WD_OUT#', '8': '+3V3'}, mpn='MAX6369KA+T', mfr='Analog Devices',
  desc='Pin-selectable watchdog timer, open-drain output, SOT-23-8', alt='None drop-in; TI TPS3431 needs a layout change')
P('U', LVT['sym'], '74LV1T34', LVT['fp'], {'2': 'WD_KICK', '3': 'GND', '4': 'WD_KICK_3V3', '5': '+3V3'}, mpn=LVT['mpn'], mfr=LVT['mfr'], desc=LVT['desc'], alt=LVT['alt'])
P('U', LVT['sym'], '74LV1T34', LVT['fp'], {'2': 'WD_EN', '3': 'GND', '4': 'WD_EN_3V3', '5': '+3V3'}, mpn=LVT['mpn'], mfr=LVT['mfr'], desc=LVT['desc'], alt=LVT['alt'])
P('U', '74xGxx:74LVC1G07', '74LVC1G07', G['fp'], {'2': 'WD_SET0_N', '3': 'GND', '4': 'WD_SET0', '5': '+1V8'}, nc=['1'], mpn='SN74LVC1G07DBVR', mfr=G['mfr'],
  desc='Open-drain buffer (watchdog SET0: low selects 10 s)', alt='Nexperia 74LVC1G07GV')
D.R('100k', 'WD_KICK', 'GND'); D.R('100k', 'WD_EN', 'GND'); D.R('100k', '+1V8', 'WD_SET0_N'); D.R('10k', '+3V3', 'WD_SET0')
D.R('0', 'WD_OUT#', 'RESET_MICO#'); D.C('100nF', '+3V3'); D.C('100nF', '+3V3'); D.C('100nF', '+3V3'); D.C('100nF', '+1V8')
# fan
P('J', 'Connector_Generic:Conn_01x04', 'FAN', 'Connector:FanPinHeader_1x04_P2.54mm_Vertical', {'1': 'GND', '2': 'FAN_5V', '3': 'FAN_TACH_RAW', '4': 'FAN_PWM_OD'},
  mpn='47053-1000', mfr='Molex', desc='4-pin PWM fan header 2.54 mm', alt='Wurth 61900411121 (KK-style), TE 640456-4')
P('FB', 'SORX:FB', '600R@100MHz', 'Inductor_SMD:L_0603_1608Metric', {'1': '+5V_USB', '2': 'FAN_5V'}, mpn='BLM18AG601SN1D', mfr='Murata', desc='Ferrite bead 600 ohm 0603',
  alt='TDK MMZ1608B601C')
D.C('10uF', 'FAN_5V')
P('U', '74xGxx:74LVC1G07', '74LVC1G07', G['fp'], {'2': 'FAN_PWM_1V8', '3': 'GND', '4': 'FAN_PWM_OD', '5': '+1V8'}, nc=['1'], mpn='SN74LVC1G07DBVR', mfr=G['mfr'],
  desc='Open-drain buffer (fan PWM; released = full speed)', alt='Nexperia 74LVC1G07GV')
P('U', '74xGxx:74LVC1G17', '74LVC1G17', G['fp'], {'2': 'FAN_TACH_RAW', '3': 'GND', '4': 'FAN_TACH', '5': '+1V8'}, mpn='SN74LVC1G17DBVR', mfr=G['mfr'],
  desc='Schmitt buffer at 1.8 V (fan tach, 5 V tolerant input)', alt='Nexperia 74LVC1G17GV')
D.R('100k', '+1V8', 'FAN_PWM_1V8'); D.R('10k', '+3V3', 'FAN_TACH_RAW'); D.C('100nF', '+1V8'); D.C('100nF', '+1V8')
D.FLAG('+3V3_SD'); D.FLAG('FAN_5V')
for i in range(1, 5):
    P('H', 'Mechanical:MountingHole', 'M3', 'MountingHole:MountingHole_3.2mm_M3', {}, desc='M3 mounting hole', inbom=False)
for i in range(1, 4):
    P('FID', 'Mechanical:Fiducial', 'Fiducial', 'Fiducial:Fiducial_1mm_Mask2mm', {}, desc='Fiducial', inbom=False)

DESIGN = D


def nets():
    """net -> sorted list of (ref, pin-key)"""
    out = collections.defaultdict(list)
    for p in D.parts.values():
        for k, n in p.pins.items():
            out[n].append((p.ref, k))
    return out


if __name__ == '__main__':
    N = nets()
    print('parts', len(D.parts), 'nets', len(N))
    single = sorted(n for n, v in N.items() if len(v) == 1)
    print('single-pin nets:', single)
    cnt = collections.Counter(p.sheet for p in D.parts.values())
    print(dict(cnt))
