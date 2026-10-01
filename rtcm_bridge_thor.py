"""
RTCM3 bridge for the rover (Thor) side: connects to the base station Pi's
RTCM relay over WiFi and writes every byte straight to the rover board's
correction-input UART (the port that used to be fed by the XBee receiver).

Replaces the XBee RF hop with a WiFi link. The rover's own ZED-F9P does all
RTK fusion on-chip exactly as before - this only changes how the RTCM3
bytes get to it.
"""
import serial
import socket
import time

PI_HOST = "192.168.0.64"
PI_PORT = 5015
DEVICE = "/dev/ttyUSB0"
BAUD = 115200  # matches the base station's UART2 (the XBee-facing port)


def main():
    while True:
        ser = None
        sock = None
        try:
            ser = serial.Serial(DEVICE, BAUD, timeout=1)
            print(f"Opened {DEVICE} at {BAUD} baud")
            sock = socket.create_connection((PI_HOST, PI_PORT), timeout=10)
            sock.settimeout(1.0)
            print(f"Connected to RTCM relay at {PI_HOST}:{PI_PORT}")
            total_bytes = 0
            last_log = time.time()
            while True:
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    print("relay closed connection")
                    break
                ser.write(data)
                total_bytes += len(data)
                if time.time() - last_log > 60:
                    print(f"relayed {total_bytes} bytes in the last 60s")
                    total_bytes = 0
                    last_log = time.time()
        except Exception as e:
            print(f"error: {e}, retrying in 3s")
        finally:
            for h in (sock, ser):
                try:
                    h.close()
                except Exception:
                    pass
            time.sleep(3)


if __name__ == "__main__":
    main()
