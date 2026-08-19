#!/bin/bash
# Applies the ReSpeaker 2-Mics v2.0 (TLV320AIC3104) mic-path mixer state.
# Mixer state is volatile (chip resets to defaults on every power-up) and
# alsactl can't target this by name reliably, so this runs explicitly at
# boot via systemd instead -- confirmed live: without this, Mic2L/Mic2R
# stay disconnected from the PGA mixer and capture is silent.
CARD="seeed2micvoicec"
for i in $(seq 1 20); do
    amixer -c "$CARD" scontrols >/dev/null 2>&1 && break
    sleep 0.5
done
amixer -c "$CARD" sset 'Left PGA Mixer Mic2L' on
amixer -c "$CARD" sset 'Right PGA Mixer Mic2R' on
amixer -c "$CARD" sset 'PGA' 40,40 unmute
