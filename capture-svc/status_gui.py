"""
Always-on-top status window so you can see what capture-svc is doing in
real time — mainly to tell "it didn't hear the clap" apart from "it
heard it, Claude is just slow to answer". Runs Tkinter on its own
thread since Tkinter isn't safe to drive from asyncio's thread, and
takes state updates through a thread-safe queue.
"""
import queue
import threading
import tkinter as tk

_STATES = {
    "listening":   ("מאזין למחיאת כף...",      "#1a1a1a", "#4ade80"),
    "clap":        ("מחיאה זוהתה!",             "#14532d", "#eab308"),
    "identifying": ("מזהה מי זה...",            "#14532d", "#eab308"),
    "greeting":    ("אומר שלום...",             "#14532d", "#4ade80"),
    "session":     ("בשיחה — מקשיב",            "#052e16", "#4ade80"),
    "heard":       ("שמעתי: {text}",            "#052e16", "#4ade80"),
    "thinking":    ("חושב...",                  "#1e1b4b", "#a78bfa"),
    "speaking":    ("עונה...",                  "#052e16", "#4ade80"),
    "error":       ("שגיאה — ראה לוג",          "#450a0a", "#f87171"),
}


class StatusWindow:
    def __init__(self):
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state: str, text: str = ""):
        """Thread-safe: call this from any thread, including asyncio's."""
        self._queue.put((state, text))

    def _run(self):
        root = tk.Tk()
        root.title("HomeBot")
        root.geometry("360x140+40+40")
        root.attributes("-topmost", True)
        root.configure(bg="#1a1a1a")

        label = tk.Label(
            root, text=_STATES["listening"][0], font=("Segoe UI", 16, "bold"),
            fg=_STATES["listening"][2], bg=_STATES["listening"][1],
            wraplength=340, justify="center",
        )
        label.pack(expand=True, fill="both")

        def poll():
            try:
                while True:
                    state, text = self._queue.get_nowait()
                    template, bg, fg = _STATES.get(state, _STATES["error"])
                    label.config(text=template.format(text=text), bg=bg, fg=fg)
                    root.configure(bg=bg)
            except queue.Empty:
                pass
            root.after(100, poll)

        root.after(100, poll)
        root.mainloop()
