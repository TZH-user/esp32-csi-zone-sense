"""M1 RX serial capture: open port, record raw bytes for N seconds, no reset."""
import serial, time, sys

port_name, baud, seconds, out_path = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
port = serial.Serial(port_name, baud, timeout=1)
t0 = time.time()
total = 0
with open(out_path, 'wb') as f:
    while time.time() - t0 < seconds:
        data = port.read(4096)
        if data:
            f.write(data)
            f.flush()
            total += len(data)
port.close()
print("capture done: %s, %d bytes in %.1fs" % (out_path, total, time.time() - t0))
