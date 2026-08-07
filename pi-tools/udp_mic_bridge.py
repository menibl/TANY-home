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
import array
import socket
import subprocess
import sys

UDP_PORT = 5005
IN_RATE = 16000    # what the M5 actually sends
OUT_RATE = 44100    # the Loopback device's real native rate -- raw
                     # hw:Loopback,0,0 at 16000Hz silently ran at 44100Hz
                     # instead ("Warning: rate is not accurate"), and
                     # switching to plughw to fix that added enough
                     # real-time-resampling CPU cost on a Pi 3B+ to cause
                     # near-continuous underruns -- confirmed live: direct
                     # capture off hw:Loopback,1,0 during a real M5 test
                     # was pure silence (peak=0 over 2s) despite the
                     # bridge actively receiving and writing packets the
                     # whole time, while a synthetic tone written to
                     # plughw:Loopback,0,0 round-tripped fine, isolating
                     # the fault to that resampling step specifically.


class _LinearResampler:
    """Continuous linear-interpolation upsampler across packet
    boundaries -- resampling each UDP packet in isolation would leave an
    audible click/discontinuity at every packet edge, so this carries
    the trailing sample and fractional phase from one call to the next."""

    def __init__(self, in_rate: int, out_rate: int):
        self._step = in_rate / out_rate
        self._phase = 0.0
        self._prev_sample = 0

    def push(self, pcm16_bytes: bytes) -> bytes:
        samples = array.array("h")
        samples.frombytes(pcm16_bytes)
        if not samples:
            return b""
        # prepend the last sample from the previous call so interpolation
        # is continuous across the packet boundary, not just within it
        extended = array.array("h", [self._prev_sample]) + samples
        out = array.array("h")
        idx = self._phase
        n = len(extended)
        while idx < n - 1:
            i = int(idx)
            frac = idx - i
            a, b = extended[i], extended[i + 1]
            out.append(int(a + (b - a) * frac))
            idx += self._step
        self._phase = idx - (n - 1)
        self._prev_sample = samples[-1]
        return out.tobytes()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    resampler = _LinearResampler(IN_RATE, OUT_RATE)

    # -B/-F: a much bigger buffer than aplay's default — UDP packets from
    # the M5StickC arrive in irregular bursts (network jitter, not a
    # steady clocked stream the way a real audio device would produce
    # one), and the default buffer was too small to absorb that,
    # causing constant underruns (confirmed via aplay's own "underrun!!!"
    # log output — isolated loopback test without UDP in the picture at
    # all played back a clean signal, so the loopback device itself was
    # never the problem). Raw hw device, not plughw, and already
    # resampled in Python above -- see OUT_RATE comment.
    proc = subprocess.Popen(
        ["aplay", "-D", "hw:Loopback,0,0", "-f", "S16_LE",
         "-r", str(OUT_RATE), "-c", "1", "-t", "raw",
         "-B", "500000", "-F", "50000", "-"],
        stdin=subprocess.PIPE,
    )

    print(f"Listening for M5StickC mic audio on UDP :{UDP_PORT}, "
          f"resampling {IN_RATE}->{OUT_RATE}Hz, piping into ALSA loopback...", flush=True)
    count = 0
    try:
        while True:
            data, addr = sock.recvfrom(4096)
            count += 1
            if count % 50 == 0:
                print(f"received {count} packets so far, last from {addr}, "
                      f"len={len(data)}, aplay alive={proc.poll() is None}", flush=True)
            proc.stdin.write(resampler.push(data))
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
