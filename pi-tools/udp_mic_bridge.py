"""
Receives PCM16 mono audio over UDP from the M5StickC PLUS's mic
(see ../m5stickc-mic/m5stickc-mic.ino) and pipes it into an ALSA
loopback device. capture-svc then reads it back out the other side of
that loopback as an ordinary capture device via ~/.asoundrc — it has no
idea the "microphone" is actually coming over WiFi from a separate
board, and needs no code changes to use it.

Requires the snd-aloop kernel module:
    sudo modprobe snd-aloop
    echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
"""
import socket
import subprocess
import sys

UDP_PORT = 5005
SAMPLE_RATE = 16000

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))

    proc = subprocess.Popen(
        ["aplay", "-D", "hw:Loopback,0,0", "-f", "S16_LE",
         "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw", "-"],
        stdin=subprocess.PIPE,
    )

    print(f"Listening for M5StickC mic audio on UDP :{UDP_PORT}, "
          f"piping into ALSA loopback...", flush=True)
    try:
        while True:
            data, addr = sock.recvfrom(4096)
            proc.stdin.write(data)
            proc.stdin.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        proc.terminate()

if __name__ == "__main__":
    sys.exit(main())
