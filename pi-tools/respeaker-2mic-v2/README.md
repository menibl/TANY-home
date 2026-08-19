# ReSpeaker 2-Mics Pi HAT v2.0 setup

The board's product name still says "2-Mics Pi HAT" but Seeed swapped the
onboard codec in the v2.0 revision from a WM8960 to a **TI TLV320AIC3104**
(chip package print reads "AC3104I" — that's just TI's shortened marking,
not a different chip). Every generic WM8960-targeted overlay/driver will
appear to work at the I2C protocol level (the chip ACKs bytes) while doing
nothing audible, because the register map is completely different silicon.

## Install

```bash
# 1. Enable I2C
sudo sed -i 's/^#dtparam=i2c_arm=on/dtparam=i2c_arm=on/' /boot/firmware/config.txt
# (add the line instead if it's not present at all)

# 2. Install the official overlay
sudo cp respeaker-official.dts /tmp/
dtc -@ -I dts -O dtb -o /tmp/respeaker-official.dtbo /tmp/respeaker-official.dts
sudo cp /tmp/respeaker-official.dtbo /boot/firmware/overlays/
echo 'dtoverlay=respeaker-official' | sudo tee -a /boot/firmware/config.txt

# 3. Install the mixer-init service (mic path is muted by default on
#    every power-up -- this connects Mic2L/Mic2R into the PGA mixer and
#    sets a sane capture gain)
mkdir -p ~/wm8960-setup
cp respeaker_mixer_init.sh ~/wm8960-setup/
chmod +x ~/wm8960-setup/respeaker_mixer_init.sh
sudo cp respeaker-mixer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now respeaker-mixer.service

sudo reboot
```

## Verify

```bash
arecord -l                                    # expect: card N: seeed2micvoicec
arecord -D hw:seeed2micvoicec,0 -f S16_LE -r 16000 -c 2 -d 3 /tmp/t.wav
```

Add a `pcm.respeaker_plug { type plug; slave.pcm "hw:seeed2micvoicec,0"; }`
block to `~/.asoundrc` so capture-svc (which names devices explicitly
rather than relying on PortAudio's own "default" resolution — see
`docker-compose.pi.yml`) can reference it as `MIC_DEVICE_NAME=respeaker_plug`.

## What went wrong before this, for the record

Confirmed over a long live debugging session, in order:
- Mainline community `wm8960-soundcard` overlay: chip never ACKed a
  reset at the WM8960's usual address (0x1a) -- turned out the real
  device sits at 0x18.
- Even at 0x18: the in-kernel driver's own "is this really a WM8960"
  sanity check rejected the probe (`Not wm8960, wm8960 reg can not read
  by i2c`) -- patched that check out to get past probe, which was
  wrong in hindsight: the check was correct, this genuinely isn't a
  WM8960.
- With the check bypassed: I2C comms, DAPM power state (including mic
  bias), and every mixer register all checked out correct by every
  software-observable measure, yet captured audio was silent, always,
  with zero variance across dozens of tests -- a strong, if
  circumstantial, sign that entire register map being programmed
  didn't correspond to anything real on this chip.
- User read the actual chip package marking off the physical board:
  "AC3104I" -> TI TLV320AIC3104, not WM8960 at all.
- The in-kernel `snd-soc-tlv320aic3x` driver already exists and
  probes cleanly against this chip with zero patching.
- A hand-authored overlay for it still failed (`Unable to install hw
  params`) because it didn't specify a clock at all, and a second
  attempt got the MCLK frequency and I2S clock-master role backwards
  (assumed Pi-as-master, board actually wants codec-as-master). Seeed's
  own overlay in `Seeed-Studio/seeed-linux-dtoverlays` has the correct
  values for both and is what's checked into this directory.
