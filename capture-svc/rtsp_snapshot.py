"""
Grabs 1-3 frames from an RTSP camera on demand. Deliberately opens and
closes the stream per-call instead of holding it open continuously — an old
machine doing 24/7 RTSP decode just to be ready "in case" is wasted CPU.
The couple hundred ms of connect latency is a non-issue since this only
runs after a clap trigger.
"""
import base64
import cv2


def grab_snapshots(rtsp_url: str, num_frames: int = 2) -> list[str]:
    """Returns a list of base64-encoded JPEGs."""
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        raise RuntimeError(f"could not open RTSP stream: {rtsp_url}")

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
        raise RuntimeError("RTSP stream opened but no frames could be read")
    return frames_b64
