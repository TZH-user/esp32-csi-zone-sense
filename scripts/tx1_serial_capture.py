import serial, time, sys
port_name, baud, seconds, out_path = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
port = serial.Serial(port_name, baud, timeout=1)
# esptool ClassicReset sequence (DTR=False, RTS pulse) -> chip reboots in run mode
port.dtr = False
port.rts = True
time.sleep(0.1)
port.rts = False
t0 = time.time()
with open(out_path, 'wb') as f:
    while time.time() - t0 < seconds:
        data = port.read(4096)
        if data:
            f.write(data)
            f.flush()
port.close()
print("capture done:", out_path)
