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

    # plughw, not raw hw: the Loopback device doesn't actually support
    # 16000Hz — aplay against "hw:Loopback,0,0" silently played at
    # 44100Hz instead (its own "Warning: rate is not accurate (requested
    # = 16000Hz, got = 44100Hz)") while treating the incoming bytes as
    # still being 16kHz-paced, i.e. played back ~2.75x too fast — not
    # quiet, just garbled into noise. plughw resamples properly instead
    # of silently running the hardware clock at the wrong rate.
    #
    # -B/-F: a much bigger buffer than aplay's default — UDP packets from
    # the M5StickC arrive in irregular bursts (network jitter, not a
    # steady clocked stream the way a real audio device would produce
    # one), and the default buffer was too small to absorb that,
    # causing constant underruns (confirmed via aplay's own "underrun!!!"
    # log output — isolated loopback test without UDP in the picture at
    # all played back a clean signal, so the loopback device itself was
    # never the problem).
    proc = subprocess.Popen(
        ["aplay", "-D", "plughw:Loopback,0,0", "-f", "S16_LE",
         "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw",
         "-B", "500000", "-F", "50000", "-"],
        stdin=subprocess.PIPE,
    )

    print(f"Listening for M5StickC mic audio on UDP :{UDP_PORT}, "
          f"piping into ALSA loopback...", flush=True)
    count = 0
    try:
        while True:
            data, addr = sock.recvfrom(4096)
            count += 1
            if count % 50 == 0:
                print(f"received {count} packets so far, last from {addr}, "
                      f"len={len(data)}, aplay alive={proc.poll() is None}", flush=True)
            proc.stdin.write(data)
            proc.stdin.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        proc.terminate()

if __name__ == "__main__":
    sys.exit(main())
