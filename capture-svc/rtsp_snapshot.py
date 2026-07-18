"""
Grabs 1-3 frames from a camera on demand — either the RTSP camera (the
clap trigger) or the machine's own local webcam (the dashboard's manual
trigger button, since a clap's whole point is not being at the computer).
Deliberately opens and closes the capture per-call instead of holding it
open continuously — an old machine doing 24/7 decode just to be ready
"in case" is wasted CPU. The couple hundred ms of connect latency is a
non-issue since this only runs after a trigger.
"""
import base64
import cv2


def _grab(source, num_frames: int, source_label: str) -> list[str]:
    """Returns a list of base64-encoded JPEGs. `source` is whatever
    cv2.VideoCapture accepts — an RTSP URL string, or a local device
    index (0 = default webcam)."""
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {source_label}: {source}")

    frames_b64 = []
    try:
        for _ in range(num_frames):
            ok, frame = cap.read()
            if not ok:
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                frames_b64.append(base64.b64encode(buf.tobytes()).decode("ascii"))
    finally:
        cap.release()

    if not frames_b64:
        raise RuntimeError(f"{source_label} opened but no frames could be read")
    return frames_b64


def grab_snapshots(rtsp_url: str, num_frames: int = 2) -> list[str]:
    return _grab(rtsp_url, num_frames, "RTSP stream")


def grab_webcam_snapshots(num_frames: int = 2, device_index: int = 0) -> list[str]:
    return _grab(device_index, num_frames, "local webcam")
