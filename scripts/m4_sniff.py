"""快速嗅探某串口 N 秒，打印含关键字的行。Usage: python m4_sniff.py <port> <baud> <seconds>"""
import serial, time, sys, re

port_name, baud, seconds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
KEY = ('csi_send', 'error', 'Error', 'mac:', 'connected', 'disconnect', 'assert',
       'abort', 'Guru', 'retry', 'ESPNOW', 'ESP-NOW', 'free_heap', 'connected to',
       'state:', 'wifi:', 'rst:')
port = serial.Serial(port_name, baud, timeout=0.2, rtscts=False, dsrdtr=False)
t0 = time.time()
buf = b''
try:
    # 打开串口会触发一次 DTR/RTS 复位（CH340/USB-SJ 均如此），正好能看到启动日志
    time.sleep(0.3)
    while time.time() - t0 < seconds:
        d = port.read(4096)
        if d:
            buf += d
            lines = re.split(rb'[\r\n]+', buf)
            buf = lines[-1]
            for ln in lines[:-1]:
                try:
                    s = ln.decode('utf-8', errors='replace')
                except Exception:
                    continue
                clean = ''.join(c if 32 <= ord(c) < 127 or ord(c) > 0x2E7F else ' ' for c in s).strip()
                if clean and any(k in clean for k in KEY):
                    print(clean[:150], flush=True)
finally:
    port.close()
print('--- sniff end %s ---' % port_name)
